"""Role, source, cutoff and freshness checks using a private Delta archive."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from integrations.company_run.planning_report import FIELDS, OWNER, publish, snapshot


class PlanningReportTests(unittest.TestCase):
    def test_native_scope_and_scan_fail_closed(self):
        from boundary import ingest
        dt = '2027-01-01 08:00:00'
        visible = dict(zip(FIELDS, ['WO-1', dt, dt, dt, None, None,
                                  'In Process', 'SHAMPOO', 10, 3, 'Nos']))
        hidden = dict(visible, name='WO-2', production_item='OTHER-SCOPE')
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)/'lake'
            archived = {k: v for k, v in visible.items() if v is not None}
            ingest(root, 'Work Order', lambda: [archived, hidden], source='test-erp')
            api = Mock()
            api.list.return_value = [visible]
            cutoff = datetime(2027, 1, 1, 12, tzinfo=timezone.utc)
            read = lambda: snapshot(api, {'source': 'test-erp'}, root, 'NCC', cutoff)
            report = read()
            self.assertEqual(report['freshness'], 'FRESH')
            self.assertEqual(report['open_work_orders'], {'In Process': 1})
            self.assertEqual(report['work_order_versions'], {'WO-1': dt})
            self.assertEqual(report['remaining_by_item'], [dict(item='SHAMPOO', uom='Nos', quantity=7)])
            self.assertIn(['company', '=', 'NCC'], api.list.call_args.args[1])
            self.assertIn(['planned_start_date', '<=', '2027-01-01 12:00:00'], api.list.call_args.args[1])
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)

            for row in (dict(visible, qty=99), dict(visible, actual_end_date='2027-01-02 08:00:00'),
                        {k: v for k, v in visible.items() if k != 'qty'}):
                api.list.return_value = [row]
                stale = read()
                self.assertEqual(stale['freshness'], 'STALE')
                self.assertNotIn('open_work_orders', stale)
                self.assertEqual(stale['last_successful_scan_utc'], report['last_successful_scan_utc'])

            def outage():
                raise OSError('private detail')
            with self.assertRaises(OSError):
                ingest(root, 'Work Order', outage, source='test-erp')
            stale = read()
            self.assertEqual(stale['freshness'], 'STALE')
            self.assertNotIn('private detail', json.dumps(stale))
            self.assertEqual(stale['last_successful_scan_utc'], report['last_successful_scan_utc'])

            ingest(root, 'Work Order', lambda: [visible], source='test-erp')
            state_path = root/'state.json'
            state = json.loads(state_path.read_text())
            next(iter(state.values()))['last_success'] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
            state_path.write_text(json.dumps(state))
            self.assertEqual(read()['freshness'], 'STALE')

    def test_unavailable_reader_marks_only_its_entity_stale_on_the_same_pass(self):
        from boundary import ingest
        from integrations.company_run.lake import scan
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = 'erpnext'
            ingest(root, 'Work Order', lambda: [], source=source)
            planner = Mock()
            planner.list.return_value = []
            cutoff = datetime.now(timezone.utc)
            report = lambda: snapshot(planner, {'source': source}, root, 'NCC', cutoff)
            first = report()
            self.assertEqual(first['freshness'], 'FRESH')
            reader = dict(url='http://localhost', api_key='reader', api_secret='fixture')
            config = dict(source=source, reader=reader, entities=['Work Order', 'Purchase Order'],
                          entity_readers={'Work Order': {}})
            with patch('integrations.lake.boundary.Frappe') as client:
                client.return_value.documents.return_value = []
                with self.assertRaises(ExceptionGroup):
                    scan('erp', config, root)
                client.return_value.documents.assert_called_once_with('Purchase Order')
                stale = report()
                self.assertEqual(stale['freshness'], 'STALE')
                self.assertEqual(stale['last_successful_scan_utc'], first['last_successful_scan_utc'])
                self.assertNotIn('open_work_orders', stale)
                del config['entity_readers']
                scan('erp', config, root)
            self.assertEqual(report()['freshness'], 'FRESH')

    def test_changed_order_between_queries_uses_complete_final_result(self):
        from integrations.company_run.planning_report import visible_backlog
        stamp = '2027-01-01 08:00:00'
        rows = [dict(zip(FIELDS, [str(i), stamp, stamp, stamp, None, None,
                                  'In Process', 'Item', 3, 0, 'Nos'])) for i in range(129)]
        api = Mock()
        api.list.side_effect = [rows[:128], [dict(rows[0], status='Completed'), *rows[1:]]]
        visible = list(visible_backlog(api, 'NCC', datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)))
        self.assertEqual([r['name'] for r in visible], [r['name'] for r in rows[1:]])
        self.assertEqual([c.kwargs['limit'] for c in api.list.call_args_list], [128, 256])

    def test_publication_retry_after_lost_reply_and_missed_post(self):
        from integrations.company_run.planning_report import reconcile
        from unittest.mock import patch
        for committed in (True, False):
            with self.subTest(committed=committed), tempfile.TemporaryDirectory() as path:
                root = Path(path)
                binding = root/'binding.json'
                settings = root/'settings.json'
                binding.write_text(json.dumps(dict(settings=str(settings), archive=str(root))))
                settings.write_text(json.dumps(dict(erp=dict(reader=dict(url='erp',site='erp'), source='erp'))))
                engine = Mock()
                engine.api = {'PLANNER': Mock(base='erp', site='erp')}
                engine.api['PLANNER'].request.return_value = OWNER
                engine.company = 'Company'
                day = '2027-01-02'
                engine.state = {'pending_planning_reports': [day]}
                stored = {}
                def create(key, kind, data, role):
                    if committed or engine.date() != day:
                        stored[key] = dict(data, name='note', owner=OWNER)
                    if engine.date() == day:
                        raise TimeoutError()
                    return stored[key]
                engine.write.side_effect = create
                engine.recover_company_doc.side_effect = lambda key, *args: stored.get(key)
                report = dict(source='erp', company='Company', as_of=day, freshness='FRESH')
                with patch('integrations.company_run.planning_report.snapshot', return_value=report):
                    for current in (day, '2027-01-03'):
                        engine.date.return_value = current
                        engine.stack.clock.now.return_value = datetime.fromisoformat(current)
                        reconcile(engine, binding)
                        self.assertEqual(engine.state['pending_planning_reports'], [day] if current == day else [])
                self.assertEqual(len(stored), 1)
                content = next(iter(stored.values()))['content']
                self.assertIn('FRESH' if committed else 'STALE', content)
                self.assertEqual(engine.write.call_count, 1 if committed else 2)

    def test_principal_and_site_are_bound_before_publish(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            binding = root/'binding.json'
            settings = root/'analytics.json'
            binding.write_text(json.dumps(dict(settings=str(settings), archive=str(root/'lake'))))
            settings.write_text(json.dumps(dict(erp=dict(reader=dict(url='http://erp', site='erp'),
                entity_readers={'Work Order': dict(url='http://erp', site='other-site')}))))
            engine = Mock()
            api = Mock(base='http://erp', site='erp')
            engine.api = {'PLANNER': api}
            for identity in ('buyer@norvale.test', OWNER):
                api.request.return_value = identity
                with self.assertRaises(PermissionError):
                    publish(engine, binding)
                engine.write.assert_not_called()

    def test_report_failure_leaves_management_work_running(self):
        from unittest.mock import patch
        from native.company import Engine

        with tempfile.TemporaryDirectory() as path:
            engine = Mock(run=Path(path), day=1, config={'report_every': 2})
            engine.state = {'decisions': {}}
            engine.docs = {}
            engine.date.return_value = '2027-01-02'
            engine.observe.return_value = []
            # A checkpoint with no binding follows the original management path.
            Engine.management(engine)
            self.assertEqual([call.args[1] for call in engine.write.call_args_list], ['Journal Entry', 'Note'])
            engine.write.reset_mock()
            binding = engine.run/'env/planning-report.json'
            binding.parent.mkdir()
            binding.touch()
            with patch('integrations.company_run.planning_report.publish', side_effect=OSError):
                Engine.management(engine)
            engine.log.assert_called_once_with('planning_report_failed', error='OSError')
            self.assertEqual([call.args[1] for call in engine.write.call_args_list], ['Journal Entry', 'Note'])

    def test_generate_publishes_through_the_recurring_phase_and_resumes(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import generate
        from company import Engine

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            env = root/'env'
            env.mkdir()
            (env/'planning-report.json').write_text(json.dumps(dict(settings=str(env/'analytics.json'),
                                                               archive=str(root/'lake'))))
            (env/'analytics.json').write_text(json.dumps(dict(erp=dict(source='test-erp',
                reader=dict(url='http://erp', site='erp')))))
            api = Mock(base='http://erp', site='erp')
            api.request.return_value = OWNER
            cutoff = datetime(2027, 1, 1, 12, tzinfo=timezone.utc)
            e = Engine.__new__(Engine)
            e.run, e.day = root, 0
            e.config = {'days': 1, 'report_every': 1}
            phases = [key for _, key, _ in e.events()]
            e.state = dict(day=0, docs={}, completed=phases[:-1], decisions={})
            e.docs = e.state['docs']
            e.persist()
            e.api = {'PLANNER': api}
            e.log, e.advance_clock, e.run_additions = Mock(), Mock(), Mock()
            e.stack = SimpleNamespace(clock=SimpleNamespace(now=lambda: cutoff))
            e.observe = Mock(return_value=[])
            e.write = Mock(side_effect=lambda key, doctype, body, role, *args:
                           dict(body, name=key, owner=OWNER))
            with patch('company.Engine', return_value=e):
                generate.main(['--run', path])
                generate.main(['--run', path])
            planner_calls = [call.args for call in e.write.call_args_list if call.args[3] == 'PLANNER']
            self.assertEqual(len(planner_calls), 1)
            key, doctype, note, _ = planner_calls[0]
            self.assertEqual((key, doctype, note['public']), ('planning-backlog:2027-01-01', 'Note', 0))
            self.assertIn('"freshness": "STALE"', note['content'])
            self.assertEqual(json.loads((root/'state.json').read_text())['completed'], phases)
