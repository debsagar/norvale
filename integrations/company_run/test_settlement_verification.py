"""Offline qualification of source-scoped settlement at the company verifier boundary."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from integrations.company_run.settlement import reconcile, read_settlements
from integrations.lake.boundary import project


def fixture():
    docs = {}

    def add(kind, name, **fields):
        doc = dict(doctype=kind, name=name, company='Norvale', modified='2027-01-10', **fields)
        docs[kind, name] = doc
        return doc

    for party, bill, source, parent, line, trade, values in (
        ('Customer', 'Sales Invoice', 'Delivery Note', 'delivery_note', 'dn_detail', 'Debtors', [10.04]),
        ('Supplier', 'Purchase Invoice', 'Purchase Receipt', 'purchase_receipt', 'pr_detail', 'Creditors', [1.08, 2.16]),
    ):
        incoming = party == 'Customer'
        party_field = {party.lower(): party}
        item = dict(item_code='SKU', uom='Each', qty=2, rate=2)
        add(source, 'D', docstatus=1, currency='EUR', items=[dict(name='L', **item)], **party_field)
        add(bill, 'I', docstatus=1, currency='EUR', grand_total=sum(values), outstanding_amount=0,
            posting_date='2027-01-01', items=[dict(name='IL', **item, **{parent:'D', line:'L'})],
            **{**party_field, 'debit_to' if incoming else 'credit_to': trade})
        schedule = []
        for i, value in enumerate(values):
            key = party + str(i)
            term = 'Term' + str(i)
            date = f'2027-01-0{i+2}'
            schedule.append(dict(name='Schedule'+str(i), payment_term=term, payment_amount=value,
                                 outstanding=0, due_date=date))
            add('Payment Entry', key, docstatus=1, posting_date=date, clearance_date=date,
                payment_type='Receive' if incoming else 'Pay', party_type=party, party=party,
                paid_amount=value, received_amount=value, paid_from_account_currency='EUR',
                paid_to_account_currency='EUR', paid_from=trade if incoming else 'Bank',
                paid_to='Bank' if incoming else trade, bank_account='Main', reference_no=key,
                references=[dict(name='Allocation'+key, reference_doctype=bill, reference_name='I',
                                 allocated_amount=value, payment_term=term)])
            add('Bank Transaction', key, docstatus=1, status='Reconciled', unallocated_amount=0,
                date=date, currency='EUR', bank_account='Main', reference_number='I',
                transaction_id=key, party_type=party, party=party, deposit=value if incoming else 0,
                withdrawal=0 if incoming else value, payment_entries=[
                    dict(name='BankAllocation'+key, payment_document='Payment Entry', payment_entry=key,
                         allocated_amount=value)])
            for cash in (True, False):
                debit = incoming == cash
                add('GL Entry', key+str(cash), is_cancelled=0, posting_date=date,
                    voucher_type='Payment Entry', voucher_no=key, account='Bank' if cash else trade,
                    account_currency='EUR', debit_in_account_currency=value if debit else 0,
                    credit_in_account_currency=0 if debit else value,
                    **({} if cash else {'party_type':party, 'party':party}))
        if len(schedule) > 1:
            docs[bill, 'I']['payment_schedule'] = schedule
    return docs


def archive(docs, source='erp'):
    return [dict(source=source, entity=kind, native_id=name, ingested_at=1,
                 payload=json.dumps(project(kind, d))) for (kind, name), d in docs.items()]


class SettlementVerificationTests(unittest.TestCase):
    def setUp(self):
        self.docs = fixture()

    def check(self, rows=None, ordinary=True, archived=None):
        return reconcile(self.docs, archive(self.docs) if rows is None else rows, 'erp', 'Norvale',
                         ordinary=ordinary, archived=archived if archived is not None else {
                             kind for kind, _ in self.docs}, bank_accounts={'Main':'Bank'}, cash_accounts={'Bank'})

    def test_each_paid_side_requires_exact_rows_gl_and_bank(self):
        result = self.check()
        self.assertTrue(all(result['checks'].values()), result)
        self.assertEqual(len(result['settlements']), 3)
        supplier = result['bills'][0]
        self.assertEqual(supplier['source_rows'][0]['document'], ['erp','Purchase Receipt','D'])
        self.assertEqual([p['cleared'] for p in supplier['installments']], ['1.08','2.16'])
        for kind, name, change in (
            ('Sales Invoice', 'I', {'items': [dict(name='IL', delivery_note='D', dn_detail='wrong')]}),
            ('GL Entry', 'Customer0True', {'is_cancelled': 1}),
            ('GL Entry', 'Customer0False', {'party': 'another customer'}),
            ('Bank Transaction', 'Supplier1', {'docstatus': 0}),
            ('Bank Transaction', 'Supplier1', {'status': 'Unreconciled'}),
            ('Bank Transaction', 'Supplier1', {'reference_number': 'other'}),
            ('Bank Transaction', 'Supplier1', {'transaction_id': 'other'}),
            ('Bank Transaction', 'Supplier1', {'bank_account': 'other'}),
        ):
            with self.subTest(kind=kind, change=change):
                self.docs = fixture()
                self.docs[kind, name].update(change)
                result = self.check()
                self.assertFalse(result['checks']['customer_and_supplier_bank_cleared'])
                if kind == 'Bank Transaction':
                    self.assertTrue(result['checks']['settled_native_bills'])
        self.docs = fixture()
        extra = dict(self.docs['Bank Transaction','Supplier1'], name='Duplicate')
        self.docs['Bank Transaction','Duplicate'] = extra
        self.assertFalse(self.check()['checks']['customer_and_supplier_bank_cleared'])

    def test_payments_and_installments_preserve_unpaid_partial_and_uncleared(self):
        self.docs['Sales Invoice','I']['outstanding_amount'] = 10.04
        del self.docs['Payment Entry','Customer0']
        self.docs['Purchase Invoice','I']['outstanding_amount'] = 2.16
        self.docs['Purchase Invoice','I']['payment_schedule'][1]['outstanding'] = 2.16
        del self.docs['Payment Entry','Supplier1']
        supplier, customer = self.check()['bills']
        self.assertEqual((supplier['status'], supplier['bank_status']), ('partial','partial'))
        self.assertEqual((customer['status'], customer['bank_status']), ('unpaid','unconfirmed'))
        self.assertEqual(supplier['installments'][1]['outstanding'], 2.16)
        self.docs['Payment Entry','Supplier1'] = fixture()['Payment Entry','Supplier1']
        self.docs['Purchase Invoice','I']['outstanding_amount'] = 0
        self.docs['Purchase Invoice','I']['payment_schedule'][1]['outstanding'] = 0
        del self.docs['Bank Transaction','Supplier1']
        supplier = self.check()['bills'][0]
        self.assertEqual((supplier['status'], supplier['bank_status']), ('paid','partial'))
        self.assertEqual((supplier['posted'], supplier['cleared']), ('3.24','1.08'))

    def test_only_current_source_archive_projections_count(self):
        for entity, name in [('Delivery Note','D'), ('Purchase Invoice','I'), ('GL Entry','Supplier0True'),
                             ('Bank Transaction','Supplier1'), ('Payment Entry','Customer0')]:
            with self.subTest(entity=entity):
                rows = archive(self.docs)
                row = next(r for r in rows if (r['entity'],r['native_id']) == (entity,name))
                row['source'] = 'other'
                stale = deepcopy(self.docs[entity,name])
                stale['modified'] = 'yesterday'
                rows.extend(archive({(entity,name): stale}))
                self.assertFalse(self.check(rows)['checks']['customer_and_supplier_bank_cleared'])

    def test_legacy_cash_and_scoped_native_reads(self):
        for key in list(self.docs):
            if key[0] == 'Bank Transaction':
                del self.docs[key]
        roles = {'FINANCE': [], 'WAREHOUSE': [], 'BUYER': []}

        def client(role):
            def ls(entity, filters, **kwargs):
                roles[role].append(entity)
                self.assertEqual(filters['company'], 'Norvale')
                return [{'name':name} for kind, name in self.docs if kind == entity]
            def get(kind, name):
                if kind == 'Account':
                    self.assertEqual(role, 'FINANCE')
                    return dict(company='Norvale', account_type='Cash', is_group=0)
                return self.docs[kind, name]
            return SimpleNamespace(list=ls, get=get)

        engine = SimpleNamespace(api={role:client(role) for role in roles}, config={}, company='Norvale')
        result = read_settlements(engine, archive(self.docs), dict(source='erp', entities=['Delivery Note']))
        self.assertTrue(all(result['checks'].values()), result)
        self.assertEqual(roles['FINANCE'], ['Sales Invoice','Purchase Invoice','Payment Entry','GL Entry'])
        self.assertEqual(roles['WAREHOUSE'], ['Delivery Note'])
        self.assertEqual(roles['BUYER'], ['Purchase Receipt'])
        self.assertTrue(all(bill['bank_status'] == 'unconfirmed' for bill in result['bills']))
