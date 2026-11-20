"""Modeled bank statements and Finance reconciliation for ordinary operation."""
from datetime import date, timedelta
import json

from native.company_erp import Pending, digest, quantity

FEED = 'BANK_FEED'
FEED_USER = 'bank_feed@norvale.test'
FEED_ROLE = 'Norvale Bank Feed'
BANK = 'Norvale Clearing Bank'
ACCOUNT = 'Operating EUR - ' + BANK
GL_NAME = 'Operating Bank'
TRANSACTION = 'Bank Transaction'
PAYMENT = 'Payment Entry'
MAPPER = 'erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry'


class BankReceipts:
    def __init__(self, flow):
        self.flow = flow
        self.e = flow.e

    def configure(self):
        """Install the statement importer identity and its native account once."""
        e = self.e
        admin = e.admin
        if not admin.list('Role', {'name': FEED_ROLE}):
            admin.create('Role', {'role_name': FEED_ROLE, 'desk_access': 1})
        for kind in (TRANSACTION, 'Sales Invoice', 'Purchase Invoice'):
            rows = admin.list('Custom DocPerm', {'parent': kind, 'role': FEED_ROLE})
            if not rows:
                result = e.stack.bench('execute', 'frappe.permissions.add_permission',
                                       '--kwargs', json.dumps({'doctype': kind, 'role': FEED_ROLE}))
                if result.returncode:
                    raise RuntimeError(result.stderr[-500:])
                rows = admin.list('Custom DocPerm', {'parent': kind, 'role': FEED_ROLE})
            if kind == TRANSACTION:
                admin.update('Custom DocPerm', rows[0]['name'], {'create': 1, 'write': 1, 'submit': 1})
                # Frappe requires submit permission to update a submitted reconciliation.
                for role in ('Accounts User', 'Accounts Manager'):
                    for perm in admin.list('Custom DocPerm', {'parent': kind, 'role': role}):
                        admin.update('Custom DocPerm', perm['name'],
                                     {'create': 0, 'submit': 1, 'delete': 0, 'cancel': 0, 'share': 0})
        if not admin.list('User', {'name': FEED_USER}):
            admin.create('User', {'email': FEED_USER, 'first_name': 'Bank Feed', 'enabled': 1,
                                  'send_welcome_email': 0, 'roles': [{'role': FEED_ROLE}]})
        if FEED not in e.tokens:
            secret = admin.call('frappe.core.doctype.user.user.generate_keys', {'user': FEED_USER})['api_secret']
            e.tokens[FEED] = admin.get('User', FEED_USER)['api_key'] + ':' + secret
            from native.company import save
            token_path = e.run / 'env/role-tokens.json'
            save(token_path, e.tokens)
            token_path.chmod(0o600)
            e.connect()
        if not admin.list('Bank', {'name': BANK}):
            admin.create('Bank', {'bank_name': BANK})
        if not admin.list('Bank Account', {'name': ACCOUNT}):
            gl_name = f'{GL_NAME} - {e.abbr}'
            if not admin.list('Account', {'name': gl_name}):
                groups = admin.list('Account', {'company': e.company, 'account_type': 'Bank', 'is_group': 1})
                if len(groups) != 1:
                    raise Pending('Expected one bank account group in the operating chart')
                admin.create('Account', {'account_name': GL_NAME, 'company': e.company,
                                         'parent_account': groups[0]['name'], 'account_type': 'Bank',
                                         'account_currency': 'EUR', 'is_group': 0})
            admin.create('Bank Account', {
                'account_name': 'Operating EUR', 'bank': BANK, 'is_company_account': 1,
                'company': e.company, 'account': gl_name,
            })
        field = {'dt': TRANSACTION, 'fieldname': 'custom_norvale_operation'}
        if not admin.list('Custom Field', field):
            admin.create('Custom Field', {**field, 'label': 'Norvale operation', 'fieldtype': 'Data',
                                          'unique': 1, 'no_copy': 1, 'read_only': 1})
        result = e.stack.bench('clear-cache')
        if result.returncode:
            raise RuntimeError(result.stderr[-500:])
        self.flow.state['bank_configured'] = True
        e.persist()

    @staticmethod
    def schedule(seed, invoice_key, invoice, due):
        """A deterministic customer or bank event based on the issued invoice."""
        sample = int(digest([seed, invoice_key])[:8], 16)
        cents = round(100 * (invoice.get('rounded_total') or invoice['grand_total']))
        if cents <= 0:
            return []
        if invoice['doctype'] == 'Purchase Invoice':
            return [(due + timedelta(days=1 + sample % 3), cents)]
        if sample % 11 == 0:
            return []
        if sample % 4 == 0 and cents > 1:
            first = cents // 2
            return [(due + timedelta(days=1), first), (due + timedelta(days=4), cents - first)]
        return [(due + timedelta(days=sample % 3), cents)]

    @staticmethod
    def purchase_terms(invoice):
        rows = invoice.get('payment_schedule', [])
        if invoice['doctype'] != 'Purchase Invoice' or len(rows) < 2:
            return {}
        terms = {r.get('name'): r for r in rows}
        names = {r.get('payment_term') for r in rows}
        if len(terms) != len(rows) or not all(terms) or len(names) != len(rows) or not all(names):
            raise Pending('Supplier installments require distinct native schedule rows and term names')
        return terms

    def import_statements(self):
        e = self.e
        if not self.flow.state.get('bank_configured'):
            self.configure()
        for key, state in e.state['ops']['invoices'].items():
            if state.get('paid') is not None:
                continue
            self.flow.attempt('bank-feed:' + key, FEED, lambda: self.import_invoice(key, state))

    def import_invoice(self, key, state):
        e = self.e
        invoice = e.get(key, FEED)
        if (invoice['docstatus'] != 1 or invoice['company'] != e.company or invoice.get('is_return')
                or invoice['currency'] != 'EUR' or invoice['doctype'] not in ('Sales Invoice', 'Purchase Invoice')):
            raise Pending('Bank feed expected a submitted EUR operating invoice')
        incoming = invoice['doctype'] == 'Sales Invoice'
        party_type = 'Customer' if incoming else 'Supplier'
        due = max(date.fromisoformat(invoice['posting_date']), date.fromisoformat(e.dated(state['due'])))
        terms = self.purchase_terms(invoice)
        if not incoming and invoice.get('on_hold'):
            return
        if terms:
            events = [(row_id, date.fromisoformat(row['due_date']), round(100 * quantity(row['outstanding'], zero=True)))
                      for row_id, row in terms.items()]
        else:
            events = [(index, day, cents) for index, (day, cents) in enumerate(
                self.schedule(e.config['seed'], key, invoice, due))]
        for index, day, cents in events:
            if cents == 0 or day.isoformat() > e.date():
                continue
            operation = f'bank:{self.flow.namespace}:{key}:{index}'
            transaction_id = digest(operation)
            fields = {'date': day.isoformat(), 'company': e.company, 'bank_account': ACCOUNT,
                      'currency': 'EUR', 'deposit': cents / 100 if incoming else 0,
                      'withdrawal': 0 if incoming else cents / 100,
                      'reference_number': invoice['name'], 'transaction_id': transaction_id,
                      'party_type': party_type, 'party': invoice[party_type.lower()]}
            if terms:
                # Persist the first eligible import day. Releasing a hold cannot backdate cash.
                days = self.flow.state.setdefault('payable_transfer_days', {})
                if operation not in days:
                    days[operation] = e.date()
                    e.persist()
                fields.update(date=days[operation], description='Payment Schedule ' + index)

            def verify(doc):
                if doc.get('owner') != FEED_USER or any(doc.get(k) != v for k, v in fields.items()):
                    raise Pending('Bank feed readback differs from the statement event')

            actual = e.write(operation, TRANSACTION, fields, FEED, True, before_submit=verify)
            verify(actual)

    def settle_invoice(self, key, *, overdue):
        e = self.e
        invoice = e.get(key, 'FINANCE')
        kind = invoice['doctype']
        if (kind not in ('Sales Invoice', 'Purchase Invoice') or invoice.get('docstatus') != 1
                or invoice.get('is_return') or invoice.get('company') != e.company):
            raise Pending('Expected a submitted operating invoice for settlement')
        incoming = kind == 'Sales Invoice'
        party_type = 'Customer' if incoming else 'Supplier'
        party = invoice.get(party_type.lower())
        api = e.api['FINANCE']
        bank = api.get('Bank Account', ACCOUNT)
        account = api.get('Account', bank['account'])
        if (bank.get('disabled') or not bank.get('is_company_account') or bank['company'] != e.company
                or account['company'] != e.company or account['account_type'] != 'Bank' or account['is_group']
                or not party or account['account_currency'] != 'EUR'
                or invoice.get('party_account_currency') != 'EUR'):
            raise Pending('Invoice or company bank account is outside the EUR settlement scope')

        scope = {'owner': FEED_USER, 'docstatus': 1, 'bank_account': ACCOUNT, 'company': e.company,
                 'currency': 'EUR', 'party_type': party_type, 'party': party,
                 'reference_number': invoice['name']}
        rows = api.list(TRANSACTION, {**scope, 'date': ['between', [invoice['posting_date'], e.date()]]},
                        limit=0, order_by='date asc, name asc')
        statements = []
        seen = set()
        for row in rows:
            statement = api.get(TRANSACTION, row['name'])
            txid = statement.get('transaction_id')
            if (any(statement.get(k) != v for k, v in scope.items()) or not txid or txid in seen
                    or not invoice['posting_date'] <= statement['date'] <= e.date()
                    or statement.get('withdrawal' if incoming else 'deposit') != 0
                    or statement.get('included_fee') or statement.get('excluded_fee')):
                raise Pending('Bank statement scope, direction or identity differs')
            seen.add(txid)
            amount = quantity(statement.get('deposit' if incoming else 'withdrawal'))
            statements.append((statement, amount))
        payments = [self.reconcile(key, statement, amount, bank['account'])
                    for statement, amount in statements]
        current = e.get(key, 'FINANCE')
        outstanding = quantity(current.get('outstanding_amount'), zero=True)
        if overdue and not payments and outstanding:
            raise Pending('No submitted bank statement for the invoice')
        return payments, outstanding

    def reconcile(self, invoice_key, statement, amount, gl_account):
        e = self.e
        api = e.api['FINANCE']
        invoice = e.get(invoice_key, 'FINANCE')
        key = 'bank-payment:' + statement['transaction_id']
        existing = e.recover_company_doc(key, PAYMENT, 'FINANCE')
        date = statement['date']
        expected = {'posting_date': date, 'reference_date': date, 'reference_no': statement['transaction_id'],
                    'bank_account': ACCOUNT, 'paid_amount': amount, 'received_amount': amount,
                    'paid_from_account_currency': 'EUR', 'paid_to_account_currency': 'EUR'}

        incoming = invoice['doctype'] == 'Sales Invoice'
        expected['paid_from' if incoming else 'paid_to'] = invoice['debit_to' if incoming else 'credit_to']

        terms = self.purchase_terms(invoice)
        term = None
        if terms:
            term = next((row for row_id, row in terms.items()
                         if statement.get('description') == 'Payment Schedule ' + row_id
                         and statement['transaction_id'] == digest(f'bank:{self.flow.namespace}:{invoice_key}:{row_id}')), None)
            if not term or term['due_date'] > date:
                raise Pending('Supplier statement does not identify an eligible installment')

        def verify(doc):
            self.flow.verify_allocation(invoice, doc, amount, gl_account)
            if term and doc['references'][0].get('payment_term') != term['payment_term']:
                raise Pending('Supplier payment refers to another installment')
            if any(doc.get(k) != v for k, v in expected.items()) or doc.get('deductions'):
                raise Pending('Payment entry differs from the bank statement')

        def verify_statement(doc, payment):
            refs = doc.get('payment_entries', [])
            if (doc['docstatus'] != 1 or doc['status'] != 'Reconciled' or doc['unallocated_amount'] != 0
                    or len(refs) != 1 or refs[0]['payment_document'] != PAYMENT
                    or refs[0]['payment_entry'] != payment['name'] or refs[0]['allocated_amount'] != amount
                    or payment.get('clearance_date') != date):
                raise Pending('Bank reconciliation could not be confirmed')

            # Retain the dated native evidence used by the cutoff reference builder.
            for record, fields in ((doc, ('doctype', 'name', 'docstatus', 'status', 'date', 'currency',
                                          'deposit', 'withdrawal', 'unallocated_amount', 'payment_entries')),
                                   (payment, ('doctype', 'name', 'docstatus', 'party', 'posting_date',
                                              'payment_type', 'paid_amount', 'received_amount',
                                              'paid_from_account_currency', 'paid_to_account_currency',
                                              'references', 'clearance_date'))):
                e.log('bank_clearance_readback', role='FINANCE', doc={k: record[k] for k in fields if k in record})

        if statement.get('payment_entries'):
            if not existing or existing.get('docstatus') != 1:
                raise Pending('Statement is already allocated to a different voucher')
            verify(existing)
            verify_statement(statement, existing)
            return existing['name']
        if statement.get('unallocated_amount') != amount or statement.get('status') != 'Unreconciled':
            raise Pending('Statement does not have the expected unallocated amount')

        def before_submit(draft):
            verify(draft)
            if api.get(TRANSACTION, statement['name']) != statement:
                raise Pending('Bank statement changed before payment submission')
            current = e.get(invoice_key, 'FINANCE')
            if not incoming and current.get('on_hold'):
                raise Pending('Finance cannot disburse a held supplier invoice')
            if term:
                row = self.purchase_terms(current).get(term['name'])
                if not row or row['payment_term'] != term['payment_term'] or row['outstanding'] != amount:
                    raise Pending('Supplier installment changed before disbursement')
            if current['docstatus'] != 1 or quantity(current['outstanding_amount'], zero=True) < amount:
                raise Pending('Bank statement exceeds the invoice balance')

        if existing:
            entry = existing
        else:
            if quantity(invoice['outstanding_amount'], zero=True) < amount:
                raise Pending('Bank statement exceeds the invoice balance')
            entry = e.mapped('FINANCE', MAPPER, dt=invoice['doctype'], dn=invoice['name'],
                             bank_account=gl_account, party_amount=amount, bank_amount=amount,
                             reference_date=date)
            if term:
                refs = [r for r in entry['references'] if r.get('payment_term') == term['payment_term']]
                if len(refs) != 1:
                    raise Pending('ERP payment template lacks native installment allocation')
                entry.update(references=[dict(refs[0], allocated_amount=amount)], paid_amount=amount, received_amount=amount)
            entry.update(posting_date=date, reference_date=date, reference_no=statement['transaction_id'],
                         bank_account=ACCOUNT)
        verify(entry)
        payment = e.write(key, PAYMENT, entry, 'FINANCE', True, before_submit=before_submit)
        verify(payment)
        if term and self.purchase_terms(e.get(invoice_key, 'FINANCE'))[term['name']]['outstanding'] != 0:
            raise Pending('Native supplier installment balance has not cleared')
        api.call('erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool.reconcile_vouchers',
                 {'bank_transaction_name': statement['name'],
                  'vouchers': json.dumps([{'payment_doctype': PAYMENT, 'payment_name': payment['name']}])})
        verify_statement(api.get(TRANSACTION, statement['name']), api.get(PAYMENT, payment['name']))
        e.log('bank_payment_reconciled', invoice=invoice['name'], statement=statement['name'],
              payment=payment['name'], amount=amount, statement_date=date)
        return payment['name']
