"""Exercise normal generator resume and live extension against offline fixtures."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError

import generate
import company
from engine import DAY
from integrations.company_run.timing import DEFAULT_TIMING
from native.recurring_extensions import open_catalog
from norvale.loader.erp_client import ErpClient
from test_incremental_cli import FrappeFixture


class RecurringExtensions(unittest.TestCase):
    def test_sales_outage_preserves_pending_order_and_independent_dispatch(self):
        for lost_reply in (False, True):
            with self.subTest(lost_reply=lost_reply), TemporaryDirectory() as temp:
                self.check_sales_outage(Path(temp), lost_reply)

    def check_sales_outage(self, root, lost_reply):
        run = root/'run'
        env = run/'env'
        env.mkdir(parents=True)
        (env/'site-created').touch()
        (env/'company-sources.json').touch()
        config = dict(company.DEFAULT_CONFIG, days=9, company_operating=True,
                      company_timing=DEFAULT_TIMING,
                      company_authority='integrations.company_run.authority:Authority')
        (run/'config.json').write_text(json.dumps(config))
        controller = company.Engine.__new__(company.Engine)
        controller.config, controller.state = config, {'completed': []}
        state = {'day': 8, 'docs': {}, 'completed': [
            key for _, key, _ in controller.recurring_events() if key != 'd0008:native-clock']}
        (run/'state.json').write_text(json.dumps(state))

        registry = root/'registry.json'
        employee = {'user': 'counter@warehouse.test', 'role': 'counter-sales'}
        registry.write_text(json.dumps({'warehouse-sales': {
            'adapter': 'erpnext-sales', 'token_env': 'TEST_RECURRING_TOKEN', 'employee': employee,
            'erp': {'url': 'http://warehouse.test', 'site': 'isolated.test',
                    'company': company.Engine.company, 'customer': 'Demo Retail',
                    'item': 'DEFAULT-SKU', 'warehouse': 'Finished Goods - NCC'},
        }}))
        addition = root/'extension.json'
        start = 8*DAY + 780
        completion = start + 15
        addition.write_text(json.dumps({
            'id': 'open-warehouse-sales',
            'systems': {'warehouse-sales': {'connection': 'warehouse-sales'}},
            'roles': {'counter-sales': {'permissions': ['write:warehouse-sales:order'],
                                        'shifts': [[780, 840]]}},
            'products': {'lotion': {'system': 'warehouse-sales', 'item': 'LOTION-2'}},
            'events': [{'id': 'warehouse-order', 'kind': 'order', 'role': 'counter-sales',
                        'at': start, 'duration': 15, 'payload': {'product': 'lotion',
                            'case': 'PO-2', 'quantity': 2, 'rate': 3}}],
        }))
        service = FrappeFixture(root/'orders.json')
        service.user = employee['user']
        db_path = run/'scheduled-additions/engine.sqlite3'
        visited = []
        outage = True
        initial_failure = not lost_reply

        def operation():
            with sqlite3.connect(db_path) as db:
                data, = db.execute("SELECT data FROM writes").fetchone()
            return json.loads(data)

        def request(req, timeout):
            if initial_failure:
                raise RuntimeError('interrupted before retry was scheduled')
            if outage and (not lost_reply or service.posts):
                raise URLError(ConnectionRefusedError(111, 'Connection refused'))
            return service.open(req, timeout)

        def client(*args):
            api = ErpClient(*args)
            api.opener = SimpleNamespace(open=request)
            return api

        def dispatch(_, clock, payload):
            visited.append((payload['participant'], clock.now))

        def resume(*args):
            generate.main(['--run', str(run), *args])

        def stage(_):
            pass

        replacements = {key: stage for key in (
            'service', 'arrivals', 'inspections', 'demand', 'fulfillment', 'production',
            'replenishment', 'approvals', 'bank_statements', 'settlements', 'management')}
        with patch.multiple(company.Engine, **replacements, scheduled_company_event=dispatch), \
             patch.object(company.Engine, 'make_company_erp'), patch.object(company.Engine, 'advance_clock'), \
             patch.object(company, 'Stack', return_value=SimpleNamespace(endpoints=lambda: {})), \
             patch('native.incremental_erp.ErpClient', side_effect=client), \
             patch('integrations.company_run.lake.scheduled_scan'), \
             patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('offline test forbids network')), \
             patch.dict(os.environ, NORVALE_CONNECTIONS_FILE=str(registry), TEST_RECURRING_TOKEN='test-only'), \
             redirect_stdout(io.StringIO()):
            resume('--extend', str(addition))
            clock = open_catalog(run, config, state)
            independent = [(name, completion + i) for i, name in enumerate(('mes', 'lab', 'site'), 1)]
            for name, at in independent:
                clock.schedule(name, 'company-native', at, payload={'participant': name})
            if lost_reply:
                service.lose_reply = True
            else:
                with self.assertRaisesRegex(RuntimeError, 'interrupted before retry'):
                    resume()
                with sqlite3.connect(db_path) as db:
                    db.execute("UPDATE writes SET data = json_remove(data, '$.event')")
                original = operation()
                self.assertEqual(original['next_attempt'], completion)
            initial_failure = False
            resume()
            self.assertEqual(visited, independent)
            pending = operation()
            self.assertEqual(pending['status'], 'pending')
            self.assertEqual(pending['next_attempt'], completion + DAY)
            self.assertEqual(pending['action']['at'], completion)
            self.assertEqual(pending['action']['order_role'], employee['role'])
            self.assertEqual(service.posts, int(lost_reply))
            if not lost_reply:
                self.assertEqual(pending, dict(original, next_attempt=completion + DAY))
            else:
                committed_marker, = [row['custom_norvale_operation'] for row in service.rows()]
                self.assertIn(pending['retry_id'] + '-m' + str(completion), committed_marker)

            outage = False
            # An operator registry edit must not redirect an already queued write
            # or its lost-reply reconciliation to another deployment.
            saved_registry = registry.read_text()
            changed = json.loads(saved_registry)
            changed['warehouse-sales']['erp']['site'] = 'different-site.test'
            registry.write_text(json.dumps(changed))
            with patch('native.incremental_erp.ErpClient', side_effect=AssertionError('wrong site contacted')):
                with self.assertRaisesRegex(ValueError, 'Connection target changed'):
                    resume('--days', '10')
            self.assertEqual(operation(), pending)
            self.assertEqual(service.posts, int(lost_reply))
            registry.write_text(saved_registry)
            service.user = 'other@warehouse.test'
            with self.assertRaisesRegex(ValueError, 'ERP token must authenticate as'):
                resume()
            self.assertEqual(operation(), pending)
            service.user = employee['user']
            resume()
            resume()
            confirmed = operation()
            self.assertEqual(confirmed['status'], 'confirmed')
            self.assertIsNone(confirmed['next_attempt'])
            for key in ('retry_id', 'action', 'evidence', 'source_version', 'system'):
                self.assertEqual(confirmed[key], pending[key])
            self.assertEqual(confirmed['event']['at'], start)
            self.assertEqual(confirmed['event']['completed_at'], completion)
            self.assertEqual(confirmed['record']['owner'], employee['user'])
            self.assertEqual(confirmed['record']['roles'], [employee['role']])
            self.assertEqual(service.posts, 1)
            order, = service.rows()
            self.assertEqual(order['transaction_date'], '2027-01-09')
            self.assertEqual(order['delivery_date'], '2027-01-16')
            self.assertEqual(order['remarks'], employee['role'])
            self.assertIn(pending['retry_id'] + '-m' + str(completion), order['custom_norvale_operation'])
            if lost_reply:
                self.assertEqual(order['custom_norvale_operation'], committed_marker)
            clock = open_catalog(run, config, state)
            self.assertEqual([entry for entry in clock.history() if entry['id'] == 'warehouse-order'], [
                {'id': 'warehouse-order', 'phase': 'start', 'at': start},
                {'id': 'warehouse-order', 'phase': 'complete', 'at': completion + DAY}])

    def test_running_legacy_checkpoint_accepts_atomic_extension(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            run = root/'run'
            (run/'env').mkdir(parents=True)
            (run/'env/site-created').touch()
            (run/'env/controller-token.json').write_text('{"token":"offline"}')
            config = dict(company.DEFAULT_CONFIG, days=2)
            (run/'config.json').write_text(json.dumps(config))
            original_config = (run/'config.json').read_bytes()
            stages = ['service', 'arrivals', 'inspections', 'demand', 'fulfillment',
                      'production', 'replenishment', 'approvals', 'settlements', 'management']
            prior = {'day': 0, 'completed': ['seed', 'recurring-setup',
                                            *[f'd0000:{stage}' for stage in stages]],
                     'docs': {'existing': {'doctype': 'Sales Order', 'name': 'SO-original'}},
                     'physical': {'existing': 5}, 'decisions': {'existing': 'approve'}}
            (run/'state.json').write_text(json.dumps(prior))
            prior_bytes = (run/'state.json').read_bytes()
            registry = root/'registry.json'
            registry.write_text(json.dumps({'warehouse-sales': {
                'adapter': 'erpnext-sales', 'token_env': 'TEST_RECURRING_TOKEN',
                'employee': {'user': 'counter@warehouse.test', 'role': 'counter-sales'},
                'erp': {'url': 'http://warehouse.test', 'site': 'isolated.test',
                        'company': company.Engine.company, 'customer': 'Demo Retail',
                        'item': 'DEFAULT-SKU', 'warehouse': 'Finished Goods - NCC'},
            }}))
            addition = root/'extension.json'
            command = {
                'id': 'open-warehouse-sales',
                'systems': {'warehouse-sales': {'connection': 'warehouse-sales'}},
                'roles': {'counter-sales': {'permissions': ['write:warehouse-sales:order'],
                                            'shifts': [[780, 840]]}},
                'products': {'new-lotion': {'system': 'warehouse-sales', 'item': 'LOTION-2'}},
                'events': [{'id': 'new-order', 'kind': 'order', 'role': 'counter-sales',
                            'at': 1440+750, 'duration': 15,
                            'payload': {'product': 'new-lotion', 'case': 'PO-2',
                                        'quantity': 2, 'rate': 3}}],
            }
            service = FrappeFixture(root/'orders.json')
            service.user = 'counter@warehouse.test'
            entered, release = Event(), Event()
            errors, visited, projects = [], [], []
            clock = SimpleNamespace(current=datetime(2027, 1, 1, 12, tzinfo=timezone.utc))
            clock.now = lambda: clock.current
            clock.advance_to = lambda at: setattr(clock, 'current', at)

            class Stack:
                def __init__(self, path):
                    self.clock = clock

                def endpoints(self):
                    projects.append(self.project)
                    return {'erp': 'offline', 'site': 'offline'}

                def settle(self):
                    pass

            def client(url, site, token):
                self.assertEqual(url, 'http://warehouse.test')
                api = ErpClient(url, site, token)
                api.opener = service
                return api

            def phase(engine):
                visited.append((engine.event, clock.now()))
                if engine.event == 'd0001:demand':
                    entered.set()
                    if not release.wait(10):
                        raise TimeoutError('offline test did not release phase')

            def forbidden(*args):
                raise AssertionError('executed completed stage or contacted network')

            def run_controller():
                try:
                    generate.main(['--run', str(run)])
                except BaseException as exc:
                    errors.append(exc)

            def submit(payload):
                addition.write_text(json.dumps(payload))
                generate.main(['--run', str(run), '--extend', str(addition)])

            replacements = {stage: phase for stage in stages}
            replacements.update(seed=forbidden, recurring_setup=forbidden)
            with patch.multiple(company.Engine, **replacements), patch.object(company, 'Stack', Stack), \
                 patch.object(company, 'ErpClient', return_value=SimpleNamespace(login=lambda *a: None)), \
                 patch('native.incremental_erp.ErpClient', side_effect=client), \
                 patch('urllib.request.OpenerDirector.open', side_effect=forbidden), \
                 patch.dict(os.environ, NORVALE_CONNECTIONS_FILE=str(registry),
                            TEST_RECURRING_TOKEN='test-only'), \
                 redirect_stdout(io.StringIO()):
                saved_registry = registry.read_text()
                unbound = json.loads(saved_registry)
                del unbound['warehouse-sales']['employee']
                registry.write_text(json.dumps(unbound))
                with self.assertRaises(SystemExit):
                    submit(command)
                self.assertEqual(service.posts, 0)
                self.assertEqual((run/'state.json').read_bytes(), prior_bytes)
                registry.write_text(saved_registry)
                invalid = {**command, 'events': [{**command['events'][0], 'at': 100}]}
                with self.assertRaises(SystemExit):
                    submit(invalid)
                self.assertEqual((run/'state.json').read_bytes(), prior_bytes)
                worker = Thread(target=run_controller)
                worker.start()
                try:
                    self.assertTrue(entered.wait(10), errors)
                    with self.assertRaises(SystemExit):
                        submit({**command, 'events': [
                            command['events'][0], {**command['events'][0]}]})
                    self.assertEqual(service.posts, 0)
                    with self.assertRaises(SystemExit):
                        submit({**command, 'roles': {**command['roles'], 'visitor': command['roles']['counter-sales']},
                                'events': [{**command['events'][0], 'role': 'visitor'}]})
                    submit(command)
                    self.assertEqual(service.posts, 0)
                finally:
                    release.set()
                    worker.join(10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                submit(command)
                generate.main(['--run', str(run)])
                with self.assertRaises(SystemExit):
                    submit({**command, 'id': 'overlap'})

            self.assertEqual(projects, [company.project(run)] * 2)
            self.assertEqual((run/'config.json').read_bytes(), original_config)
            state = json.loads((run/'state.json').read_text())
            for section in ('docs', 'physical', 'decisions'):
                self.assertEqual(state[section], prior[section])
            self.assertEqual(state['completed'], prior['completed'] +
                             [f'd0001:{stage}' for stage in stages])
            self.assertEqual([key for key, _ in visited], [f'd0001:{stage}' for stage in stages])
            self.assertEqual(service.posts, 1)
            order, = service.rows()
            self.assertEqual(order['docstatus'], 0)
            self.assertEqual(order['owner'], 'counter@warehouse.test')
            self.assertEqual(order['items'][0]['item_code'], 'LOTION-2')
            self.assertEqual(order['remarks'], 'counter-sales')
            self.assertEqual(order['transaction_date'], '2027-01-02')
            from native.recurring_extensions import open_catalog
            catalog = open_catalog(run, config, state)
            self.assertEqual(catalog.history(), [
                {'id': 'new-order', 'phase': 'start', 'at': 1440+780},
                {'id': 'new-order', 'phase': 'complete', 'at': 1440+795}])
            self.assertEqual(catalog.export()['records'][0]['native_id'], order['name'])

    def test_frozen_paths_rejected_before_service_access(self):
        with TemporaryDirectory() as temp:
            redirect = Path(temp)/'frozen-link'
            redirect.symlink_to(company.ROOT/'data')
            for path in [redirect, company.RUN, company.ROOT/'data'/'new']:
                for flags in [[], ['--provision'], ['--extend', 'missing.json']]:
                    with self.subTest(path=path, flags=flags), self.assertRaises(SystemExit):
                        generate.main(['--run', str(path), *flags])
        help_text = subprocess.check_output(
            [sys.executable, 'native/company.py', '--help'], text=True)
        self.assertIn('--extend', help_text)
