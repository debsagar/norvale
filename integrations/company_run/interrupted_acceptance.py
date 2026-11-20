"""Independent readback of completed interrupted-WIP dispositions."""
import json
from decimal import Decimal
from .consumption import amount

from integrations.mes.client import MO, WO
from integrations.qms.adapter import CASE, QMS
from native.company_erp import digest


def verified_interrupted(engine):
    flow = engine.company_erp
    verified = False
    for case in flow.equipment.cases.values():
        if not case.get('disposition'):
            continue  # An unresolved interruption remains pending, not accepted.
        try:
            authority = flow.equipment.scoped_authority(case['center'])
            reader = authority.operator('PRODUCTION')
            batch = case['interrupted']
            source = reader.source_id
            mo_id = batch['mo']
            completion = f'{source}:{MO}:{mo_id}'
            evidence = case['qa_evidence']
            fault = reader.read('mrp.workcenter.productivity', case['fault']['native_id'])
            wo = reader.read(WO, fault['norvale_interrupted_id'][0])
            mo = reader.read(MO, mo_id)
            candidates = [w for w in flow.state['work'].values()
                          if w.get('request', {}).get('work_order') == batch['work_order']
                          and w.get('equipment') == case['center']
                          and w.get('revision', {}).get('mes_source', source) == source]
            work, = candidates
            revision = work['revision']
            order = engine.api['PLANNER'].get('Work Order', batch['work_order'])
            definition = json.loads(order['custom_norvale_definition'])
            if (any(definition.get(k) != v for k, v in revision.items())
                    or case['fault']['source_id'] != source
                    or case['disposition'].get('source_id') != source
                    or case['disposition'].get('system') != 'mes'
                    or case['disposition'].get('entity') != MO
                    or case['fault'].get('norvale_interrupted_id') != fault['norvale_interrupted_id']
                    or case['disposition'].get('native_id') != mo_id
                    or batch['completion_ref'] != completion
                    or batch['batch'] != 'NV-WIP-' + digest(completion)[:24]
                    or fault['workcenter_id'][0] != authority.native_center(case['center'])
                    or wo['production_id'][0] != mo_id or wo['workcenter_id'][0] != fault['workcenter_id'][0]
                    or mo['id'] != mo_id or mo['state'] != 'cancel' or mo['origin'] != batch['work_order']
                    or mo['product_qty'] != batch['qty'] or mo['product_uom_id'][1] not in authority.interrupted_settings(batch['item'])['units']
                    or authority.interrupted_settings(batch['item'])['units'][mo['product_uom_id'][1]] != batch['uom']
                    or reader.read('product.product', mo['product_id'][0])['default_code'] != batch['item']
                    or mo['bom_id'][0] != int(revision['mes_ack_id'])
                    or mo['norvale_revision'] != work['dispatch']['version']
                    or work['dispatch'].get('source', source) != source
                    or order['name'] != batch['work_order'] or order['docstatus'] != 1
                    or order['company'] != engine.company
                    or order['production_item'] != batch['item'] or order['stock_uom'] != batch['uom']
                    or order['bom_no'] != revision['erp_bom'] or order['qty'] != work['qty']
                    or work.get('rejected_remainder') != batch['qty']
                    or fault['norvale_disposition'] != 'qms:' + digest(evidence)
                    or authority.interrupted_disposition(batch, work, as_of=case['qa_as_of'], role='QA') != evidence):
                return False
            qms = QMS(**authority.interrupted_settings(batch['item'])['qms_reader'])
            nc = qms.get(CASE, evidence['case'])
            if (nc['name'] != evidence['case'] or nc['custom_qms_batch'] != batch['batch']
                    or nc['custom_failed_inspection'] != evidence['id']):
                return False
            moves = reader._rows('stock.move', mo['move_raw_ids'] + mo['move_finished_ids'])
            lines = reader._lines(mo['move_raw_ids'] + mo['move_finished_ids'])
            if any(row['state'] == 'done' for row in moves + lines):
                return False
            # Inspect every submitted manufacture for this ERP work, not only checkpointed postings.
            filters = {'docstatus': 1, 'stock_entry_type': 'Manufacture', 'work_order': batch['work_order']}
            counted = set()
            produced = Decimal(0)
            while True:
                page = engine.api['PLANNER'].list('Stock Entry', filters, limit=500, order_by='name asc')
                for item in page:
                    post = engine.api['PLANNER'].get('Stock Entry', item['name'])
                    pin = json.loads(post.get('remarks') or '{}')
                    ref = pin['completion_id']
                    prefix = f'{source}:{MO}:'
                    if (post['name'] != item['name'] or post['docstatus'] != 1
                            or post['stock_entry_type'] != 'Manufacture' or post['work_order'] != order['name']
                            or post['bom_no'] != order['bom_no'] or not ref.startswith(prefix)
                            or pin.get('mes_source', source) != source or ref == completion):
                        return False
                    if ref in counted:
                        return False
                    counted.add(ref)
                    sibling_id = int(ref[len(prefix):])
                    sibling = reader.completion(sibling_id)
                    output = sibling['output']
                    finished, = [row for row in post['items'] if row.get('is_finished_item')]
                    if (sibling['completion_ref'] != ref or sibling['erp_work_ref'] != order['name']
                            or sibling['recorded_at_utc'] != pin['version']
                            or sibling['revision'] != work['dispatch']['version']
                            or sibling['bom']['native_id'] != int(revision['mes_ack_id'])
                            or str(pin['dispatch_id']) != str(work['dispatch']['id'])
                            or pin['revision'] != revision
                            or pin['output_lot_id'] != f"{source}:stock.lot:{output['lot']['native_id']}"
                            or finished['item_code'] != batch['item']
                            or finished['qty'] != output['quantity']
                            or amount(output['quantity']) <= 0 or post['fg_completed_qty'] != output['quantity']):
                        return False
                    produced += amount(output['quantity'])
                if len(page) < 500:
                    break
                filters['name'] = ['>', page[-1]['name']]
            if produced != amount(order['produced_qty']) or produced + amount(batch['qty']) > amount(order['qty']):
                return False
            verified = True
        except (KeyError, TypeError, ValueError, IndexError):
            return False
    return verified
