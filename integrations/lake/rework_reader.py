"""Resolve rework only from source records observed in the archive."""
from decimal import Decimal
import hashlib
import json


from integrations.qms.measurements import numeric_results


def one_snapshot(rows):
    """Repeated reads can corroborate the same pinned native version."""
    return rows[0] if rows and all(row == rows[0] for row in rows) else None


def resolve(lot, posting, erp, history, latest, movements, unresolved, *, disputed, physical_only=False):
    def fail(reason):
        unresolved.append([lot['batch'], reason])
    if posting['name'] in disputed:
        return fail('conflicting ERP observations for rework posting')
    pin = posting.get('rework')
    if not pin or not isinstance(pin.get('qms_authority'), dict):
        return fail('rework provenance')
    authority = pin['qms_authority']
    inputs = [(row, batch, -qty) for row in posting.get('items', [])
              for batch, qty in movements(posting, row, 'Outward', 'Stock Entry')]
    outputs = [(row, batch, qty) for row in posting.get('items', [])
               for batch, qty in movements(posting, row, 'Inward', 'Stock Entry')]
    if len(inputs) != 1 or len(outputs) != 1:
        return fail('rework batch movements')
    incoming, failed_batch, total = inputs[0]
    outgoing, output_batch, salvage = outputs[0]
    number = lambda x: Decimal(str(x))
    if (output_batch != lot['batch'] or failed_batch == output_batch
        or incoming.get('item_code') != lot['item'] or outgoing.get('item_code') != lot['item']
        or incoming.get('stock_uom') != lot['unit'] or outgoing.get('stock_uom') != lot['unit']
        or incoming.get('s_warehouse') != outgoing.get('t_warehouse')
        or not incoming.get('s_warehouse') or pin.get('uom') != lot['unit']
        or number(salvage) != number(authority.get('quantity', 0))
        or number(total) != number(salvage) + number(pin.get('scrapped', 0))):
        return fail('rework conservation and hold')
    source_ref = pin.get('rework', '').split(':')
    if len(source_ref) != 3 or source_ref[1] != 'mrp.production':
        return fail('rework MES source')
    source = source_ref[0]
    reports = [d for d in history.get(source, {}).get('mrp.production', [])
               if d.get('completion_ref') == pin['rework'] and d.get('modified') == pin.get('rework_version')]
    report = one_snapshot(reports)
    if not pin.get('rework_version') or not report:
        return fail('rework MES completion version')
    native = report.get('rework_native')
    if not native or not native.get('source_order') or not native.get('authority'):
        return fail('original MES execution')
    originals = []
    for (src, entity, _), doc in latest.items():
        if (src, entity) != (erp, 'Stock Entry') or doc.get('docstatus') != 1 or doc.get('stock_entry_type') != 'Manufacture':
            continue
        for row in doc.get('items', []):
            if row.get('is_finished_item') and row.get('item_code') == lot['item']:
                for batch, quantity in movements(doc, row, 'Inward', 'Stock Entry'):
                    if batch == failed_batch and number(quantity) == number(total):
                        originals.append(doc)
    if (len(originals) != 1 or originals[0]['name'] in disputed or not posting.get('company') or originals[0].get('company') != posting.get('company')
        or originals[0].get('completion', {}).get('mes_source', source) != source):
        return fail('original ERP posting and MES version')
    completion = originals[0].get('completion', {})
    snapshots = [d for d in history.get(source, {}).get('mrp.production', [])
                 if d.get('name') == str(native['source_order'])
                 and d.get('completion_ref') == completion.get('completion_id')
                 and d.get('modified') == completion.get('version')]
    original = one_snapshot(snapshots)
    if not completion.get('version') or not original:
        return fail('original ERP posting and MES version')
    request = dict(batch=failed_batch, qms_batch=authority.get('batch'),
                   inspection=authority.get('inspection'), mes_order=int(original['name']))
    marker = 'QMS:' + str(authority.get('inspection')) + ':' + hashlib.sha256(
        json.dumps([request, authority], sort_keys=True, allow_nan=False).encode()).hexdigest()
    if native.get('authority') != marker:
        return fail('native QMS authority crosslink')
    rows = report.get('inputs', [])
    out, old = report.get('output', {}), original.get('output', {})
    if (len(rows) != 1 or not report.get('company_id') or report.get('company_id') != original.get('company_id')
        or report.get('revision') != original.get('revision')
        or report.get('hold_location_id') != original.get('hold_location_id')
        or str(native.get('input_location')) != original.get('hold_location_id')
        or rows[0].get('lot_id') != old.get('lot_id') or out.get('lot_id') == old.get('lot_id')
        or any(x.get('product_id') != old.get('product_id') or x.get('unit_id') != old.get('unit_id') for x in (rows[0], out))
        or number(rows[0].get('quantity', 0)) != number(salvage)
        or number(out.get('quantity', 0)) != number(salvage)
        or number(old.get('quantity', 0)) != number(total)):
        return fail('native rework movements and company')
    scrap = native.get('scrap')
    loss = number(pin.get('scrapped', 0))
    if loss:
        lines = scrap.get('lines', []) if scrap else []
        if (not scrap or scrap.get('id') != pin.get('scrap_id') or scrap.get('state') != 'done'
            or str(scrap.get('production_id')) != report['name'] or len(lines) != 1
            or lines[0].get('state') != 'done' or str(lines[0].get('lot_id')) != old.get('lot_id')
            or str(lines[0].get('product_id')) != old.get('product_id')
            or str(lines[0].get('unit_id')) != old.get('unit_id')
            or str(lines[0].get('location_id')) != original.get('hold_location_id')
            or lines[0].get('destination_id') != scrap.get('destination_id')
            or number(lines[0].get('quantity', 0)) != loss):
            return fail('native scrap')
    elif scrap or pin.get('scrap_id'):
        return fail('native scrap')
    if not physical_only and not lot.get('quality_release'):
        return fail('released rework retest version')
    qms_source = lot.get('qms_source') or (lot.get('quality_release') or [None])[0]
    if not qms_source or (pin.get('qms_source') and pin['qms_source'] != qms_source):
        return fail('rework QMS source')
    failures = [d for d in history.get(qms_source, {}).get('Quality Inspection', [])
                if d.get('name') == authority.get('inspection') and d.get('modified') == authority.get('version')]
    cases = [d for d in history.get(qms_source, {}).get('Non Conformance', [])
             if d.get('name') == authority.get('case') and d.get('custom_failed_inspection') == authority.get('inspection')
             and d.get('custom_qms_batch') == authority.get('batch')]
    failure = one_snapshot(failures)
    if not failure or not cases:
        return fail('authorized failed QMS version and case')
    if (failure.get('workflow_state') != 'Rework Authorized' or failure.get('status') != 'Rejected'
        or failure.get('docstatus') != 1 or failure.get('modified_by') != authority.get('approver')
        or failure.get('batch_no') != authority.get('batch') or failure.get('item_code') != lot['item']
        or failure.get('custom_lot_uom') != lot['unit'] or failure.get('custom_rework_input_batch') != failed_batch
        or number(failure.get('custom_lot_quantity', 0)) != number(total)
        or number(failure.get('custom_rework_salvage_quantity', 0)) != number(salvage)):
        return fail('authorized QMS scope')
    before = numeric_results(failure)
    if not before or all(before.values()):
        return fail('measured failure')
    if physical_only:
        lot['physical_rework'] = True
        return
    release = lot.get('quality_release')
    retests = [d for d in history.get(qms_source, {}).get('Quality Inspection', [])
               if release and d.get('name') == release[2] and d.get('modified') == release[3]]
    retest = one_snapshot(retests)
    if (not retest or (retest.get('workflow_state'), retest.get('status'),
            retest.get('docstatus')) != ('Released', 'Accepted', 1)
        or retest.get('item_code') != lot['item'] or retest.get('batch_no') != authority.get('batch')
        or retest.get('custom_retest_of') != failure['name']
        or retest.get('custom_rework_execution') != pin['rework']
        or retest.get('custom_rework_output_batch') != output_batch
        or retest.get('custom_spec_revision') != failure.get('custom_spec_revision')
        or retest.get('custom_lot_uom') != lot['unit']
        or number(retest.get('custom_lot_quantity', 0)) != number(salvage)):
        return fail('released rework retest version')
    before, after = numeric_results(failure), numeric_results(retest)
    if not before or before.keys() != after.keys() or all(before.values()) or not all(after.values()):
        return fail('measured failure and passing retest')
    lot['work_order'] = originals[0].get('work_order')
    lot['stock_entry'] = posting['name']
    lot['rework'] = dict(stock_entry=posting['name'], original_stock_entry=originals[0]['name'], failed_batch=failed_batch, total=total,
                         salvage=salvage, scrapped=pin.get('scrapped'), unit=lot['unit'],
                         hold_warehouse=incoming['s_warehouse'], mes=[source, 'mrp.production', report['name'], report['modified']],
                         original_mes=[source, 'mrp.production', original['name'], original['modified']],
                         original_inputs=original['inputs'], native_scrap=scrap,
                         failed_inspection=[qms_source, 'Quality Inspection', failure['name'], failure['modified']],
                         case=[qms_source, 'Non Conformance', authority['case']])
