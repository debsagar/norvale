"""Offline entry-point replay with independent MES and QMS readbacks."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engine import DAY
from integrations.company_run import __main__ as app, timing
from integrations.company_run.demo import Demo
from integrations.company_run.maintenance import MaintenanceAuthority
from integrations.mes.client import CENTER, EVENT, MO, WO, Mes as MesClient
from integrations.qms.adapter import CASE, INSPECTION, OP
import native.company as company
from native.company import DEFAULT_CONFIG, Engine, save
from native.company_erp import CompanyERP, Pending
from native.recurring_extensions import open_catalog


class TimedCompany(unittest.TestCase):
    def test_partial_consumption_preserves_pinned_lots_and_native_units(self):
        actor = Demo.__new__(Demo)
        units = {'Nos': {'must_be_whole_number': 1}, 'Kg': {'must_be_whole_number': 0}}
        actor.e = SimpleNamespace(api={'PRODUCTION': SimpleNamespace(get=lambda _, unit: units[unit])})
        work = dict(qty=108, revision={}, inputs=[
            dict(item='bottle', qty=85, uom='Nos'), dict(item='bottle', qty=23, uom='Nos'),
            dict(item='oil', qty=0.135, uom='Kg')])
        rows = [dict(lot_id=i, quantity=row['qty']) for i, row in enumerate(work['inputs'], 1)]
        qty = actor.output_quantity(work)
        self.assertEqual(qty, 54)
        self.assertEqual(actor.allocate_consumption(work, rows, qty),
                         [dict(lot_id=1, quantity=54), dict(lot_id=3, quantity=0.0675)])
        rows[0]['quantity'], rows[1]['quantity'] = 23, 85
        self.assertEqual(actor.allocate_consumption(work, rows, qty)[:2],
                         [dict(lot_id=1, quantity=23), dict(lot_id=2, quantity=31)])
        work['inputs'].append(dict(item='carton', qty=1, uom='Nos'))
        self.assertEqual(actor.output_quantity(work), 108)

    def run_company(self, run, interrupted, *, operating=False, outage=False, held_wip=False):
        works = {}
        for i, item in enumerate(('lotion', 'shampoo')):
            key = item + ':' + 'order-and-revision-' * 9
            works[key] = dict(key=key, item=item, revision={'id':'fixture-revision'}, uom='Nos', qty=(30 if operating else 60) * (i + 1), equipment=i + 1,
                              inputs=[dict(batch='RAW', item='oil', uom='Kg', qty=8, disposition='v1')],
                              request=dict(key='full-dispatch-key:' + key, work_order='wo-' + item))
        if held_wip:
            for work in works.values():
                work['equipment'] = 1
                work['revision'].update(inspection_spec='released-spec', erp_bom='BOM')
                work['request']['released_on'] = '2027-01-01'
        cfg = dict(DEFAULT_CONFIG, days=1 if outage or held_wip else 3, company_authority='integrations.company_run.authority:Authority',
                   company_timing=dict(shifts={'PRODUCTION': [[600, 630], [660, 700]],
                                               'LAB': [[620, 640], [690, 700]]}, lab_minutes=30))
        if operating:
            cfg['company_operating'] = True
        save(run/'config.json', cfg)
        save(run/'env/controller-token.json', {'token': 'offline'})
        refs = {batch: batch for batch in ('RAW', 'lot-lotion', 'lot-shampoo')}
        save(run/'env/company-sources.json', dict(mes={'source_id':'operator-source'}, operator={}, company_id=1,
                                                lab=dict(references=refs)))
        (run/'env/site-created').touch()
        (run/'ticket-check.json').touch()
        raw = dict(item='oil', batch='RAW', kind='raw')
        save(run/'state.json', dict(day=0, docs={}, physical={}, decisions={},
                                   completed=['seed', 'recurring-setup'],
                                   acceptance_assays={'RAW': dict(state='Released')},
                                   ops=dict(maintenance={}, orders={'L': {'item': 'lotion'}, 'S': {'item': 'shampoo'}}),
                                   company_erp=dict(work=works, completions={}, shipments={}, moves={}, lots={'RAW': raw})))
        native = dict.fromkeys((1, 2), 'confirmed')
        output = {}
        labor_calls = []
        checked = []
        ledger = {}
        observed = []
        failures = ['start', 'complete'] if interrupted else []
        current = {}
        wall = SimpleNamespace(now=None)
        if interrupted and not operating:
            (run/'interrupt-after-release').touch()

        def record(stage, key):
            e = current['engine']
            observed.append((stage, key, e.additions.now, e.date(), e.event))
            # The service clock can advance substantially while an API call is in flight.
            if interrupted:
                wall.now += timedelta(minutes=5)

        def cut(stage):
            if failures and failures[0] == stage:
                failures.pop(0)
                raise TimeoutError(stage + ' accepted before readback')

        fault = {}
        inspection = {}
        comments = []
        ticket = dict(number=1, html_url='https://tickets.invalid/issues/1', created_at='2027-01-01')

        class Mes(MesClient):
            source_id = 'operator-source'
            url = 'https://mes.invalid'

            def call(self, model, method, *args, **kwargs):
                if (model, method) == (EVENT, 'search'):
                    return [9] if fault and not fault['date_end'] else []
                if (model, method) == (WO, 'search_count'):
                    self_test.assertEqual(args[0], [['workcenter_id', '=', 1], ['state', '=', 'progress']])
                    return sum(state == 'progress' for state in native.values())
                if (model, method) == (CENTER, 'norvale_restart'):
                    self_test.assertEqual(args[:2], ([1], 9))
                    fault.update(date_end='2027-01-02', description=f'Fault observed\nVerified restart {args[2]}')
                    record('restart', 1)
                    return 9
                if (model, method) == (MO, 'norvale_reject_interrupted'):
                    self_test.assertEqual(args[:2], ([1], 9))
                    self_test.assertTrue(fault['date_end'])
                    if native[1] == 'progress':
                        fault['norvale_disposition'] = args[2]
                        native[1] = 'cancel'
                        record('cancel', 1)
                        raise SystemExit('MES cancelled before controller readback')
                    self_test.assertEqual(native[1], 'cancel')
                    self_test.assertEqual(fault['norvale_disposition'], args[2])
                    return 1
                raise AssertionError((model, method, args))

            def __init__(self, **kwargs):
                pass

            def read(self, model, mo):
                if outage:
                    minute = current['engine'].additions.now
                    if mo == 2 and minute < 2 * DAY:
                        raise TimeoutError('start preflight unavailable')
                    if mo == 1 and native[mo] == 'progress' and minute < 2 * DAY and failures == ['complete']:
                        raise TimeoutError('start committed but readback is unavailable')
                if held_wip:
                    if model == CENTER:
                        return dict(working_state='blocked' if fault and not fault['date_end'] else 'normal')
                    if model == EVENT:
                        return dict(fault)
                    if model == WO:
                        return dict(state='progress' if native[mo] == 'progress' else 'ready',
                                    production_id=[mo, 'MO'], workcenter_id=[1, 'center'])
                    if model == 'product.product':
                        return dict(default_code='lotion')
                    return dict(id=mo, state=native[mo], workorder_ids=[mo], origin='wo-lotion',
                                product_id=[100, 'lotion'], product_qty=30, product_uom_id=[1, 'Unit'])
                if model == 'mrp.workorder':
                    return {'state': 'ready'}
                if operating:
                    return {'state': native[mo], 'workorder_ids': [mo]}
                return {'state': native[mo], 'workorder_ids': [mo]}

            def start(self, mo, rows, released):
                self_test.assertEqual(released, [10])
                if held_wip:
                    self_test.assertTrue(self.availability(1)['available'])
                if native[mo] == 'confirmed':
                    record('start', mo)
                    native[mo] = 'progress'
                    cut('start')
                self_test.assertEqual(native[mo], 'progress')

            def complete(self, mo, lot, qty, rows, released, *, labor=None):
                if operating:
                    persisted = json.loads((run/'state.json').read_text())['company_erp']['work']
                    expected, = [w['scheduled_labor'] for w in persisted.values()
                                 if w['dispatch']['id'] == str(mo) and 'scheduled_labor' in w]
                    self_test.assertEqual(labor, expected)
                    offset = 2 * DAY if outage else 0
                    expected = [[600 + offset, 624 + offset]] if mo == 1 else [
                        [600 + offset, 630 + offset], [660 + offset, 678 + offset]]
                    if not held_wip:
                        self_test.assertEqual(labor['intervals'], expected)
                    self_test.assertEqual(labor['minutes'], 24 if mo == 1 else 48)
                    labor_calls.append((mo, labor))
                else:
                    self_test.assertIsNone(labor)
                if native[mo] == 'progress':
                    self_test.assertEqual(released, [10])
                    record('complete', mo)
                    native[mo] = 'done'
                    output[mo] = lot, qty, rows
                    cut('complete')
                self_test.assertEqual(output[mo], (lot, qty, rows))

        class Authority(MaintenanceAuthority):
            def settings(self):
                return dict(operator={}, equipment_centers=[1], units={'Unit': 'Nos'},
                            restart_verifier='verifier', maintenance_ticket_reader='fixture',
                            qms_reader={'source': 'offline'}, qms_approver='qa',
                            specifications={'lotion': 'current-spec'})

            def operator(self, role):
                return Mes()

            def maintenance_mes(self, role):
                self_test.assertEqual(role, 'MAINTENANCE')
                return Mes()

            def tickets(self, role, *, token=None):
                self_test.assertEqual(role, 'MAINTENANCE')
                return SimpleNamespace(open=lambda *a, **kw: ticket, close=lambda *a, **kw: None,
                                       comments=lambda number: deepcopy(comments))

            def completions(self, ack, *, as_of, role):
                mo = int(ack['id'])
                if native[mo] != 'done':
                    return []
                work = next(w for w in works.values() if w['item'] == 'shampoo')
                lot, qty, inputs = output[mo]
                return [dict(id=f'{Mes.source_id}:mrp.production:{mo}', version='done',
                             dispatch_id=str(mo), work_order=work['request']['work_order'],
                             revision=work['revision'], source=Mes.source_id, authorized=True,
                             inputs=[dict(batch='RAW', item='oil', uom='Kg', qty=inputs[0]['quantity'])],
                             output=dict(lot_id=str(lot), item='shampoo', uom='Nos', qty=qty))]

        def qms_list(doctype, filters, **kwargs):
            if not inspection:
                return []
            if doctype == CASE:
                self_test.assertEqual(filters, dict(custom_qms_batch=inspection['batch_no'],
                                                    custom_failed_inspection=inspection['name']))
                return [dict(name='NC-1')]
            self_test.assertEqual((doctype, filters), (INSPECTION, {'batch_no': inspection['batch_no']}))
            return [{k: inspection[k] for k in ('name', 'modified', 'creation')}]

        def erp_post(e, key, doctype, fields, role, submit, **kwargs):
            self_test.assertEqual((doctype, role, submit), ('Stock Entry', 'PRODUCTION', True))
            self_test.assertNotIn(key, ledger)
            draft = dict(fields, name='MAT-STE-1', doctype=doctype, docstatus=0)
            kwargs['before_submit'](draft)
            doc = dict(draft, docstatus=1)
            ledger[key] = doc
            record('manufacture', fields['work_order'])
            return doc

        def create_flow(e, _):
            current['engine'] = e
            if held_wip:
                return CompanyERP(e, Authority())
            flow = CompanyERP(e, SimpleNamespace())
            flow.equipment.guard_execution = lambda identity: checked.append(identity)
            flow.equipment.available = lambda center, role, **kw: None
            return flow

        def dispatch(flow, work):
            mo = ('lotion', 'shampoo').index(work['item']) + 1 if held_wip else work['equipment']
            if not work.get('dispatch'):
                work['dispatch'] = dict(source=Mes.source_id, id=str(mo))
                flow.e.persist()

        def disposition(flow, lot, role):
            # A later authorization can never be treated as the pinned one.
            return 'revoked' if lot['batch'] == 'RAW' and flow.e.additions.now == 678 else 'v1'

        def collect(flow, work):
            mo = work['equipment']
            if native[mo] == 'done':
                work['produced'] = output[mo][1]
                batch = 'lot-' + work['item']
                flow.state['lots'][batch] = dict(kind='finished', batch=batch, item=work['item'], work_order=work['request']['work_order'])
                flow.e.persist()

        def sync(flow):
            for batch, assay in flow.e.state['acceptance_assays'].items():
                if batch != 'RAW' and assay['state'] == 'Released' and batch not in ledger:
                    record('release-post', batch)
                    ledger[batch] = assay['id']

        def inspect(cfg, lot, day, *, failed=False, prior=None):
            record('lab-result', lot['batch'])
            return dict(name=lot['batch'] + (':retest' if prior else ':initial'),
                        workflow_state='Rework Authorized' if failed else 'Released')

        def service(e):
            record('service', e.day)

        def phase_production(e):
            e.company_erp.production([])

        def phase_inspections(e):
            e.company_erp.inspections()

        def fake_advance(e, target):
            wall.now = max(wall.now, target) if wall.now else target
            if held_wip and native[1] == 'progress' and not fault:
                # An equipment outage arrives from MES between scheduled start and finish.
                fault.update(workcenter_id=[1, 'center'], norvale_interrupted_id=[1, 'WO'],
                             description='Fault observed', loss_type='availability', date_end=False,
                             norvale_disposition=False)
                record('fault', 1)

        def invoke(through):
            with patch.object(sys, 'argv', ['company_run', str(run), '--through', str(through)]):
                app.main()

        def forbidden(*args, **kwargs):
            raise AssertionError('Unexpected network call')

        self_test = self
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {'company': company}))
            for owner, name, impl in [
                (Engine, 'make_company_erp', create_flow),
                (Engine, 'advance_clock', fake_advance),
                (Engine, 'service', service),
                (Engine, 'get', lambda *args: {'items': [{'item_code': 'oil'}]}),
                (Engine, 'plan_production' if operating else 'production', phase_production),
                (Engine, 'inspections', phase_inspections),
                (CompanyERP, 'definition', lambda flow, item: ({}, {'oil': ('Kg', 1)})),
                (CompanyERP, 'dispatch', dispatch),
                (CompanyERP, 'disposition', disposition),
                (CompanyERP, 'collect', collect),
                (CompanyERP, 'poll_work', lambda *args, **kw: None),
                (CompanyERP, 'inspections', sync),
                (Demo, 'publish', lambda *args: None),
                (Demo, 'product', lambda *args: 100),
                (Demo, 'unique', lambda _, model, domain, values: 10 if values['name'] == 'RAW' else int(values['name'].split('-')[-1])),
                (Demo, 'receive_mes_lot', lambda *args: None),
            ]:
                if held_wip and name in ('service', 'collect', 'poll_work'):
                    continue
                stack.enter_context(patch.object(owner, name, impl))
            for name in ('arrivals', 'demand', 'fulfillment', 'replenishment', 'approvals', 'settlements', 'management'):
                stack.enter_context(patch.object(Engine, name, lambda _: None))
            for path, replacement in {
                'native.company.Stack': lambda _: SimpleNamespace(endpoints=lambda: {'erp': '', 'site': ''}),
                'native.company.ErpClient': lambda *a: SimpleNamespace(login=lambda *a: None),
                'integrations.company_run.demo.Mes': Mes,
                'integrations.company_run.demo.inspect': inspect,
                'urllib.request.OpenerDirector.open': forbidden,
                'socket.create_connection': forbidden,
            }.items():
                stack.enter_context(patch(path, replacement))
            if held_wip:
                for owner, name, impl in [
                    (Engine, 'write', erp_post),
                    (Engine, 'recover_company_doc', lambda _, key, *a: ledger.get(key)),
                    (CompanyERP, 'recover_dispatch', lambda *a: None),
                    (CompanyERP, 'batch_row', lambda _, fields, batch: dict(fields, batch_no=batch)),
                    (CompanyERP, 'batch', lambda _, identity, item: 'lot-' + item),
                    (CompanyERP, 'balance', lambda *a: 8),
                    (CompanyERP, 'verify_row', lambda *a: None),
                ]:
                    stack.enter_context(patch.object(owner, name, impl))
                stack.enter_context(patch('integrations.company_run.maintenance.QMS', lambda **kw:
                    SimpleNamespace(list=qms_list, get=lambda *a: dict(inspection))))
            if operating:
                stack.enter_context(patch('integrations.company_run.operating.Operating.definitions', lambda _: None))
                stack.enter_context(patch.object(Engine, 'bank_statements', lambda _: None))
                stack.enter_context(patch.object(sys, 'path', sys.path[:]))
                from generate import main as recurring
                stack.enter_context(patch('integrations.company_run.timing.queue_samples', lambda *args: None))
                stack.enter_context(patch('time.sleep', lambda _: None))
                stack.enter_context(redirect_stdout(StringIO()))
                recurring(['--run', str(run)])
                if held_wip:
                    self.assertEqual(native, {1: 'progress', 2: 'confirmed'})
                    self.assertFalse(Mes().availability(1)['available'])
                    recurring(['--run', str(run), '--days', '2'])
                    pending = json.loads((run/'state.json').read_text())
                    case, = pending['ops']['equipment_faults'].values()
                    batch = case['interrupted']
                    body = (f'Verified restart: {case["fault"]["source_url"]}\n'
                            'Diagnosis: vibration\nRepair: bracket tightened\nTest: vibration\nResult: PASS')
                    comments.append(dict(id=1, user={'login': 'controller'}, body=body,
                                         created_at='2027-01-02', updated_at='2027-01-02'))
                    recurring(['--run', str(run), '--days', '3'])
                    self.assertFalse(fault['date_end'])
                    comments[0].update(id=2, user={'login': 'verifier'})
                    inspection.update(name='QI-1', modified='v1', creation='2027-01-04',
                                      report_date='2027-01-05', docstatus=1, status='Rejected',
                                      workflow_state='Lot Rejected', modified_by='qa', batch_no=batch['batch'],
                                      item_code='lotion', custom_spec_revision='released-spec',
                                      custom_lot_uom='Nos', custom_lot_quantity=30,
                                      custom_decision_rationale=f'Failed assay for {batch["completion_ref"]}',
                                      readings=[dict(numeric=1, specification='viscosity', min_value=1,
                                                     max_value=2, reading_1=3, status='Rejected')],
                                      **{OP: batch['batch'] + ':interrupted'})
                    recurring(['--run', str(run), '--days', '4'])
                    pending = json.loads((run/'state.json').read_text())
                    case, = pending['ops']['equipment_faults'].values()
                    clock = open_catalog(run, cfg, pending)
                    self.assertEqual(case['verification']['actor'], 'verifier')
                    self.assertTrue(case['restart']['date_end'])
                    self.assertNotIn('disposition', case)
                    self.assertEqual(Mes().availability(1)['working_state'], 'normal')
                    self.assertFalse(Mes().availability(1)['available'])
                    with self.assertRaisesRegex(Pending, 'blocked or occupied'):
                        Authority().require_equipment(1, role='PRODUCTION')
                    self.assertEqual(native, {1: 'progress', 2: 'confirmed'})
                    slot = clock.state['company_running']['1']
                    self.assertEqual(slot['owner'][2], '1')
                    self.assertEqual([entry[0][2] for entry in slot['waiting']], ['2'])
                    self.assertFalse(labor_calls)
                    self.assertFalse(ledger)
                    inspection['custom_spec_revision'] = 'current-spec'
                    recurring(['--run', str(run), '--days', '5'])
                    self.assertEqual(native, {1: 'progress', 2: 'confirmed'})
                    inspection['custom_spec_revision'] = 'released-spec'
                    with self.assertRaisesRegex(SystemExit, 'cancelled before controller readback'):
                        recurring(['--run', str(run), '--days', '6'])
                    pending = json.loads((run/'state.json').read_text())
                    case, = pending['ops']['equipment_faults'].values()
                    self.assertEqual(case['qa_evidence']['id'], 'QI-1')
                    self.assertNotIn('disposition', case)
                    self.assertEqual(native, {1: 'cancel', 2: 'confirmed'})
                    self.assertTrue(Mes().availability(1)['available'])
                    recurring(['--run', str(run)])
                    recurring(['--run', str(run)])
                    final = json.loads((run/'state.json').read_text())
                    case, = final['ops']['equipment_faults'].values()
                    self.assertEqual(case['qa_as_of'], '2027-01-06')
                    self.assertEqual(case['qa_evidence']['id'], 'QI-1')
                    rejected, = [w for w in final['company_erp']['work'].values() if w['item'] == 'lotion']
                    self.assertEqual(rejected['rejected_remainder'], 30)
                    self.assertFalse(rejected.get('mes_execution_confirmed'))
                    self.assertFalse(rejected.get('produced'))
                    self.assertNotIn('mes_consumption', rejected)
                    self.assertEqual(native, {1: 'cancel', 2: 'done'})
                    self.assertEqual([mo for mo, _ in labor_calls], [2])
                    self.assertEqual([doc['work_order'] for doc in ledger.values()], ['wo-shampoo'])
                    self.assertEqual([(stage, key) for stage, key, *_ in observed if stage in
                                      ('start', 'fault', 'restart', 'cancel', 'complete', 'manufacture')],
                                     [('start', 1), ('fault', 1), ('restart', 1), ('cancel', 1),
                                      ('start', 2), ('complete', 2), ('manufacture', 'wo-shampoo')])
                    self.assertFalse(open_catalog(run, cfg, final).state['company_running'])
                    return
                if outage:
                    pending = json.loads((run/'state.json').read_text())
                    by_center = {w['equipment']: w for w in pending['company_erp']['work'].values()}
                    self.assertEqual(native, {1: 'progress', 2: 'confirmed'})
                    self.assertIn('mes_start', by_center[1])
                    self.assertNotIn('mes_start', by_center[2])
                    self.assertTrue(all('scheduled_labor' not in w for w in by_center.values()))
                    clock = open_catalog(run, cfg, pending)
                    self.assertEqual(set(clock.state['company_running']), {'1'})

                    # A competing dispatch arrives during the outage. The fixture has
                    # no execution endpoint for it, so a native call would fail the test.
                    contender = deepcopy(by_center[1])
                    contender.pop('mes_start')
                    contender.update(key='contender', dispatch=dict(source=Mes.source_id, id='3'),
                                     request=dict(key='competing-dispatch', work_order='wo-contender'))
                    pending['company_erp']['work']['contender'] = contender
                    save(run/'state.json', pending)
                    job = ['mes', Mes.source_id, '3', 'competing-dispatch', 'contender']
                    timing.schedule(clock, job, 'start', DAY + 600, role='PRODUCTION')
                    recurring(['--run', str(run), '--days', '2'])

                    pending = json.loads((run/'state.json').read_text())
                    clock = open_catalog(run, cfg, pending)
                    waiting = clock.state['company_running']['1']['waiting']
                    self.assertEqual(waiting, [[job, 0, None]])
                    pending['company_erp']['work']['contender']['rejected_remainder'] = 1
                    save(run/'state.json', pending)
                    recurring(['--run', str(run), '--days', '3'])
                    resumed = open_catalog(run, cfg, pending)
                    wake = 'company:' + json.dumps([job, 'start', 1], separators=(',', ':'))
                    self.assertTrue(resumed.has_event(wake))
                    self.assertFalse(resumed.state['company_running'])
                recurring(['--run', str(run)])
                self.assertEqual(native, {1: 'done', 2: 'done'})
                self.assertEqual([mo for mo, _ in labor_calls], [1, 1, 2] if interrupted else [1, 2])
                if interrupted:
                    self.assertEqual(labor_calls[0], labor_calls[1])
            elif interrupted:
                for message in ('start', 'complete'):
                    with self.assertRaisesRegex(TimeoutError, message + ' accepted'):
                        invoke(0)
                with self.assertRaisesRegex(InterruptedError, 'before ERP sync'):
                    invoke(1)
                cutover = json.loads((run/'release-interruption.json').read_text())
                self.assertNotIn(cutover['batch'], ledger)
            else:
                invoke(0)
                self.assertEqual(native, {1: 'done', 2: 'progress'})
                self.assertFalse(ledger)
            if not operating:
                invoke(2)
                invoke(2)

        state = json.loads((run/'state.json').read_text())
        with patch.dict(sys.modules, {'company': company}):
            clock = open_catalog(run, cfg, state)
        labor = {w['equipment']: w['scheduled_labor'] for w in state['company_erp']['work'].values()
                 if not w.get('rejected_remainder')}
        offset = 2 * DAY if outage else 0
        self.assertEqual(labor[1]['intervals'], [[600 + offset, 624 + offset]])
        self.assertEqual(labor[2]['intervals'], [[600 + offset, 630 + offset], [660 + offset, 678 + offset]])
        self.assertEqual(labor[2]['minutes'], 48)
        self.assertEqual((labor[2]['source'],labor[2]['work_order']), ('operator-source','wo-shampoo'))
        return observed, clock.history(), ledger, checked

    def test_normal_queue_holds_same_center_until_dated_pinned_qa_rejection(self):
        with TemporaryDirectory() as tmp:
            self.run_company(Path(tmp) / 'held-wip', False, operating=True, held_wip=True)

    def test_normal_operating_entry_resends_the_persisted_original_envelope(self):
        with TemporaryDirectory() as tmp:
            self.run_company(Path(tmp) / 'normal', True, operating=True)

    def test_primary_distinguishes_preflight_outage_and_ambiguous_start(self):
        with TemporaryDirectory() as tmp:
            self.run_company(Path(tmp) / 'outage', True, operating=True, outage=True)

    def test_same_minutes_and_ids_after_native_and_qms_interruptions(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            baseline, replay = self.run_company(run/'baseline', False), self.run_company(run/'replay', True)
        self.assertEqual(baseline[:3], replay[:3])
        observed, history, ledger, checked = replay
        times = {(stage, key): minute for stage, key, minute, _, _ in observed if stage != 'lab-result'}
        self.assertEqual(times['start', 1], 600)
        self.assertEqual(times['start', 2], 600)
        self.assertEqual(times['complete', 1], 624)
        self.assertEqual(times['complete', 2], DAY + 600)
        self.assertIn('operator-source:mrp.production:2', checked)
        self.assertEqual(set(ledger), {'lot-lotion', 'lot-shampoo'})
        results = [(key, at) for stage, key, at, _, _ in observed if stage == 'lab-result']
        self.assertEqual(results, [('lot-lotion', 690), ('lot-lotion', DAY + 690),
                                   ('lot-shampoo', DAY + 690)])
        self.assertEqual([(row['phase'], row['at']) for row in history
                          if json.loads(row['id'].removeprefix('company:')) ==
                          [['lab', 'lot-lotion', None], 'incubate', 0]],
                         [('start', 624), ('complete', 654)])
        ids = [row['id'] for row in history if row['phase'] == 'complete']
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(any('operator-source' in event and 'order-and-revision-' * 9 in event for event in ids))
        for _, _, at, date, event in observed:
            self.assertEqual(date, f'2027-01-0{1 + at // DAY}')
            if event.startswith('company:'):
                self.assertIn(event, ids)
