"""Bound staffed operations within one commissioned ERP work order."""
from collections import defaultdict
from fractions import Fraction
from math import isclose
from xmlrpc.client import Fault

from native.company_erp import Pending, stock_steps
from native.production_profiles import production_batch
from .partial_work import partial_work


def backorder(company, work):
    request = work['request']
    source = work['dispatch']['source']
    try:
        revision = work['revision']
        pin = dict(mes_source=source, mes=revision['mes_ack_id'], erp=revision['erp_bom'])
        authority = company.company_erp.authority
        reports = authority.completions(work['dispatch'], as_of=company.date(), role='PRODUCTION')
        posted = {entry['report']['id']: entry['report']
                  for entry in company.company_erp.state['completions'].values()
                  if entry['work'] == work['key'] and entry.get('done') and entry.get('target_id')}
        if {report['id']: report for report in reports} != posted:
            raise Pending('Native completion differs from the posted original ERP work')
        evidence = partial_work(company, [work], {request['work_order']: pin})
    except (Fault, RuntimeError) as exc:
        raise Pending('Native backorder family could not be confirmed') from exc
    remaining = evidence.get(request['work_order'])
    confirmed = {identifier for identifier, operation in work['production_operations'].items()
                 if 'completed_day' in operation}
    if (not remaining or remaining['mes_source'] != source
            or not isclose(remaining['posted'], work['produced'], rel_tol=0, abs_tol=1e-8)
            or set(map(str, remaining['completed'])) != confirmed):
        raise Pending('Native backorder does not match the posted original ERP work')
    if work.get('order') and not company.company_erp.order_covers(work, remaining['remaining'], 'PLANNER'):
        raise Pending('Sales line no longer covers the remaining ERP work')
    return remaining


def queue_operation(company, work, actor):
    policy = actor.cfg.get('production_batch')
    if policy is not None:
        production_batch(policy)
    operations = work.get('production_operations', {})
    for operation in operations.values():
        if operation.get('definitive_stop'):
            return None
        if 'completed_day' not in operation:
            # The operation pin can survive a crash before its clock event is saved.
            return operation['dispatch']
    # A finished native operation can precede ERP posting or its acknowledgement.
    if policy is None or (operations and not work.get('produced')):
        return None
    ack = work['dispatch']
    qty = work['qty']
    inputs = work['inputs']
    group = None
    if work.get('produced'):
        if ('backorder_not_before' not in policy or company.day < policy['backorder_not_before']
                or not operations or any(op['completed_day'] >= company.day for op in operations.values())):
            return None
        remaining = backorder(company, work)
        ack = dict(ack, id=str(remaining['backorder']))
        group, qty = remaining['group'], remaining['remaining']
        used = defaultdict(Fraction)
        for posting in company.company_erp.state['completions'].values():
            if posting['work'] == work['key'] and posting.get('done') and posting.get('target_id'):
                for row in posting['report']['inputs']:
                    used[row['batch']] += Fraction(str(row['qty']))
        inputs = []
        for pin in work['inputs']:
            unused = Fraction(str(pin['qty'])) - used[pin['batch']]
            if unused < 0:
                raise Pending('Posted consumption exceeds the original lot allocation')
            if unused:
                inputs.append(dict(pin, qty=float(unused)))

    target = min(qty, policy['target'], company.config['capacity_units'])
    step = work['revision'].get('stock_step', 1)
    if target <= 0 or target != stock_steps(target, step):
        raise Pending('Commissioned production target differs from the output stock increment')
    if target < qty:
        totals = defaultdict(Fraction)
        for pin in inputs:
            key = pin['item'], pin['uom']
            totals[key] += Fraction(str(pin['qty']))
        for (_, unit), total in totals.items():
            amount = total * Fraction(str(target)) / Fraction(str(qty))
            if amount.denominator != 1 and company.api['PRODUCTION'].get('UOM', unit)['must_be_whole_number']:
                raise Pending('Commissioned target requires a fractional count of a component')

    operation = dict(key=work['key'], dispatch=ack, request=work['request'],
                     item=work['item'], uom=work['uom'], revision=work['revision'],
                     equipment=work['equipment'], qty=qty, inputs=inputs, production_target=target)
    if group is not None:
        operation['native_group'] = group
        original = operations[work['dispatch']['id']]
        if 'mes_stock_location' not in original:
            raise Pending('Original native stock location has not been confirmed')
        operation['mes_stock_location'] = original['mes_stock_location']
    work.setdefault('production_operations', {})[ack['id']] = operation
    company.persist()
    return ack
