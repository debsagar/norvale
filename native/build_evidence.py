"""Read-only reference builder. No engine imports, network calls or run writes."""
import argparse
from datetime import date,timedelta
from collections import Counter
import hashlib
import json
import os
from pathlib import Path


def load(run):
    state = json.loads((run / 'state.json').read_text())
    config = json.loads((run / 'config.json').read_text())
    trace = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines() if line.strip()]
    if not state.get('completed') or not trace:
        raise ValueError('No completed stages and trace available; integration/run is pending')
    return state, config, trace


def outside(run, out):
    if out.resolve() == run.resolve() or run.resolve() in out.resolve().parents:
        raise ValueError('Output must be outside the run directory')


def matches_bank_clearance(voucher, statement):
    """The native voucher must be the exact fully reconciled statement allocation."""
    incoming = voucher.get('payment_type') == 'Receive'
    amount = voucher.get('received_amount' if incoming else 'paid_amount')
    currency = voucher.get('paid_to_account_currency' if incoming else 'paid_from_account_currency')
    refs = statement.get('payment_entries') or []
    return (statement.get('docstatus') == 1 and statement.get('status') == 'Reconciled'
            and statement.get('unallocated_amount') == 0 and voucher.get('docstatus') == 1
            and voucher.get('clearance_date') and statement.get('date') == voucher['clearance_date']
            and len(refs) == 1 and refs[0].get('payment_document') == 'Payment Entry'
            and refs[0].get('payment_entry') == voucher['name']
            and voucher.get('payment_type') in ('Pay', 'Receive')
            and currency and statement.get('currency') == currency
            and (amount or 0) > 0 and refs[0].get('allocated_amount') == amount
            and statement.get('deposit' if incoming else 'withdrawal') == amount
            and statement.get('withdrawal' if incoming else 'deposit') == 0)


INVOICES = {
    'Sales Invoice': ('Customer', 'Delivery Note', 'delivery_note', 'dn_detail', 'debit_to'),
    'Purchase Invoice': ('Supplier', 'Purchase Receipt', 'purchase_receipt', 'pr_detail', 'credit_to'),
}

def shipment_rows(invoice, docs):
    party, entity, parent_field, row_field, _ = INVOICES[invoice['doctype']]
    result = []
    used = set()
    for row in invoice.get('items', []):
        parent = docs.get((entity, row.get(parent_field)), {})
        detail = row.get(row_field)
        identity = (row.get(parent_field), detail)
        matches = [line for line in parent.get('items', []) if line.get('name') == detail]
        if (parent.get('docstatus') != 1 or parent.get('is_return') or invoice.get('update_stock')
                or not row.get('name') or not detail or identity in used or len(matches) != 1
                or any(not invoice.get(k) or invoice[k] != parent.get(k)
                       for k in ('company', party.lower(), 'currency'))
                or any(row.get(k) is None or row[k] != matches[0].get(k)
                       for k in ('item_code', 'uom', 'qty', 'rate'))):
            return []
        used.add(identity)
        result.append((row['name'], entity, parent['name'], detail))
    return result


def sales_orders(doc):
    field = 'against_sales_order' if doc.get('doctype') == 'Delivery Note' else 'sales_order'
    return {item[field] for item in doc.get('items') or [] if item.get(field)}


def sales_allocations(doc):
    return [(index, ref) for index, ref in enumerate(doc.get('references') or [])
            if ref.get('reference_doctype') == 'Sales Invoice' and ref.get('reference_name')]


def sales_facts(documents, *, system="erp"):
    """Project one observable ERP snapshot using typed order and line references.

    Scope each call to one instance. Day order comes from native business
    dates. Use a snapshot retained at the desired cutoff for historical grading.
    """
    orders = documents.get('Sales Order', {})
    deliveries = documents.get('Delivery Note', {})
    invoices = documents.get('Sales Invoice', {})

    def fact(kind, native_id, doc, **fields):
        return dict(kind=kind, system=system, native_id=native_id,
                    at=date.fromisoformat(doc['posting_date']).toordinal(), **fields)

    def line_id(delivery, line):
        return json.dumps([delivery, line], separators=(',', ':'))

    for name, invoice in invoices.items():
        if invoice.get('docstatus') != 1 or invoice.get('is_return'):
            continue
        items = invoice.get('items') or []
        cases = sorted(sales_orders(invoice).intersection(orders))
        for order_id in cases:
            order = orders[order_id]
            case = order_id if system == 'erp' else f'{system}/{order_id}'
            if (order.get('docstatus') != 1 or not invoice.get('company') or not invoice.get('customer')
                    or order.get('company') != invoice['company']
                    or order.get('customer') != invoice['customer']):
                continue
            links = []
            order_lines = {line['name']: line for line in order.get('items', [])}
            for item in items:
                if item.get('sales_order') != order_id:
                    continue
                delivery_name, row_name = item.get('delivery_note'), item.get('dn_detail')
                delivery = deliveries.get(delivery_name, {})
                actual = next((r for r in delivery.get('items', []) if r['name'] == row_name), {})
                agreed = order_lines.get(item.get('so_detail'), {})
                if (delivery.get('docstatus') != 1 or delivery.get('is_return')
                        or delivery.get('posting_date', '') > invoice['posting_date']
                        or any(delivery.get(k) != invoice.get(k) for k in ('company', 'customer', 'currency'))
                        or actual.get('against_sales_order') != order_id or not agreed
                        or actual.get('so_detail') != item.get('so_detail')
                        or any(not item.get(k) or item[k] != actual.get(k) or item[k] != agreed.get(k)
                               for k in ('item_code', 'stock_uom'))
                        or item.get('stock_qty') != actual.get('stock_qty')):
                    links.append(None)
                    continue
                key = line_id(delivery_name, row_name)
                links.append(key)
                yield fact('shipment', key, delivery, case=case, quantity=actual.get('stock_qty'),
                           uom=actual['stock_uom'])

            # A document total or payment allocation spanning several orders
            # cannot be apportioned to one order from the header alone.
            whole = all(item.get('sales_order') == order_id for item in items)
            currency = invoice.get('currency')
            key = name if whole else line_id(name, case)
            amount = invoice.get('rounded_total') if not invoice.get('disable_rounded_total') else None
            if amount is None:
                amount = invoice.get('grand_total')
            yield fact('invoice', key, invoice, case=case, currency=currency,
                       amount=amount if whole and currency else None, shipment_id=links)

    for name, payment in documents.get('Payment Entry', {}).items():
        if payment.get('docstatus') != 1 or payment.get('payment_type') != 'Receive':
            continue
        for index, ref in sales_allocations(payment):
            invoice_id = ref['reference_name']
            invoice = invoices.get(invoice_id)
            if invoice is None:
                continue
            currency = payment.get('paid_from_account_currency')
            verified = (payment.get('party_type') == 'Customer'
                        and payment.get('party') and payment['party'] == invoice.get('customer')
                        and payment.get('company') and payment['company'] == invoice.get('company')
                        and currency and currency == (invoice.get('party_account_currency')
                                                      or invoice.get('currency'))
                        and ref.get('exchange_rate', 1) == 1)
            yield fact('payment', line_id(name, ref.get('name') or index), payment, invoice_id=invoice_id,
                       currency=currency, amount=ref.get('allocated_amount') if verified else None)


def build(run, cutoff):
    state, config, trace = load(run)
    if cutoff > state['day']:
        raise ValueError('Cutoff exceeds saved run day')
    docs = {}
    for index, row in enumerate(trace, 1):
        if row.get('kind') in ('readback', 'bank_clearance_readback') and row['day'] <= cutoff:
            doc = row['doc']
            identity = doc['doctype'], doc['name']
            if row['kind'] == 'bank_clearance_readback' and identity in docs:
                row = {**row, 'doc': {**docs[identity][1]['doc'], **doc}}
            docs[identity] = (index, row)
    questions=[]
    def candidates(dt):
        return sorted((v for (kind,_),v in docs.items() if kind==dt),key=lambda v:(v[1]['day'],v[1]['doc']['name']))
    def evidence(v):
        line,row=v;doc=row['doc']
        return {'system':'ERPNext','doctype':doc['doctype'],'native_id':doc['name'],'observed_day':row['day'],'trace_locator_private':f'trace.jsonl:{line}'}
    def add(domain,level,question,answer,records,answerability='answerable'):
        questions.append({'id':f'R{len(questions)+1:02}', 'domain':domain,'difficulty':level,'cutoff_day':cutoff,
            'question':question,'answerability':answerability,'reference_answer':answer,
            'rubric':{'claims':[{'claim':k,'expected':v,'evidence':[evidence(r) for r in records]} for k,v in answer.items()],
                'grading':'Grade each claim separately. Require native IDs, cutoff and uncertainty; posted records do not independently prove physical completion or causality.'}})
    # Deliberate business coverage and early/middle/late selection, never random wording.
    for dt,domain,fraction,fields,question in [
        ('Sales Order','sales',0,('customer','transaction_date','delivery_date','items'),'What did this customer order, and when was delivery promised?'),
        ('Purchase Order','purchasing',.5,('supplier','transaction_date','schedule_date','grand_total','workflow_state'),'What supplier commitment and approval state were recorded?'),
        ('Job Card','operations',1,('work_order','operation','employee','time_logs','total_completed_qty'),'Who recorded work on this operation, for how long and what output?'),
        ('Payment Entry','finance',1,('party','posting_date','paid_amount','references'),'What payment was booked and which invoices did it settle?')]:
        rows=candidates(dt)
        if rows:
            chosen=rows[round((len(rows)-1)*fraction)];doc=chosen[1]['doc']
            add(domain,'easy',f"{question} Use {dt} {doc['name']} as of the cutoff.",{k:doc.get(k) for k in fields},[chosen])
    chains=[('Work Order','production',('Sales Order','Stock Entry','Job Card','Quality Inspection'),'Reconstruct the customer demand, authorized work, reported labor, manufacturing posting and inspection linked to this job.'),
            ('Purchase Order','procure-to-pay',('Purchase Receipt','Purchase Invoice','Payment Entry'),'Reconstruct approval, receipt, billing and settlement for this purchase. Distinguish missing steps from completed steps.'),
            ('Sales Order','order-to-cash',('Delivery Note','Sales Invoice','Payment Entry'),'Reconstruct the dispatch, billing and payment history for this customer order, including open work.')]
    for dt,domain,types,prompt in chains:
        rows=candidates(dt)
        if not rows:continue
        selected=rows[len(rows)//2];linked=[selected];ids={selected[1]['doc']['name']}
        if dt=='Work Order':
            order=selected[1]['doc'].get('sales_order')
            linked.extend(r for r in candidates('Sales Order') if r[1]['doc']['name']==order)
            ids.update(r[1]['doc']['name'] for r in linked)
        for _ in range(4):
            for kind in types:
                for row in candidates(kind):
                    doc=row[1]['doc']
                    def refs(value):
                        if isinstance(value,dict):return any(refs(v) for v in value.values())
                        if isinstance(value,list):return any(refs(v) for v in value)
                        return isinstance(value,str) and value in ids
                    if dt == 'Sales Order':
                        order_id = selected[1]['doc']['name']
                        if kind == 'Payment Entry':
                            bills = {entry[1]['doc']['name'] for entry in linked
                                     if entry[1]['doc']['doctype'] == 'Sales Invoice'}
                            matches = any(ref['reference_name'] in bills
                                          for _, ref in sales_allocations(doc))
                        else:
                            matches = order_id in sales_orders(doc)
                    else:
                        matches = refs(doc)
                    if row not in linked and matches:
                        linked.append(row)
                        ids.add(doc['name'])
        add(domain,'medium',prompt+' Start with '+selected[1]['doc']['name']+'.',
            {'recorded_sequence':[{'doctype':r[1]['doc']['doctype'],'native_id':r[1]['doc']['name'],'business_date':r[1]['doc'].get('posting_date',r[1]['doc'].get('transaction_date',r[1]['doc'].get('planned_start_date'))),'observed_day':r[1]['day'],'status':r[1]['doc'].get('status')} for r in sorted(linked,key=lambda r:r[1]['doc'].get('posting_date',r[1]['doc'].get('transaction_date',r[1]['doc'].get('creation',''))))],
             'not_established':'Unlinked or absent steps and physical receipt beyond the available records.'},linked)
    policies=[v for v in candidates('Note') if 'policy' in v[1]['doc'].get('title','').lower() or 'mandate' in v[1]['doc'].get('title','').lower()]
    if policies:
        authority=[]
        for v in policies:
            content=v[1]['doc'].get('content')
            try:dates=json.loads(content)
            except (TypeError,ValueError):dates={}
            end=dates.get('review_date');start=dates.get('effective_date');asof=str(date(2027,1,1)+timedelta(days=cutoff))
            authority.append({'native_id':v[1]['doc']['name'],'content':content,'within_recorded_effective_interval':start<=asof<end if start and end else None})
        add('authority','hard','Which dated mandates or policy revisions could govern purchasing at the cutoff, and do the records establish their implementation?',
            {'recorded_authority':authority,
             'implementation_limit':'A policy note establishes authorization, not downstream compliance. Compare effective dates, scope and subsequent native workflow outcomes before claiming implementation.'},policies,'bounded')
    production=candidates('Work Order')
    if production:
        add('causal-explanation','hard','Do the available records identify how much earlier customer orders would have completed without supplier delays?',
            {'answer':'Insufficient evidence: the realized trajectory contains competing inventory, capacity, maintenance and approval constraints; no controlled counterfactual is observed.',
             'required_evidence':'Dated order, material, approval, receipt, capacity and service evidence can support contributing mechanisms, not a uniquely identified causal effect.'},[production[len(production)//2]],'insufficient')
    if candidates('Payment Entry'):
        payment = candidates('Payment Entry')[-1]
        voucher = payment[1]['doc']
        incoming = voucher.get('payment_type') == 'Receive'
        amount = voucher.get('received_amount' if incoming else 'paid_amount')
        currency = voucher.get('paid_to_account_currency' if incoming else 'paid_from_account_currency')
        matches = [row for row in candidates('Bank Transaction')
                   if matches_bank_clearance(voucher, row[1]['doc'])]
        question = f"What bank-statement evidence supports clearance of Payment Entry {voucher['name']} by the cutoff?"
        if len(matches) == 1:
            statement = matches[0][1]['doc']
            add('financial-assurance','hard',question,
                {'bank_transaction':statement['name'], 'cleared_on':statement['date'],
                 'allocated_amount':amount, 'currency':currency},
                [payment, matches[0]])
        else:
            add('financial-assurance','hard',question,
                {'answer':'The observed records do not establish one reconciled bank statement for this payment at the cutoff.'},
                [payment, *matches], 'insufficient')
    ticket_rows=[r for r in trace if r.get('kind')=='ticket_readback' and r['day']<=cutoff]
    mail_rows=[r for r in trace if r.get('kind')=='native_mail_readback' and r['day']<=cutoff]
    if ticket_rows:
        chosen=ticket_rows[len(ticket_rows)//2];ticket=chosen['record'];number=ticket['number']
        history=[r for r in ticket_rows if r['record']['number']==number]
        messages=[r for r in mail_rows if 'Filler unavailable' in r['message'].get('subject','') and min(v['day'] for v in history)<=r['day']<=max(v['day'] for v in history)]
        refs=[{'system':'Gitea','native_id':number,'observed_day':r['day'],'state':r['record']['state']} for r in history]
        refs += [{'system':'mail','native_id':r['message'].get('message_id',r['message'].get('subject')),'observed_day':r['day']} for r in messages]
        questions.append({'id':f'R{len(questions)+1:02}','domain':'maintenance','difficulty':'medium','cutoff_day':cutoff,'answerability':'bounded',
            'question':f'Reconstruct the service request and resolution for ticket {number}. Which ERP task and notification support the sequence? Distinguish business dates in the text from ticket server timestamps.',
            'reference_answer':{'ticket_body':ticket.get('body'),'recorded_states':refs,'mail_subjects':[r['message']['subject'] for r in messages], 'uncertainty':'Temporal proximity alone does not prove a mail belongs to this ticket; require the ERP task/body reference. Ticket server timestamps may differ from simulated business dates.'},
            'rubric':{'claims':[{'claim':'Native request, linked task and recorded closure sequence','evidence':refs},{'claim':'Do not infer physical restart from ticket closure alone','evidence':refs}]}})
        questions.append({'id':f'R{len(questions)+1:02}','domain':'consequential-outcome','difficulty':'hard','cutoff_day':cutoff,'answerability':'bounded',
            'question':f'What evidence supports maintenance as a constraint on production around ticket {number}, and what would be needed to attribute a particular customer delay to it?',
            'reference_answer':{'supported_mechanism':'The service request and closure delimit a recorded equipment intervention. The ERP task and notification support the reported interruption.', 'not_established':'Ticket closure alone does not establish actual restart, or the number of customer-delay days caused by maintenance. Compare dated work orders, job cards, shipment promises, inventory and approvals before attributing outcomes.'},
            'rubric':{'claims':[{'claim':'Identify dated service interval and linked ERP task without equating server timestamps with business dates','evidence':refs},{'claim':'Distinguish supported equipment constraint from unproven customer-delay attribution','evidence':refs}]}})
    if mail_rows:
        r=mail_rows[0];msg=r['message']
        questions.append({'id':f'R{len(questions)+1:02}','domain':'communications','difficulty':'easy','cutoff_day':cutoff,'answerability':'answerable','question':'What request was delivered in the earliest recorded approval email, and which native purchase does its body identify?',
            'reference_answer':{'subject':msg.get('subject'),'body':msg.get('body'),'sender':r['sender'],'recipient':r['recipient']},'rubric':{'claims':[{'claim':'Delivered content and recipient; sending is not approval','evidence':[{'system':'mail','native_id':msg.get('message_id',msg.get('subject')),'observed_day':r['day']}]}]}})
    if not questions:
        raise ValueError('No supported native readbacks at cutoff')
    for q in questions:q['cutoff_date']=str(date(2027,1,1)+timedelta(days=cutoff))
    return {'cutoff_day': cutoff, 'cutoff_date':str(date(2027,1,1)+timedelta(days=cutoff)), 'configured_days': config.get('days'), 'questions': questions,
            'limits': ['Deliberate early/middle/late cases across business domains; small development reference, not client validation.',
                       'Missing process domains are omitted, never fabricated.',
                       'Runtime shocks/interventions are execution provenance, not identified causal effects.',
                       'ERP posted dispatch/receipt does not prove physical delivery/arrival.',
                       'ERP, delivered mail and ticket readbacks support scoped questions; private trace locators are provenance and must not be released as extractor input.'],
            'sources': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (run/'state.json', run/'config.json', run/'trace.jsonl')}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=Path(__file__).parent/'recurring-run')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cutoff', type=int, required=True)
    args = parser.parse_args()
    outside(args.run, args.out)
    artifact = build(args.run, args.cutoff)
    args.out.mkdir(parents=True, exist_ok=True)
    private = args.out/'private'
    private.mkdir(exist_ok=True, mode=0o700)
    private.chmod(0o700)
    target = private/'reference.json'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(artifact, stream, indent=2)
    public = {'distribution': dict(Counter((q['domain']+':'+q['difficulty']+':'+q['answerability']) for q in artifact['questions'])),
              'examples': [{k:q[k] for k in ('difficulty','question','cutoff_day','answerability')} for level in ('easy','medium','hard') for q in artifact['questions'] if q['difficulty']==level][:9],
              'limits': artifact['limits']}
    (args.out/'public-examples.json').write_text(json.dumps(public, indent=2))


if __name__ == '__main__':
    main()
