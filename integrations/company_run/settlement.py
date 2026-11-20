"""Read-only acceptance of posted cash and independently reconciled bank payments."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import json

from integrations.lake.boundary import project
from integrations.lake.erp_scope import ERP_ANALYTICS_ROLES
from native.build_evidence import INVOICES, matches_bank_clearance, shipment_rows
from native.company_bank import BankReceipts
from native.company_erp import CompanyERP, Pending


FINANCE = ('Sales Invoice', 'Purchase Invoice', 'Payment Entry', 'GL Entry', 'Bank Transaction')


def money(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def posted_gl(invoice, payment, entries, amount, cash_account):
    incoming = invoice['doctype'] == 'Sales Invoice'
    party_account = invoice.get(INVOICES[invoice['doctype']][4])
    if (not cash_account or not party_account or cash_account == party_account
            or payment.get('paid_from' if incoming else 'paid_to') != party_account):
        return []
    expected = {
        cash_account: (1 if incoming else -1, payment.get('received_amount' if incoming else 'paid_amount'),
                       payment.get('paid_to_account_currency' if incoming else 'paid_from_account_currency')),
        party_account: (-1 if incoming else 1, amount,
                        invoice.get('party_account_currency') or invoice.get('currency')),
    }
    verified = []
    for account, (sign, value, currency) in expected.items():
        value = money(value)
        side = [gl for gl in entries if gl.get('account') == account]
        amounts = [(money(gl.get('debit_in_account_currency')), money(gl.get('credit_in_account_currency')))
                   for gl in side]
        if (not currency or not value or value < 0 or not side
                or any(gl.get('account_currency') != currency
                       or gl.get('posting_date') != payment.get('posting_date') for gl in side)
                or any(debit is None or credit is None or debit < 0 or credit < 0
                       or (credit if sign == 1 else debit) != 0 for debit, credit in amounts)
                or sum(sign * (debit - credit) for debit, credit in amounts) != value):
            return []
        if account == party_account and any(
                (gl.get('party_type'), gl.get('party')) != (payment['party_type'], payment['party'])
                for gl in side):
            return []
        verified.extend(gl['name'] for gl in side)
    return sorted(verified)


def reconcile(docs, rows, source, company, *, ordinary, archived, bank_accounts, cash_accounts):
    """Use native records as the universe and compare their current archive projections."""
    latest = {}
    for row in sorted(rows, key=lambda r: r['ingested_at']):
        if row['source'] == source:
            latest[row['entity'], row['native_id']] = row['payload']

    def ref(entity, name):
        return [source, entity, name]

    def confirmed(entity, name):
        doc = docs.get((entity, name))
        if not doc or doc.get('name') != name or doc.get('company') != company:
            return False
        if entity not in archived:
            return not ordinary
        payload = latest.get((entity, name))
        return payload is not None and json.loads(payload) == project(entity, doc)

    ledger, statements, allocations = defaultdict(list), defaultdict(list), defaultdict(list)
    transaction_ids = defaultdict(set)
    for (entity, name), doc in docs.items():
        if doc.get('company') != company:
            continue
        if entity == 'GL Entry' and doc.get('voucher_type') == 'Payment Entry' and doc.get('is_cancelled') == 0:
            ledger[doc['voucher_no']].append(doc)
        if entity == 'Bank Transaction' and doc.get('docstatus') == 1:
            transaction_ids[doc.get('transaction_id')].add(name)
            for allocation in doc.get('payment_entries', []):
                if allocation.get('payment_document') == 'Payment Entry':
                    statements[allocation.get('payment_entry')].append(doc)
        if entity == 'Payment Entry' and doc.get('docstatus') == 1:
            for allocation in doc.get('references', []):
                if allocation.get('reference_doctype') in INVOICES:
                    allocations[allocation['reference_doctype'], allocation.get('reference_name')].append((name, doc, allocation))

    bills, links, problems = [], [], []
    for (entity, name), invoice in sorted(docs.items()):
        if (entity not in INVOICES or invoice.get('company') != company or invoice.get('docstatus') != 1
                or invoice.get('is_return')):
            continue
        party = INVOICES[entity][0]
        sources = shipment_rows(invoice, docs)
        valid_sources = bool(sources) and confirmed(entity, name) and all(
            confirmed(kind, parent) for _, kind, parent, _ in sources)
        total = money((not invoice.get('disable_rounded_total') and invoice.get('rounded_total'))
                      or invoice.get('grand_total'))
        outstanding = money(invoice.get('outstanding_amount'))
        posted = cleared = Decimal(0)
        try:
            terms = {r['payment_term']: r for r in BankReceipts.purchase_terms(invoice).values()}
        except Pending:
            terms = {}
            valid_sources = False
        installments = defaultdict(lambda: [Decimal(0), Decimal(0)])
        payments = []
        for payment_name, payment, allocation in allocations.pop((entity, name), []):
            amount = money(allocation.get('allocated_amount'))
            cash = payment.get('paid_to' if party == 'Customer' else 'paid_from')
            payment_ref = ref('Payment Entry', payment_name)
            term = allocation.get('payment_term')
            record = dict(payment=payment_ref, allocation=allocation.get('name'),
                          amount=str(amount) if amount is not None else None, payment_term=term,
                          posted=False, bank_status='unverified', gl_entries=[], bank_transaction=None)
            payments.append(record)
            try:
                CompanyERP.verify_allocation(invoice, payment, allocation.get('allocated_amount'), cash)
            except Pending:
                problems.append(payment_ref)
                continue
            gl = posted_gl(invoice, payment, ledger[payment_name], amount, cash)
            if (not valid_sources or not allocation.get('name') or amount is None or amount <= 0
                    or not payment.get('posting_date') or payment['posting_date'] < invoice.get('posting_date', '')
                    or cash not in cash_accounts or not confirmed('Payment Entry', payment_name) or not gl
                    or any(not confirmed('GL Entry', entry) for entry in gl)
                    or (terms and term not in terms)):
                problems.append(payment_ref)
                continue
            posted += amount
            installments[term][0] += amount
            record.update(posted=True, bank_status='unconfirmed',
                          gl_entries=[ref('GL Entry', entry) for entry in gl])
            candidates = statements[payment_name]
            if candidates:
                record['bank_status'] = 'unverified'
            if len(candidates) == 1:
                bank = candidates[0]
                if (confirmed('Bank Transaction', bank['name']) and matches_bank_clearance(payment, bank)
                        and bank.get('reference_number') == name
                        and bank.get('party_type') == party and bank.get('party') == payment['party']
                        and bank.get('transaction_id') and bank['transaction_id'] == payment.get('reference_no')
                        and transaction_ids[bank['transaction_id']] == {bank['name']}
                        and payment.get('bank_account') == bank.get('bank_account')
                        and bank_accounts.get(bank.get('bank_account')) == cash
                        and bank.get('date') >= payment.get('posting_date', '')
                        and (not terms or bank['date'] >= terms[term]['due_date'])):
                    cleared += amount
                    installments[term][1] += amount
                    record.update(bank_status='cleared', bank_transaction=ref('Bank Transaction', bank['name']))
            links.append(dict(payment=payment_name, party=party, invoice=name,
                              source_documents=sorted({parent for _, _, parent, _ in sources}),
                              **{k: v for k, v in record.items() if k != 'payment'}))
        schedule = []
        for term, row in terms.items():
            applied, banked = installments[term]
            balance = money(row.get('outstanding'))
            scheduled = money(row.get('payment_amount'))
            if scheduled is None or balance is None or balance < 0 or scheduled - applied != balance:
                valid_sources = False
            schedule.append(dict(id=row['name'], term=term, due_date=row['due_date'],
                                 posted=str(applied), cleared=str(banked), outstanding=row.get('outstanding')))
        if not valid_sources or outstanding is None or total is None or outstanding < 0 or total <= 0:
            status = 'unverified'
        elif total - posted != outstanding:
            status = 'unverified'
        else:
            status = 'paid' if outstanding == 0 else 'partial' if posted else 'unpaid'
        if status == 'unverified':
            problems.append(ref(entity, name))
        bills.append(dict(invoice=ref(entity, name), party=party, status=status,
                          bank_status=('cleared' if status == 'paid' and cleared == posted else
                                       'partial' if cleared else 'unconfirmed'),
                          outstanding=invoice.get('outstanding_amount'), posted=str(posted), cleared=str(cleared),
                          source_rows=[dict(invoice_row=line, document=ref(kind, parent), source_row=detail)
                                       for line, kind, parent, detail in sources],
                          installments=schedule, payments=payments))
    problems.extend(ref('Payment Entry', name) for group in allocations.values() for name, _, _ in group)
    parties = {'Customer', 'Supplier'}
    posted_parties = {bill['party'] for bill in bills if bill['status'] == 'paid'}
    cleared_parties = {bill['party'] for bill in bills if bill['bank_status'] == 'cleared'}
    checks = dict(customer_and_supplier_settled=posted_parties == parties,
                  settled_native_bills=posted_parties == parties and not problems)
    if ordinary:
        checks['customer_and_supplier_bank_cleared'] = cleared_parties == parties and all(
            payment['bank_status'] == 'cleared' for bill in bills for payment in bill['payments']
            if payment['posted'])
    return dict(checks=checks, bills=bills, settlements=links, discrepancies=sorted(set(map(tuple, problems))))


def read_settlements(engine, rows, config):
    """Use scoped accounts for direct reads and the configured source for archive joins."""
    finance = engine.api['FINANCE']
    ordinary = bool(engine.config.get('company_operating'))
    archived = set(config['entities'])
    finance_scans = {entity for entity in FINANCE if config.get('entity_readers', {}).get(entity)}
    archived -= set(FINANCE) - finance_scans
    docs = {}
    for entity in (*FINANCE, 'Purchase Receipt', 'Delivery Note'):
        if entity == 'Bank Transaction' and not ordinary and entity not in archived:
            continue
        client = engine.api[ERP_ANALYTICS_ROLES.get(entity, 'WAREHOUSE')]
        filters = {'company': engine.company}
        if entity == 'GL Entry':
            filters.update(voucher_type='Payment Entry', is_cancelled=0)
        else:
            filters['docstatus'] = 1
        while True:
            page = client.list(entity, filters, limit=500, order_by='name asc')
            for row in page:
                name = row['name']
                docs[entity, name] = dict(client.get(entity, name), doctype=entity)
            if len(page) < 500:
                break
            filters['name'] = ['>', page[-1]['name']]
    cash_accounts = set()
    for name in sorted({doc.get('paid_to' if doc['payment_type'] == 'Receive' else 'paid_from')
                        for (entity, _), doc in docs.items() if entity == 'Payment Entry'
                        and doc.get('payment_type') in ('Receive', 'Pay')} - {None, ''}):
        account = finance.get('Account', name)
        if (account.get('company') == engine.company and not account.get('is_group')
                and account.get('account_type') in ('Cash', 'Bank')):
            cash_accounts.add(name)
    accounts = {}
    for name in sorted({doc['bank_account'] for (entity, _), doc in docs.items()
                        if entity == 'Bank Transaction' and doc.get('bank_account')}):
        account = finance.get('Bank Account', name)
        if (account.get('name') == name and account.get('company') == engine.company
                and account.get('is_company_account')):
            accounts[name] = account.get('account')
    result = reconcile(docs, rows, config['source'], engine.company, ordinary=ordinary,
                       archived=archived, bank_accounts=accounts, cash_accounts=cash_accounts)
    result['documents'] = {kind: sorted(name for entity, name in docs if entity == kind)
                           for kind in ('Sales Invoice', 'Purchase Invoice', 'Payment Entry')}
    return result
