"""Exercise receipt-based billing through the recurring inspections phase."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from native.company import Engine
from native.company_erp import CompanyERP
from norvale.loader.erp_client import ErpError


class SupplierBillingTests(unittest.TestCase):
    def test_supplier_arrival_uses_confirmed_dates_once(self):
        e = Engine.__new__(Engine)
        e.config, e.day = {'seed': 7}, 2
        e.regime = lambda: {'supply': 1}
        po = dict(docstatus=0, items=[{'schedule_date': e.dated(8)}, {'schedule_date': e.dated(12)}])
        original = dict(day=1, due=4)
        with self.assertRaisesRegex(ValueError, 'submitted'):
            e.schedule_supplier_arrival('p', original, po)
        self.assertNotIn('arrival_due', original)
        po['docstatus'] = 1
        e.schedule_supplier_arrival('p', original, po)
        shifted = dict(day=1, due=4)
        po['items'][1]['schedule_date'] = e.dated(15)
        e.schedule_supplier_arrival('p', shifted, po)
        self.assertEqual(shifted['arrival_due'] - original['arrival_due'], 3)
        saved = original.copy()
        e.day += 1
        e.schedule_supplier_arrival('p', original, po)
        self.assertEqual(original, saved)

    def test_replenishment_uses_native_promises_and_remaining_stock_units(self):
        e = Engine.__new__(Engine)
        e.day = 0
        e.wh = {'rm': 'Released', 'hold': 'Hold'}
        e.state = {'ops': {'purchases': {'po': {'items': []}}}}
        e.catalog = SimpleNamespace(source_list={'X': 'S'}, safety_days={'X': 1},
                                    vendors={'S': {'supplies': {'X': (2, (2, 0), None)}}})
        e.observed_usage = lambda: {'X': 4}
        e.inventory = lambda role: {}
        e.observe = Mock(return_value=[])
        e.api = {'BUYER': Mock()}
        e.api['BUYER'].get.return_value = {}
        e.effective_policies = lambda role: []
        e.regime = lambda: {'cost': 1}
        row = dict(item_code='X', qty=10, received_qty=6, conversion_factor=3, schedule_date=e.dated(3))
        po = dict(name='PO', docstatus=1, items=[row])
        e.get = lambda key, role: po
        plans = []
        def capture(build):
            plans.append(build())
            return []
        e.plan = capture
        e.replenishment()
        self.assertEqual(plans.pop(), [])
        row['schedule_date'] = e.dated(4)
        e.replenishment()
        self.assertEqual(plans.pop()[0]['items'][0]['item_code'], 'X')
        row.update(schedule_date=e.dated(2), received_qty=7)
        e.replenishment()
        self.assertTrue(plans.pop())
        row['received_qty'] = 0
        po['status'] = 'Closed'
        e.replenishment()
        self.assertTrue(plans.pop())

    def test_overdue_line_review_episode_and_retry(self):
        e = Engine.__new__(Engine)
        e.day = 2
        e.wh = {'rm': 'Released'}
        e.catalog = SimpleNamespace(source_list={})
        e.persist = Mock()
        e.log = Mock()
        records = {}
        names = {}
        e.docs = names
        active = {}

        def write(key, kind, fields, role):
            self.assertEqual((kind, role), ('ToDo', 'SCM_MGR'))
            name = names.setdefault(key, str(len(names)))
            return records.setdefault(name, dict(name=name, **fields))

        def update(kind, name, fields):
            self.assertEqual(kind, 'ToDo')
            records[name].update(fields)

        e.write = Mock(side_effect=write)
        e.get = lambda key, role: records[names[key]].copy()
        buyer = Mock()
        buyer.get.side_effect = lambda kind, name: records[name].copy()
        scm = Mock()
        scm.update.side_effect = update
        e.api = dict(BUYER=buyer, SCM_MGR=scm)
        row = dict(name='line', item_code='X', qty=10, received_qty=0, uom='Box',
                   conversion_factor=2, schedule_date=e.dated(1))
        po = dict(name='PO', docstatus=1, status='To Receive and Bill', items=[row])
        orders = {'PO': po}

        def review(stock=0, use=3):
            e.review_supplier_delays(orders, {('X', 'Released'): stock}, {'X': use}, active)

        review(stock=3)
        review(use=0)
        po['docstatus'] = 0
        review()
        self.assertFalse(records)
        po['docstatus'] = 1

        # The Frappe POST succeeded but the buyer's first GET timed out.
        read = buyer.get.side_effect
        buyer.get.side_effect = TimeoutError('lost buyer readback')
        with self.assertRaises(TimeoutError):
            review()
        buyer.get.side_effect = read
        review()
        review()
        first, = records.values()
        self.assertEqual((first['date'], first['reference_name'], first['allocated_to']),
                         (e.dated(3), 'PO', 'buyer@norvale.test'))
        scm.update.assert_not_called()

        e.day = 3
        row['received_qty'] = 4
        review()
        self.assertIn('Open balance 6 Box', first['description'])
        self.assertEqual(first['date'], e.dated(4))
        row['expected_delivery_date'] = e.dated(5)
        review()
        self.assertEqual(first['status'], 'Closed')
        self.assertIn('Previous native delivery date', first['description'])

        e.day = 6
        review()
        second = records['1']
        self.assertEqual((second['status'], second['date']), ('Open', e.dated(7)))
        row['received_qty'] = 10
        review()
        self.assertIn('fully received', second['description'])

        e.day = 7
        row['received_qty'] = 4
        review()
        po.update(docstatus=2, status='Cancelled')
        review()
        self.assertIn('Cancelled', records['2']['description'])

        e.day = 8
        po.update(docstatus=1, status='To Receive and Bill')
        review()
        po.update(docstatus=0, workflow_state='Rejected')
        review()
        self.assertIn('order rejected', records['3']['description'])
        self.assertFalse(active)

    @classmethod
    def setUpClass(cls):
        cls.archive = json.loads((Path(__file__).resolve().parents[1]/'data/observable/native-records.json').read_text())

    def setUp(self):
        self.invoice = deepcopy(next(iter(self.archive['Purchase Invoice'].values())))
        line = self.invoice['items'][0]
        self.receipt = deepcopy(self.archive['Purchase Receipt'][line['purchase_receipt']])
        self.order = deepcopy(self.archive['Purchase Order'][line['purchase_order']])
        self.engine = SimpleNamespace(config={}, day=1, persist=Mock(),log=Mock(),dated=lambda d:'2027-01-01',
            state={'ops':{'purchases':{'po':{'received':0}},'invoices':{}}})
        self.engine.company_erp = CompanyERP(self.engine,None)
        self.engine.company_erp.inspections = Mock()
        def get(key,role):
            self.assertEqual(role,'FINANCE')
            return {'po':self.order,'receipt:po':self.receipt}[key]
        self.engine.get = get
        self.engine.mapped = Mock(return_value=self.invoice)
        def write(key,dt,fields,role,submit,before_submit):
            self.assertEqual((key,dt,role,submit),('invoice:po','Purchase Invoice','FINANCE',True))
            before_submit(fields)
            return self.invoice
        self.engine.write = Mock(side_effect=write)

    def test_native_terms_and_recovery(self):
        self.invoice['due_date'] = '2027-02-18'
        # A native bill exists, but its first readback is lost.
        write = self.engine.write.side_effect
        self.engine.write.side_effect = TimeoutError('readback')
        Engine.inspections(self.engine)
        self.assertFalse(self.engine.state['ops']['invoices'])
        self.engine.write.side_effect = write
        Engine.inspections(self.engine)
        Engine.inspections(self.engine)
        self.assertEqual(self.engine.state['ops']['invoices'],{'invoice:po':{'due':48}})
        self.assertEqual(self.engine.mapped.call_count,2)
        self.assertEqual(self.engine.mapped.call_args.kwargs['existing'],('invoice:po','Purchase Invoice'))

    def test_disagreement_blocks_finance_while_other_phases_can_continue(self):
        for field,value in [('rate',10000),('pr_detail','wrong'),('qty',0.5)]:
            with self.subTest(field=field):
                row = self.invoice['items'][0]
                saved = row[field]
                row[field] = value
                Engine.inspections(self.engine)
                self.assertIn('supplier-invoice:po',self.engine.company_erp.state['pending'])
                self.engine.write.assert_not_called()
                row[field] = saved
        self.order['payment_terms_template'] = 'Net 30'
        Engine.inspections(self.engine)
        self.engine.write.assert_not_called()

    def test_finance_denial_requires_linked_purchase_before_read_share(self):
        e = self.engine
        denial = ErpError(403, 'PermissionError', 'Access denied')
        e.get = Mock(side_effect=[denial,self.order,self.order])
        e.api = {'BUYER':Mock()}
        self.assertEqual(e.company_erp.finance_order('po',self.receipt),self.order)
        e.api['BUYER'].call.assert_called_once_with('frappe.share.add', dict(doctype='Purchase Order',
            name=self.order['name'],user='finance@norvale.test',read=1,write=0,submit=0,share=0,everyone=0,notify=0))
        e.get.side_effect=[denial,self.order]
        e.api['BUYER'].reset_mock()
        self.receipt['items'][0]['purchase_order']='wrong'
        with self.assertRaisesRegex(ValueError,'Buyer cannot link'):
            e.company_erp.finance_order('po',self.receipt)
        e.api['BUYER'].call.assert_not_called()

    def test_installment_import_respects_hold_and_persists_eligible_transfer_date(self):
        from native.company_bank import BankReceipts
        e = self.engine
        e.company, e.config = self.invoice['company'], {'seed':42}
        e.date = lambda:'2027-01-14'
        e.dated = lambda _: '2027-01-13'
        self.invoice.update(on_hold=1,payment_schedule=[
            dict(name='a',payment_term='first',due_date='2027-01-13',outstanding=4),
            dict(name='b',payment_term='second',due_date='2027-01-23',outstanding=6)])
        e.get = lambda *args:self.invoice
        bank = BankReceipts(e.company_erp)
        bank.import_invoice('invoice',{'due':10})
        e.write.assert_not_called()
        self.invoice['on_hold']=0
        def accept(op,kind,fields,role,submit,before_submit):
            doc=dict(fields,owner='bank_feed@norvale.test',docstatus=1)
            before_submit(doc)
            return doc
        e.write.side_effect = accept
        bank.import_invoice('invoice',{'due':10})
        e.date = lambda:'2027-01-16'
        bank.import_invoice('invoice',{'due':10})
        for call in e.write.call_args_list:
            statement = call.args[2]
            self.assertEqual((statement['description'],statement['date'],statement['withdrawal']),
                             ('Payment Schedule a','2027-01-14',4.0))
        self.assertEqual(e.write.call_count,2)
        self.assertEqual(e.write.call_args_list[0].args[0],e.write.call_args_list[1].args[0])
        self.invoice['payment_schedule'][1]['payment_term']='first'
        with self.assertRaisesRegex(ValueError, 'distinct native'):
            bank.import_invoice('invoice',{'due':10})

    def test_all_frozen_native_supplier_bills_match(self):
        bills=self.archive['Purchase Invoice'].values()
        for doc in bills:
            row=doc['items'][0]
            CompanyERP.match_supplier_bill(self.archive['Purchase Order'][row['purchase_order']],
                                           self.archive['Purchase Receipt'][row['purchase_receipt']],doc)
        self.assertEqual(len(bills),91)
