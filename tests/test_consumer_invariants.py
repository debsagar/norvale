"""Offline consumer checks; durable engine operations are represented by doubles."""
import json
import unittest
from consumers import norvale, managed_services


class Provider:
    def __init__(self):
        self.calls = []
        self.action = 'approve'
    def call(self, tier, prompt, tools):
        self.calls.append((tier, json.loads(prompt)))
        return {'raw': json.dumps({'judgment': True} if tier == 'jev' else {'action': self.action})}


class Engine:
    def __init__(self):
        self.event = {'id': 'event', 'role': 'manager'}
        self.event_started_at = self.now = 10
        self.state, self.saved, self.rows, self.visible = {}, {}, [], []
        self.providers = Provider()
        self.fail_write = False
        self.observations = 0
    def observe(self, role, *, as_of):
        self.observations += 1
        return self.visible[:]
    def load_consumer_decision(self, key):
        return self.saved.get(key)
    def save_consumer_decision(self, key, value):
        return self.saved.setdefault(key, value)
    def write(self, system, kind, fields):
        if self.fail_write:
            self.fail_write = False
            raise RuntimeError('crash after decision')
        row = {'native_id': f'{system}-{len(self.rows)}', 'kind': kind, **fields}
        self.rows.append(row)
        return row


class Invariants(unittest.TestCase):
    def approval(self, engine, **kwargs):
        payload = {'case': 'C', 'amount': 10, 'quote': 'quote', 'open_commitments': 2, **kwargs}
        norvale.Consumer({}).handle(engine, 'approval_request', payload)

    def test_invalid_role_and_fields_never_observe_or_route(self):
        for role, changes in [(None, {}), ('operator', {}), ('manager', {'amount': float('nan')}),
                              ('manager', {'quote': ''}), ('manager', {'open_commitments': True})]:
            e = Engine(); e.event['role'] = role
            with self.subTest(role=role, changes=changes), self.assertRaises(ValueError):
                self.approval(e, **changes)
            self.assertEqual((e.observations, e.providers.calls, e.rows), (0, [], []))

    def test_resume_uses_saved_decision_and_evidence(self):
        e = Engine(); e.visible = [{'kind': 'order', 'value': 1}]; e.fail_write = True
        with self.assertRaises(RuntimeError): self.approval(e)
        e.visible[0]['value'] = 2
        e.providers.action = 'reject'
        self.approval(e)
        self.assertEqual(e.rows[0]['kind'], 'approval')
        self.assertEqual(len(e.providers.calls), 2)
        self.assertEqual(e.saved['event']['input']['observations'][0]['value'], 1)

    def test_reused_id_rejects_changed_request(self):
        e = Engine()
        self.approval(e)
        with self.assertRaises(ValueError):
            self.approval(e, case="different", amount=11)
        self.assertEqual(len(e.rows), 1)

    def test_cache_tracks_visible_orders_and_excludes_outputs(self):
        e = Engine()
        e.providers.call = lambda tier, prompt, tools: (e.providers.calls.append((tier, json.loads(prompt))) or {'raw': '{"judgment":false}'})
        config = {'policies': [{'scope': 'approval_request', 'effective_at': 0, 'review_at': 100, 'limit': 20, 'approver': 'director', 'evidence': ['authority']}]}
        c = managed_services.Consumer(config)
        payload = {'case': 'C', 'amount': 10, 'quote': 'q', 'open_commitments': 2}
        for i, rows in enumerate(([{'kind': 'order', 'quantity': 1}], [{'kind': 'order', 'quantity': 1}, {'kind': 'approval'}], [{'kind': 'order', 'quantity': 2}])):
            e.event['id'] = str(i); e.visible = rows; c.handle(e, 'approval_request', payload)
        self.assertEqual(len(e.providers.calls), 2)
        self.assertEqual(e.providers.calls[-1][1]['observations'], [{'kind': 'order', 'quantity': 2}])

    def test_review_links_and_invalid_config(self):
        e = Engine(); norvale.Consumer({}).handle(e, 'management_review', {'case': 'C'})
        self.assertEqual(e.rows[1]['review_id'], e.rows[0]['native_id'])
        self.assertEqual((e.rows[2]['review_id'], e.rows[2]['followup_id']), (e.rows[0]['native_id'], e.rows[1]['native_id']))
        for config in ({'production_minutes': -1, 'daily_capacity': 1, 'opening_material': 1}, {'production_minutes': 1, 'daily_capacity': 1, 'opening_material': 1}):
            e = Engine()
            with self.assertRaises((ValueError, RuntimeError)):
                norvale.Consumer(config).handle(e, 'order', {'case': 'C', 'quantity': 1, 'rate': 1})
            self.assertEqual(e.rows, [])

import tempfile
from pathlib import Path
from engine import Engine as Runtime
from test_engine_goals import Native, Providers, PowerLoss


class DurableInvariants(unittest.TestCase):
    def fixture(self, consumer, config, boundary=None):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        native, providers = Native(root / 'native.json'), Providers()
        def start(hook=boundary):
            return Runtime(root / 'run', consumer(config), seed=41, native=native,
                           providers=providers, monotonic=providers.monotonic, boundary=hook)
        return start(), native, providers, start

    def test_production_recovery_and_exact_links(self):
        def crash(stage, operation):
            if stage == 'after_send' and operation['action']['kind'] == 'production':
                raise PowerLoss()
        config = {'production_minutes': 10, 'daily_capacity': 5, 'opening_material': 5,
                  'shifts': {'operator': [[100, 200]]}}
        run, native, _, start = self.fixture(norvale.Consumer, config, crash)
        run.schedule('O', 'order', 201, payload={'case': 'C', 'quantity': 5, 'rate': 1})
        with self.assertRaises(PowerLoss): run.run(until=2000)
        run = start(None); run.run(until=2000)
        rows = native.records(); self.assertEqual([r['kind'] for r in rows], ['order', 'production'])
        self.assertEqual(rows[1]['order_id'], rows[0]['native_id'])
        self.assertEqual(rows[1]['started_at'], 1540)
        with self.assertRaisesRegex(ValueError, 'Duplicate event ID'):
            run.schedule('production:O', 'production', 2010, duration=10, role='operator',
                     payload={'case': 'C', 'order_id': rows[0]['native_id'], 'quantity': 5})
        run.run(until=4000)
        self.assertEqual(len(native.records()), 2)

    def test_capacity_and_bad_completion(self):
        config = {'production_minutes': 10, 'daily_capacity': 5, 'opening_material': 20,
                  'shifts': {'operator': [[100, 200]]}}
        run, native, _, _ = self.fixture(norvale.Consumer, config)
        run.schedule('A', 'order', 201, payload={'case': 'A', 'quantity': 3, 'rate': 1})
        run.schedule('B', 'order', 202, payload={'case': 'B', 'quantity': 3, 'rate': 1})
        with self.assertRaises(ValueError): run.run(until=202)
        self.assertEqual([r['case'] for r in native.records()], ['A'])

    def test_order_reserves_actual_staffed_day_before_native_write(self):
        cfg = dict(production_minutes=30, daily_capacity=1, opening_material=2,
                   roles={'operator':dict(staff=1,shifts=[[540,570]])})
        run, native, _, reopen = self.fixture(norvale.Consumer,cfg)
        for key in ('A','B'):
            run.schedule(key,'order',540,payload=dict(case=key,quantity=1,rate=1))
        run.run(until=550)
        run = reopen()
        run.run(until=2010)
        outputs = [(row['case'],row['started_at']) for row in native.records() if row['kind']=='production']
        self.assertEqual(outputs,[('A',540),('B',1980)])

    def test_early_completion_rejected(self):
        cfg = dict(production_minutes=10, daily_capacity=1, opening_material=1)
        run, native, _, _ = self.fixture(norvale.Consumer, cfg)
        run.schedule("A", "order", 100, payload=dict(case="A", quantity=1, rate=1))
        run.run(until=100)
        run.schedule("forged", "production", 100, role="operator", duration=0,
                     payload=dict(case="A", quantity=1, order_id=native.records()[0]["native_id"]))
        with self.assertRaises(ValueError):
            run.run(until=100)
        self.assertEqual([r["kind"] for r in native.records()], ["order"])

    def test_fractional_orders_fit_exactly(self):
        cfg = dict(production_minutes=10, daily_capacity=0.3, opening_material=0.3)
        run, native, _, _ = self.fixture(norvale.Consumer, cfg)
        for case, qty in [("A", 0.1), ("B", 0.2)]:
            run.schedule(case, "order", 100, payload=dict(case=case, quantity=qty, rate=1))
        run.run(until=110)
        self.assertEqual([r["quantity"] for r in native.records() if r["kind"] == "production"], [0.1, 0.2])

    def test_cross_company_role_and_approval_resume(self):
        for consumer in (norvale.Consumer, managed_services.Consumer):
            with self.subTest(consumer=consumer):
                def crash(stage, operation):
                    if stage == 'after_send': raise PowerLoss()
                run, native, providers, start = self.fixture(consumer, {}, crash)
                payload = {'case': 'C', 'amount': 10, 'quote': 'q', 'open_commitments': 2, 'role': 'manager'}
                run.schedule('D', 'approval_request', 0, payload=payload, role='operator')
                with self.assertRaises(ValueError): run.run(until=0)
                self.assertFalse(native.records()); self.assertFalse(providers.calls)
                # A separate run avoids the deliberately rejected queued event.
                run, native, providers, start = self.fixture(consumer, {}, crash)
                run.schedule('D', 'approval_request', 0, payload=payload, role='manager')
                with self.assertRaises(PowerLoss): run.run(until=0)
                providers.replies['model'] = '{"action":"reject"}'
                run = start(None); run.run(until=0)
                self.assertEqual([r['kind'] for r in native.records()], ['approval'])
                self.assertEqual(len(providers.calls), 2)
