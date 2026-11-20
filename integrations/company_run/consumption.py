"""Reconcile ordinary-company manufacture issues from independently collected records."""
from collections import defaultdict
from decimal import Decimal

# The ordinary receivers use kilograms and individual counts in both systems.
# These are unit names for the same scale. Unknown or scaled units need a
# separate contract, even when the observed numbers happen to match.
STOCK_UNITS = {'kg': 'Kg', 'Units': 'Nos'}


def amount(value):
    if type(value) not in (int, float):
        raise ValueError('missing native quantity')
    qty = Decimal(str(value))
    if not qty.is_finite():
        raise ValueError('non-finite native quantity')
    return qty


def bundle_entry(post, row, bundles, direction, warehouse):
    bundle = bundles.get(row.get('serial_and_batch_bundle'), {})
    batch = row.get('batch_no')
    entries = bundle.get('entries', [])
    if (not all((post.get('name'), row.get('name'), row.get('item_code'), batch, warehouse))
            or (bundle.get('docstatus'), bundle.get('is_cancelled'), bundle.get('voucher_type'),
                bundle.get('voucher_no'), bundle.get('voucher_detail_no'), bundle.get('item_code'),
                bundle.get('type_of_transaction'), bundle.get('warehouse'))
            != (1, 0, 'Stock Entry', post['name'], row['name'], row['item_code'], direction, warehouse)
            or len(entries) != 1 or entries[0].get('batch_no') != batch
            or entries[0].get('warehouse') != warehouse or entries[0].get('serial_no')):
        raise ValueError('batch bundle differs from manufacture row')
    return entries[0]


def reconcile_inputs(post, inputs, source, postings, bundles, ledger, unit_ids):
    """Match each MES input to one native ERP batch row and active ledger issue."""
    by_batch = {}
    pins = {}
    for pin in post.get('intermediate_inputs', []):
        ref = pin.get('mes_move', {})
        line = str(ref.get('native_id'))
        if ref.get('source_id') != source or ref.get('entity') != 'stock.move.line' or line in pins:
            raise ValueError('ambiguous intermediate movement reference')
        pins[line] = pin
    for line in inputs:
        unit, unit_id = line.get('unit'), line.get('unit_id')
        stock_uom = STOCK_UNITS.get(unit)
        if (not stock_uom or not unit_id or unit_ids[source, 'name', unit] != {unit_id}
                or unit_ids[source, 'id', unit_id] != {unit}):
            raise ValueError('unrecognized or conflicting MES stock unit')
        batch = line.get('lot_name')
        lot = f"{source}:stock.lot:{line.get('lot_id')}"
        if (pin := pins.pop(line.get('line_id'), None)) is not None:
            producer = postings.get(pin.get('erp_manufacture'), {})
            upstream = producer.get('completion', {})
            batch = pin.get('batch')
            finished = [r for r in producer.get('items', []) if r.get('is_finished_item')]
            if (pin.get('mes_lot_id') != lot or producer is post or producer.get('docstatus') != 1
                    or producer.get('stock_entry_type') != 'Manufacture' or upstream.get('output_lot_id') != lot
                    or len(finished) != 1 or finished[0].get('batch_no') != batch
                    or finished[0].get('item_code') != line.get('item_code')
                    or finished[0].get('stock_uom') != stock_uom):
                raise ValueError('intermediate lot differs from upstream manufacture')
            bundle_entry(producer, finished[0], bundles, 'Inward', finished[0].get('t_warehouse'))
        if (not batch or not line.get('lot_id') or not line.get('product_id') or not line.get('line_id')
                or not line.get('item_code') or batch in by_batch):
            raise ValueError('missing or duplicate MES input lot')
        by_batch[batch] = (line, stock_uom)
    if not by_batch or pins:
        raise ValueError('missing MES inputs or unresolved intermediate movements')

    issues = []
    raw_ids = set()
    finished_ids = {r.get('name') for r in post.get('items', []) if r.get('is_finished_item')}
    for row in post.get('items', []):
        if row.get('is_finished_item') and not row.get('s_warehouse'):
            continue
        name, batch, item, warehouse = (row.get(k) for k in ('name', 'batch_no', 'item_code', 's_warehouse'))
        if (name in raw_ids or name in finished_ids or row.get('t_warehouse')
                or batch not in by_batch):
            raise ValueError('unexpected or duplicate manufacture input row')
        raw_ids.add(name)
        line, stock_uom = by_batch.pop(batch)
        qty = amount(line.get('quantity'))
        if (qty <= 0 or line.get('item_code') != item or stock_uom != row.get('uom')
                or stock_uom != row.get('stock_uom') or qty != amount(row.get('qty'))
                or any(amount(row[field]) != qty for field in ('stock_qty', 'transfer_qty') if field in row)
                or ('conversion_factor' in row and amount(row['conversion_factor']) != 1)):
            raise ValueError('MES and ERP input item, unit or quantity differ')
        entry = bundle_entry(post, row, bundles, 'Outward', warehouse)
        if amount(entry.get('qty')) != -qty:
            raise ValueError('batch bundle input quantity differs')
        moves = ledger.get(name, [])
        if len(moves) != 1:
            raise ValueError('missing or duplicate active ERP ledger issue')
        move, = moves
        if (not post.get('company') or not post.get('posting_date')
                or (move.get('docstatus'), move.get('is_cancelled'), move.get('company'),
                    move.get('posting_date'), move.get('item_code'), move.get('warehouse'),
                    move.get('stock_uom'), move.get('serial_and_batch_bundle'))
                != (1, 0, post['company'], post['posting_date'], item, warehouse, stock_uom,
                    row['serial_and_batch_bundle']) or amount(move.get('actual_qty')) != -qty):
            raise ValueError('ERP ledger issue differs from manufacture input')
        issues.append(dict(mes_line=line['line_id'], mes_lot=line['lot_id'], mes_unit_id=line['unit_id'],
                           mes_unit=line['unit'], mes_quantity=str(qty), item=item, batch=batch,
                           erp_row=name, erp_bundle=row['serial_and_batch_bundle'], erp_ledger=move['name'],
                           erp_unit=stock_uom, erp_quantity=str(amount(row['qty'])),
                           ledger_quantity=str(amount(move['actual_qty']))))
    if by_batch or any(
        detail not in raw_ids and (detail not in finished_ids or amount(move.get('actual_qty')) <= 0)
        for detail, moves in ledger.items() for move in moves
    ):
        raise ValueError('missing manufacture inputs or unlinked ERP ledger issue')
    return sorted(issues, key=lambda issue: issue['erp_row'])


def index_issues(latest):
    ledger = defaultdict(lambda: defaultdict(list))
    for doc in latest.values():
        if doc.get('voucher_type') == 'Stock Entry' and doc.get('is_cancelled') != 1:
            ledger[doc.get('voucher_no')][doc.get('voucher_detail_no')].append(doc)
    return ledger
