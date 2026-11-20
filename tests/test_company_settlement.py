"""Finance checks through the recurring settlements phase."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from native.company import Engine
from native.company_erp import CompanyERP


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.invoice = dict(doctype='Sales Invoice',name='INV',docstatus=1,company='Company',customer='Acme',
                            outstanding_amount=24.5,party_account_currency='EUR')
        self.entry = dict(payment_type='Receive',company='Company',party_type='Customer',party='Acme',
                          paid_from_account_currency='EUR',paid_amount=24.5,paid_to='Cash - NCC',
                          references=[dict(reference_doctype='Sales Invoice',reference_name='INV',allocated_amount=24.5)])
        self.saved = {}
        self.e = SimpleNamespace(company='Company', abbr='NCC', config={}, day=2, date=lambda:'2027-01-03',docs={'invoice':{}},
                                 state={'ops':{'invoices':{'invoice':{'due':2}}}}, persist=Mock(),log=Mock())
        self.e.company_erp = CompanyERP(self.e,None)
        self.e.get = lambda key,role: dict(self.invoice)
        self.e.recover_company_doc = lambda key,dt,role: self.saved.get(key)
        self.e.mapped = Mock(side_effect=lambda *a,**kw:copy.deepcopy(self.entry))
        self.e.write = Mock(side_effect=self.write)

    def write(self,key,dt,doc,role,submit,*,before_submit):
        if key not in self.saved:
            self.saved[key] = dict(doc,name='PAY',docstatus=0)
        actual = self.saved[key]
        if not actual['docstatus']:
            before_submit(actual)
            actual['docstatus'] = 1
            self.invoice['outstanding_amount'] = 0
        return actual

    def test_lost_submit_reply_recovers_without_another_mapper_or_payment(self):
        def lost(*args,**kw):
            self.write(*args,**kw)
            raise TimeoutError('response lost')
        self.e.write.side_effect = lost
        Engine.settlements(self.e)
        self.assertNotIn('paid',self.e.state['ops']['invoices']['invoice'])
        self.e.write.side_effect = self.write
        self.e.company_erp = CompanyERP(self.e,None)
        Engine.settlements(self.e)
        self.assertEqual(self.e.state['ops']['invoices']['invoice'],dict(due=2,paid=2,payment='PAY'))
        self.e.mapped.assert_called_once()
        self.assertEqual(len(self.saved),1)

    def test_missing_or_mismatched_evidence_stays_pending(self):
        self.e.state['ops']['invoices']['missing'] = {'due':1}
        self.entry['references'][0]['allocated_amount'] = 20
        Engine.settlements(self.e)
        self.e.write.assert_not_called()
        for key in ('invoice','missing'):
            self.assertNotIn('paid',self.e.state['ops']['invoices'][key])
            self.assertIn('settle:'+key,self.e.company_erp.state['pending'])

    def test_balance_change_after_mapping_blocks_submit(self):
        def changed(*args,**kw):
            self.invoice['outstanding_amount'] = 10
            return self.write(*args,**kw)
        self.e.write.side_effect = changed
        Engine.settlements(self.e)
        self.assertEqual(self.saved['pay:invoice']['docstatus'],0)
        self.assertNotIn('paid',self.e.state['ops']['invoices']['invoice'])

class ArchivedAllocations(unittest.TestCase):
    def test_frozen_allocations_match_submitted_invoices(self):
        path = Path(__file__).resolve().parents[1]/'data/observable/native-records.json'
        documents = json.loads(path.read_text())
        receipts = {'Receive':0,'Pay':0}
        for payment in documents['Payment Entry'].values():
            allocation, = payment['references']
            invoice = documents[allocation['reference_doctype']][allocation['reference_name']]
            self.assertEqual((invoice['docstatus'],payment['docstatus'],invoice['outstanding_amount']), (1,1,0))
            self.assertEqual(invoice['currency'],invoice['party_account_currency'])
            CompanyERP.verify_allocation(invoice,payment,invoice['rounded_total'] or invoice['grand_total'],'Cash - NCC')
            receipts[payment['payment_type']] += 1
        self.assertEqual(receipts,{'Receive':317,'Pay':88})

    def test_frozen_partial_deliveries_conserve_stock_and_balance_cash(self):
        path = Path(__file__).resolve().parents[1] / 'data/observable'
        docs = json.loads((path / 'native-records.json').read_text())
        order = 'SAL-ORD-2027-00015'
        deliveries = [d for d in docs['Delivery Note'].values()
                      if any(r.get('against_sales_order') == order for r in d['items'])]
        deliveries.sort(key=lambda d: d['posting_date'])
        self.assertEqual([sum(r['qty'] for r in d['items']) for d in deliveries], [144, 48])
        delivery_ids = {d['name'] for d in deliveries}

        invoices = [d for d in docs['Sales Invoice'].values()
                    if any(r.get('delivery_note') in delivery_ids for r in d['items'])]
        self.assertEqual(len(invoices), len(deliveries))
        self.assertEqual({r['delivery_note'] for d in invoices for r in d['items']}, delivery_ids)
        invoice_ids = {d['name'] for d in invoices}
        payments = [d for d in docs['Payment Entry'].values()
                    if any(r.get('reference_name') in invoice_ids for r in d['references'])]
        self.assertEqual({r['reference_name'] for d in payments for r in d['references']}, invoice_ids)
        self.assertAlmostEqual(sum(d['paid_amount'] for d in payments), 345.60)
        self.assertAlmostEqual(sum(d['grand_total'] for d in invoices), 345.60)

        works = {d['name'] for d in docs['Work Order'].values() if d.get('sales_order') == order}
        posted = [d for d in docs['Stock Entry'].values()
                  if d.get('work_order') in works and d['stock_entry_type'] == 'Manufacture']
        inspections = {d['reference_name']: d for d in docs['Quality Inspection'].values()
                       if d['docstatus'] == 1 and d['status'] == 'Accepted'}
        # This archived ERP-only trajectory records inspection-to-output and
        # delivery-to-order links. Its exports have no dispatch-lease lineage.
        for output, delivery in zip(sorted(posted, key=lambda d: d['posting_date']), deliveries, strict=True):
            self.assertLessEqual(inspections[output['name']]['report_date'], delivery['posting_date'])

        stock_ids = delivery_ids | {d['name'] for d in posted}
        movement = [r for r in json.loads((path / 'stock-ledger.json').read_text())
                    if r['voucher_no'] in stock_ids]
        self.assertEqual(sum(r['actual_qty'] for r in movement if r['item_code'] == 'SKU-30018'), 0)
        self.assertEqual(sum(r['actual_qty'] for r in movement if r['item_code'] == 'TUB200'), -192)
        gl_ids = stock_ids | invoice_ids | {d['name'] for d in payments}
        ledger = [r for r in json.loads((path / 'general-ledger.json').read_text())
                  if r['voucher_no'] in gl_ids]
        self.assertAlmostEqual(sum(r['debit'] - r['credit'] for r in ledger), 0)
        self.assertAlmostEqual(sum(r['debit'] - r['credit'] for r in ledger
                                   if r['account'] == 'Cash - NCC'), 345.60)


class OperatingSettlement(unittest.TestCase):
    def test_overdue_invoice_waits_for_submitted_scoped_bank_observation(self):
        from native.company_bank import ACCOUNT, FEED_USER, PAYMENT, TRANSACTION

        invoice = dict(doctype='Sales Invoice', name='INV-1', docstatus=1, company='Company',
                       customer='Acme', currency='EUR', party_account_currency='EUR',
                       posting_date='2027-01-01', outstanding_amount=75, debit_to='Receivable')
        statement = dict(doctype=TRANSACTION, name='BANK-1', owner=FEED_USER, docstatus=1,
                         bank_account=ACCOUNT, company='Company', currency='EUR', party_type='Customer',
                         party='Acme', reference_number=invoice['name'], transaction_id='remittance-1',
                         date='2027-01-05', deposit=75, withdrawal=0, unallocated_amount=75,
                         status='Unreconciled')
        documents = {
            ('Bank Account', ACCOUNT): dict(account='Bank GL', company='Company', is_company_account=1),
            ('Account', 'Bank GL'): dict(company='Company', account_type='Bank', is_group=0,
                                         account_currency='EUR'),
        }
        api = Mock()
        api.get.side_effect = lambda kind, name: copy.deepcopy(documents[kind, name])

        def list_docs(kind, filters, **_):
            dates = filters.pop('date', None)
            return [dict(name=name) for (doctype, name), record in documents.items() if doctype == kind
                    and all(record.get(k) == v for k, v in filters.items())
                    and (dates is None or dates[1][0] <= record['date'] <= dates[1][1])]

        def reconcile(method, request):
            self.assertTrue(method.endswith('.reconcile_vouchers'))
            payment, = json.loads(request['vouchers'])
            self.assertEqual(request['bank_transaction_name'], 'BANK-1')
            documents[PAYMENT, payment['payment_name']]['clearance_date'] = statement['date']
            documents[TRANSACTION, 'BANK-1'].update(status='Reconciled', unallocated_amount=0,
                payment_entries=[dict(payment_document=PAYMENT, payment_entry=payment['payment_name'],
                                      allocated_amount=75)])

        api.list.side_effect = list_docs
        api.call.side_effect = reconcile
        e = SimpleNamespace(company='Company', day=3, date=lambda: '2027-01-04',
                            config=dict(company_operating=True), api={'FINANCE': api},
                            state={'ops': {'invoices': {'invoice': {'due': 3}}}},
                            persist=Mock(), log=Mock(), get=lambda *args: copy.deepcopy(invoice),
                            recover_company_doc=lambda *args: documents.get((PAYMENT, 'PAY-1')))
        e.company_erp = CompanyERP(e, None)

        def mapper(role, method, **args):
            self.assertEqual((role, args['dn'], args['party_amount']), ('FINANCE', 'INV-1', 75))
            return dict(payment_type='Receive', company='Company', party_type='Customer', party='Acme',
                        paid_amount=75, received_amount=75, paid_from='Receivable', paid_to='Bank GL',
                        paid_from_account_currency='EUR', paid_to_account_currency='EUR',
                        references=[dict(reference_doctype='Sales Invoice', reference_name='INV-1',
                                         allocated_amount=75)])

        def write(key, kind, fields, role, submit, *, before_submit):
            self.assertEqual((key, kind, role, submit), ('bank-payment:remittance-1', PAYMENT, 'FINANCE', True))
            doc = dict(fields, name='PAY-1', doctype=PAYMENT, docstatus=0)
            before_submit(doc)
            doc['docstatus'] = 1
            documents[kind, doc['name']] = doc
            invoice['outstanding_amount'] = 0
            return copy.deepcopy(doc)

        e.mapped, e.write = Mock(side_effect=mapper), Mock(side_effect=write)
        Engine.settlements(e)
        documents[TRANSACTION, 'BANK-1'] = statement
        Engine.settlements(e)
        e.write.assert_not_called()
        self.assertIn('No submitted bank statement', e.company_erp.state['pending']['settle:invoice']['last_error'])

        e.day, e.date = 4, lambda: '2027-01-05'
        e.state = json.loads(json.dumps(e.state))
        e.company_erp = CompanyERP(e, None)
        Engine.settlements(e)
        Engine.settlements(e)
        e.write.assert_called_once()
        self.assertEqual(e.state['ops']['invoices']['invoice']['payments'], ['PAY-1'])
        self.assertEqual(documents[PAYMENT, 'PAY-1']['clearance_date'], statement['date'])
        self.assertEqual(documents[TRANSACTION, 'BANK-1']['unallocated_amount'], 0)

    def test_billing_rejects_mismatched_delivery_before_submit(self):
        from native.company_erp import Pending

        headers = dict(customer='Customer', company='Company', currency='EUR')
        line = dict(item_code='S', qty=3, stock_qty=3, conversion_factor=1, uom='ea', rate=2.45)
        delivery = dict(name='DN', docstatus=1, items=[dict(name='DN-row', **line)], **headers)
        invoice = dict(name='INV', items=[dict(delivery_note='DN', dn_detail='DN-row', **line)], **headers)
        draft = copy.deepcopy(invoice)
        draft['items'][0]['dn_detail'] = 'wrong'
        e = SimpleNamespace(day=1, dated=lambda day: '2027-01-04',
                            state={'ops': {'invoices': {}}}, company_erp=True,
                            get=Mock(return_value=delivery), mapped=Mock(return_value=invoice), persist=Mock())
        submitted = Mock()

        def write(*args, before_submit):
            before_submit(draft)
            submitted()
            return draft

        e.write = write
        with self.assertRaisesRegex(Pending, 'confirmed delivery'):
            Engine.invoice_delivery(e, 'delivery', 1)
        submitted.assert_not_called()
        self.assertFalse(e.state['ops']['invoices'])
        draft['items'][0]['dn_detail'] = 'DN-row'
        for record, field, wrong in [(draft, 'customer', 'other'), (draft, 'currency', 'USD'),
                                     (draft['items'][0], 'rate', 12), (draft['items'][0], 'item_code', 'other')]:
            saved = record[field]
            record[field] = wrong
            with self.assertRaises(Pending):
                Engine.invoice_delivery(e, 'delivery', 1)
            record[field] = saved
        submitted.assert_not_called()
        Engine.invoice_delivery(e, 'delivery', 1)
        submitted.assert_called_once()
