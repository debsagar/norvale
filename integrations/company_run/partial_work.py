"""Prove current partial work from the original pinned native execution."""
from decimal import Decimal, InvalidOperation


def number(value):
    if isinstance(value, bool):
        raise ValueError('boolean quantity')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('invalid quantity') from exc
    if not result.is_finite():
        raise ValueError('non-finite quantity')
    return result


def partial_work(engine, works, pins, *, include_finished=False):
    evidence = {}
    for work in works:
        try:
            request, dispatch = work['request'], work['dispatch']
            work_id = request['work_order']
            pin = pins[work_id]
            order = engine.api['PLANNER'].get('Work Order', work_id)
            ordered, posted = number(order['qty']), number(order['produced_qty'])
            if not 0 < posted <= ordered or (posted == ordered and not include_finished):
                continue
            authority = engine.company_erp.authority
            cfg, product = authority.route(order['production_item'], dispatch.get('source'), pinned=True)
            mes = authority.execution_reader(cfg, product)
            if pin['mes_source'] != mes.source_id or (dispatch.get('source') and dispatch['source'] != mes.source_id):
                continue
            original = mes.read('mrp.production', int(dispatch['id']))
            group = original['procurement_group_id']
            if (not group or order['bom_no'] != pin['erp']
                    or str(original['bom_id'][0]) != str(pin['mes'])):
                continue
            ids = mes.call('mrp.production', 'search', [['procurement_group_id', '=', group[0]]])
            if not ids or len(ids) != len(set(ids)) or original['id'] not in ids:
                continue
            family = [mes.read('mrp.production', identifier) for identifier in ids]
            family.sort(key=lambda row: row['backorder_sequence'])
            seq = [row['backorder_sequence'] for row in family]
            if (family[0]['id'] != original['id'] or seq[0] not in (0, 1)
                    or seq != list(range(seq[0], seq[0] + len(seq))) or len(family) < 2):
                continue
            item = mes.read('product.product', original['product_id'][0])['default_code']
            unit = cfg['units'][original['product_uom_id'][1]]
            if (order['name'] != work_id or order['docstatus'] != 1 or order.get('status') in ('Cancelled', 'Stopped')
                    or item != order['production_item'] or unit != order['stock_uom']
                    or request['item'] != item or request['uom'] != unit
                    or number(request['qty']) != ordered or number(work['qty']) != ordered
                    or dispatch['version'] != original['norvale_revision']):
                continue
            if any(row['origin'] != work_id or row['procurement_group_id'][0] != group[0]
                   or row['product_id'][0] != original['product_id'][0]
                   or row['product_uom_id'][0] != original['product_uom_id'][0]
                   or row['bom_id'][0] != original['bom_id'][0]
                   or row['norvale_revision'] != original['norvale_revision']
                   or row['company_id'][0] != original['company_id'][0]
                   or row['state'] not in ('done', 'confirmed', 'progress', 'to_close')
                   or number(row['product_qty']) <= 0 for row in family):
                continue
            done = [row for row in family if row['state'] == 'done']
            remaining = [row for row in family if row['state'] != 'done']
            if (not done or len(remaining) > 1
                    or (remaining and family[-1] != remaining[0])
                    or sum((number(row['product_qty']) for row in family), Decimal(0)) != ordered):
                continue
            outputs = []
            for row in done:
                report = mes.completion(row['id'])
                output = report['output']
                if (report['work']['source_id'] != mes.source_id
                        or report['work']['native_id'] != row['id'] or report['erp_work_ref'] != work_id
                        or report['revision'] != original['norvale_revision']
                        or report['bom']['native_id'] != original['bom_id'][0]
                        or output['item_code'] != item or cfg['units'][output['uom_name']] != unit
                        or number(output['quantity']) != number(row['product_qty'])):
                    raise ValueError('completion mismatch')
                outputs.append(number(output['quantity']))
            outstanding = number(remaining[0]['product_qty']) if remaining else Decimal(0)
            if sum(outputs, Decimal(0)) != posted or outstanding != ordered - posted:
                continue
            evidence[work_id] = dict(ordered=float(ordered), posted=float(posted),
                mes_source=mes.source_id, dispatch=original['id'], group=group[0],
                completed=[row['id'] for row in done], backorder=remaining[0]['id'] if remaining else None,
                remaining=float(outstanding), item=item, stock_uom=unit, pin=pin)
        except (KeyError, IndexError, TypeError, ValueError, InvalidOperation, ArithmeticError):
            continue
    return evidence
