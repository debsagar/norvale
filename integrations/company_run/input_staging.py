"""Conserve ERP input lots across independently credited MES sites."""
from fractions import Fraction
from hashlib import sha256
import json
from math import isclose

from native.company_erp import Pending

PREFIX = 'NV-STAGE-'
LINE = 'stock.move.line'
FIELDS = ['state', 'lot_id', 'product_id', 'product_uom_id', 'quantity',
          'location_id', 'location_dest_id', 'company_id', 'owner_id', 'package_id', 'result_package_id']


def matches(row, pin, amount):
    return (row['state'] == 'done' and row['lot_id'][0] == pin['lot']
            and row['product_id'][0] == pin['product'] and row['product_uom_id'][0] == pin['uom']
            and row['company_id'][0] == pin['company'] and row['location_dest_id'][0] == pin['location']
            and not row['owner_id'] and not row['package_id'] and not row['result_package_id']
            and isclose(row['quantity'], amount, rel_tol=0, abs_tol=1e-9))


def stage(actor, work, inp, lot, product, native):
    flow = actor.e.company_erp
    root = flow.state['work'][work['key']]
    batch = inp['batch']
    original, = [row for row in root['inputs'] if row['batch'] == batch]
    available, disposition = flow.available(lot, 'PRODUCTION')
    if available + 1e-8 < inp['qty'] or disposition != inp['disposition']:
        raise Pending('MES staging requires released ERP stock for the input')
    used = root.get('used', {}).get(batch, 0)
    if (inp['qty'] <= 0 or inp['qty'] + used > original['qty'] + 1e-8
            or any(inp[field] != original[field] for field in ('item', 'uom', 'disposition'))):
        raise Pending('MES staging exceeds the original ERP input allocation')
    mes = actor.mes
    source = mes.source_id
    if root['dispatch'].get('source', source) != source or work['dispatch'].get('source', source) != source:
        raise Pending('MES receipt source differs from the original dispatch')
    uom, = mes.call('product.product', 'read', [product], fields=['uom_id'])
    pin = dict(source=source, product=product, lot=native, company=actor.cfg['company_id'],
               location=actor.cfg['stock'], uom=uom['uom_id'][0], qty=original['qty'])
    pin['receipt'] = PREFIX + sha256(json.dumps(
        [root['request']['key'], batch, source], separators=(',', ':')).encode()).hexdigest()
    ledger = flow.state.setdefault('mes_input_staging', {}).setdefault(batch, {})
    previous = ledger.get(root['key'])
    if previous is not None and any(previous[k] != value for k, value in pin.items()):
        raise Pending('MES input staging differs from its durable intent')

    # Older dispatches could have posted full-lot adjustments before saving a
    # start. Without same-site native history their outstanding credit is unknown.
    historical = [other for other in flow.state['work'].values()
                  if not other.get('scoped_mes_staging') and other.get('request')
                  and other['key'] not in ledger
                  and any(row['batch'] == batch for row in other['inputs'])]
    for other in historical:
        dispatch = other.get('dispatch', {})
        other_source = dispatch.get('source') or other['revision'].get('mes_source')
        if other_source is None:
            other_source = flow.authority.settings().get('mes', {}).get('source_id')
        if other_source != source:
            raise Pending('Historical cross-site ERP input staging requires reconciliation')
        if other.get('mes_stock_location', pin['location']) != pin['location']:
            raise Pending('Historical ERP input staging has a different stock location')

    inventory = [['lot_id', '=', native], ['state', '=', 'done'],
                 ['location_id.usage', '=', 'inventory']]
    legacy = mes.call(LINE, 'search_read', inventory + [
        '|', ['move_id.origin', '=', False], '!', ['move_id.origin', '=like', PREFIX + '%']],
        fields=FIELDS, limit=2)
    if legacy:
        if (len(legacy) != 1 or not matches(legacy[0], pin, lot['qty'])
                or any(entry['source'] != source or not entry.get('legacy') for entry in ledger.values())):
            raise Pending('Historical MES input receipt requires reconciliation')
        pin.update(legacy=legacy[0]['id'], native_source_location=legacy[0]['location_id'][0])
    elif any(entry.get('legacy') for entry in ledger.values()) or any(
            other.get('used', {}).get(batch) or other.get('mes_start')
            or any(op.get('mes_start') for op in other.get('production_operations', {}).values())
            for other in historical):
        raise Pending('Historical MES input receipt could not be verified')

    if not legacy and not any(entry['source'] == source and entry['location'] == pin['location']
                              for entry in ledger.values()):
        quants = mes.call('stock.quant', 'search_read', [
            ['lot_id', '=', native], ['location_id', '=', pin['location']],
        ], fields=['quantity'])
        if any(quant['quantity'] for quant in quants):
            raise Pending('MES opening balance has no reconciled native receipt')

    total = sum(Fraction(str(entry['qty'])) for key, entry in ledger.items() if key != root['key'])
    limit = min(lot['qty'], inp['disposition'].get('qty', lot['qty']))
    if total + Fraction(str(pin['qty'])) > Fraction(str(limit)) + Fraction('0.00000001'):
        raise Pending('MES staging would exceed the released ERP source lot')
    if previous is not None and (previous.get('legacy') != pin.get('legacy')
                                 or (legacy and previous['native_source_location'] != pin['native_source_location'])):
        raise Pending('Historical MES receipt changed during staging')
    if not legacy and (previous is None or 'production_id' in previous):
        pin['production_id'] = int(root['dispatch']['id'])
        if previous is not None and previous['production_id'] != pin['production_id']:
            raise Pending('MES receipt manufacturing order changed')
    if previous is None:
        ledger[root['key']] = pin
        actor.e.persist()
    if legacy:
        return

    def readback():
        return mes.call(LINE, 'search_read', [['move_id.origin', '=', pin['receipt']],
                        ['company_id', '=', pin['company']], ['location_id.usage', '=', 'inventory']],
                        fields=FIELDS, limit=2)

    rows = readback()
    if not rows and not (previous or {}).get('native_line'):
        mes.call('stock.lot', 'norvale_stage_input', [native], pin['receipt'], pin['location'], pin['qty'],
                 int(root['dispatch']['id']))
        rows = readback()
    if len(rows) != 1 or not matches(rows[0], pin, pin['qty']):
        raise Pending('Native MES staging receipt differs from the authorized allocation')
    if 'production_id' in pin:
        matched = mes.call(LINE, 'search', [['id', '=', rows[0]['id']],
                           ['move_id.norvale_staging_order_id', '=', pin['production_id']],
                           ['move_id.norvale_staging_receipt', '=', pin['receipt']], ['move_id.is_inventory', '=', True],
                           ['move_id.product_uom', '=', pin['uom']], ['move_id.product_uom_qty', '=', pin['qty']],
                           ['move_id.quantity', '=', pin['qty']]], limit=1)
        if matched != [rows[0]['id']]:
            raise Pending('Native staging receipt differs from the pinned order or allocated stock move')
    confirmation = dict(native_line=rows[0]['id'], native_source_location=rows[0]['location_id'][0])
    entry = ledger[root['key']]
    if 'native_line' not in entry:
        entry.update(confirmation)
        actor.e.persist()
    elif any(entry[k] != value for k, value in confirmation.items()):
        raise Pending('Confirmed MES staging receipt identity changed')
