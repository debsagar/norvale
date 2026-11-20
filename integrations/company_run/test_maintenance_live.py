"""Exercise the engine service hook against dedicated MES, Gitea and QMS fixtures."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from threading import Barrier
from xmlrpc.client import Fault

from integrations.company_run.authority import Authority
from integrations.mes.client import EVENT, MO, Mes
from integrations.mes.test_live import input_lots, must_fail
from integrations.qms.adapter import CASE, INSPECTION, APIError, QMS
from native.company import Engine, save
from native.company_erp import CompanyERP, Pending
from native.systems_v2 import Tickets


def engine(run):
    e = object.__new__(Engine)
    e.run = run
    path = run / 'state.json'
    e.state = json.loads(path.read_text()) if path.exists() else {
        'ops': {'maintenance': {}}, 'physical': {},
    }
    e.day = 1
    e.config = {'maintenance_units': 12}
    e.persist = lambda: save(path, e.state)
    e.log = lambda kind, **data: None
    e.company_erp = CompanyERP(e, Authority(run))
    return e


def before(run, fixture):
    e = engine(run)
    flow, a = e.company_erp, e.company_erp.authority
    cfg = a.settings()
    m = Mes(**cfg['mes'])
    operator = Mes(**cfg['operator'])
    center_a, center_b = fixture['centers']
    bom_a, bom_b = fixture['boms']
    product, hold = fixture['product'], fixture['hold']
    inputs = input_lots((fixture['bulk'][1], 1), (fixture['bottle'], 2))
    released = [row['lot_id'] for row in inputs]
    mo_a = m.dispatch('W5-WO-A', product, bom_a, m.acknowledge(bom_a)['revision'], 2, hold)['id']
    if 'a' not in flow.state['work']:
        operator.start(mo_a, inputs, released)
        flow.state['work']['a'] = dict(request={'work_order': 'W5-WO-A', 'released_on': e.date()}, qty=14, produced=12,
                                      equipment=center_a, item='NV-SHAMPOO', uom='Nos',
                                      revision={'inspection_spec': cfg['specifications']['NV-SHAMPOO']})
    wo_a, = operator.read(MO, mo_a)['workorder_ids']
    e.service()  # Recurring per-equipment usage trigger on the operating engine.
    e = engine(run)
    flow, a = e.company_erp, e.company_erp.authority
    e.service()  # Replay after a controller restart.
    key, = flow.equipment.cases
    case = flow.equipment.cases[key]
    fault = case['fault']
    ticket = case['ticket']
    assert m.read(EVENT, fault['native_id'])['norvale_interrupted_id'][0] == wo_a
    assert not operator.availability(center_a)['available']
    assert operator.availability(center_b)['available']
    assert len([row for row in Tickets(run).records() if Tickets.marker(key) in row['body']]) == 1
    assert len(m.call(EVENT, 'search', [['description', '=like', 'Fault ' + key + '%']])) == 1
    mo_b = m.dispatch('W5-WO-B', product, bom_b, m.acknowledge(bom_b)['revision'], 2, hold)['id']
    operator.start(mo_b, inputs, released)
    assert operator.complete(mo_b, fixture['output'][1], 2, inputs, released)['output']['quantity'] == 2
    assert not operator.availability(center_a)['available']
    issue = f'/repos/controller/operations/issues/{ticket["number"]}'
    body = (f'Verified restart: {fault["source_url"]}\nDiagnosis: loose bracket\n'
            'Repair: bracket tightened\nTest: vibration check within limit\nResult: PASS')
    writer = Tickets(run)
    writer.request('POST', issue + '/comments', {'body': body})
    writer.request('PATCH', issue, {'state': 'closed'})
    e.service()
    assert not m.read(EVENT, fault['native_id'])['date_end']
    verifier = Tickets(run, token=(run / 'maintenance-verifier.token').read_text().strip())
    verifier.request('POST', issue + '/comments', {'body': body})
    must_fail(lambda: operator.restart(center_a, fault['native_id'], 'unauthorized'),
              'Manufacturing authority required')
    e.service()
    assert case['restart']['date_end'] and operator.availability(center_a)['working_state'] == 'normal'
    assert not operator.availability(center_a)['available']  # Interrupted operation still occupies A.
    must_fail(lambda: operator.complete(mo_a, fixture['output'][0], 2, inputs, released),
              'Interrupted work requires a separate batch disposition')
    try:
        flow.equipment.guard_execution(case['interrupted']['completion_ref'])
    except Pending:
        pass
    else:
        raise AssertionError('ERP completion guard accepted interrupted work')
    assert not flow.state['lots']
    assert not case.get('disposition')
    print(f'Odoo MES fault {fault["native_id"]} interrupted WO {wo_a}; one Gitea issue '
          f'#{ticket["number"]} after replay; MES MO {mo_b} on B completed 2 units into hold.')
    print(f'Ticket closure and controller comment left A blocked; verifier comment '
          f'{case["verification"]["id"]} enabled MES restart; interrupted MO {mo_a} remained pending.')


def after(run, fixture, reference):
    e = engine(run)
    flow, a = e.company_erp, e.company_erp.authority
    cfg = a.settings()
    case, = flow.equipment.cases.values()
    batch = case['interrupted']
    e.service()
    assert not case.get('disposition')
    analyst, approver = (QMS(**cfg['lab'][role]) for role in ('analyst', 'approver'))
    ref = analyst.get('Stock Entry', reference)
    row, = ref['items']
    inspection = analyst.create(
        INSPECTION, batch['batch'] + ':interrupted',
        inspection_type='In Process', reference_type='Stock Entry',
        reference_name=reference, child_row_reference=row['name'],
        item_code=batch['item'], batch_no=batch['batch'], report_date=e.date(),
        sample_size=1, inspected_by=cfg['lab']['analyst_user'],
        custom_spec_revision=flow.state['work']['a']['revision']['inspection_spec'],
        custom_lot_quantity=batch['qty'], custom_lot_uom=batch['uom'],
        custom_decision_rationale='Disposition of interrupted ' + batch['completion_ref'],
        readings=[dict(specification=cfg['lab']['parameter'], numeric=1, min_value=5, max_value=6,
                       reading_1='7.2')])
    analyst.transition(inspection['name'], 'Record')
    nc = analyst.create(CASE, batch['batch'] + ':incident', procedure=cfg['lab']['procedure'],
                        subject='Interrupted manufacturing batch', details='Equipment fault',
                        custom_qms_batch=batch['batch'], custom_failed_inspection=inspection['name'])
    try:
        analyst.transition(inspection['name'], 'Reject Lot')
    except APIError:
        pass
    else:
        raise AssertionError('QMS analyst could authorize rejection')
    e.service()
    assert not case.get('disposition')
    approved = approver.transition(inspection['name'], 'Reject Lot')
    e.service()
    assert case['qa_evidence']['id'] == approved['name'] and case['qa_evidence']['case'] == nc['name']
    e = engine(run)
    e.service()
    flow = e.company_erp
    case, = flow.equipment.cases.values()
    operator = Mes(**cfg['operator'])
    assert operator.read(MO, batch['mo'])['state'] == 'cancel'
    assert operator.availability(case['center'])['available']
    assert flow.state['work']['a']['rejected_remainder'] == batch['qty']
    assert not flow.state['lots']
    quant = operator.call('stock.quant', 'search_count',
                          [['lot_id', '=', fixture['output'][0]], ['location_id', '=', fixture['hold']],
                           ['quantity', '>', 0]])
    assert not quant
    print(f'QMS approver rejected {approved["name"]} with case {nc["name"]}; '
          f'MES MO {batch["mo"]} cancelled after verified readback and remained cancelled on replay.')
    print('A available for new work, zero interrupted output in native hold and no ERP lot registered.')


def race(run, fixture):
    cfg = Authority(run).settings()
    planner = Mes(**cfg['mes'])
    operator = Mes(**cfg['operator'])
    technician = Mes(**cfg['maintenance_mes'])
    center, bom = fixture['centers'][0], fixture['boms'][0]
    inputs = input_lots((fixture['bulk'][1], 0.5), (fixture['bottle'], 1))
    revision = planner.acknowledge(bom)['revision']
    mo = planner.dispatch('W5-Race', fixture['product'], bom, revision, 1, fixture['hold'])
    barrier = Barrier(2)

    def start():
        barrier.wait()
        try:
            return operator.start(mo['id'], inputs, [row['lot_id'] for row in inputs])
        except Fault as exc:
            if 'allocated equipment is unavailable' not in exc.faultString:
                raise
            return None

    def fault():
        barrier.wait()
        return technician.fault(center, 'W5-Race')

    with ThreadPoolExecutor(max_workers=2) as pool:
        starting = pool.submit(start)
        faulted = pool.submit(fault)
        started, event = starting.result(), faulted.result()
    assert not operator.availability(center)['available']
    assert (event['norvale_interrupted_id'][0] if event['norvale_interrupted_id'] else None) == (
        started['id'] if started else None)
    print('Concurrent native start and fault serialized with a consistent interrupted-work link.')


if __name__ == '__main__':
    phase, run, fixture, *extra = sys.argv[1:]
    globals()[phase](Path(run), json.loads(Path(fixture).read_text()), *extra)
