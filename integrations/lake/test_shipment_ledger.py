import copy
import json
import unittest
from types import SimpleNamespace
from genealogy import shipment


class ShipmentLedgerTest(unittest.TestCase):
    def test_multilot_and_discrepancies(self):
        line = dict(name='L', item_code='X', stock_qty=3, stock_uom='ea', warehouse='WH', serial_and_batch_bundle='B')
        bundle = dict(name='B', docstatus=1, is_cancelled=0, voucher_type='Delivery Note', voucher_no='D',
                      voucher_detail_no='L', item_code='X', type_of_transaction='Outward', warehouse='WH',
                      entries=[dict(batch_no='A', qty=-1, warehouse='WH'), dict(batch_no='B', qty=-2, warehouse='WH')])
        ledger = dict(name='S', company='C', posting_date='2027-01-03', docstatus=1, is_cancelled=0, voucher_type='Delivery Note', voucher_no='D',
                      voucher_detail_no='L', item_code='X', warehouse='WH', stock_uom='ea',
                      serial_and_batch_bundle='B', actual_qty=-3)
        records = {'Delivery Note':[dict(name='D', company='C', posting_date='2027-01-03', docstatus=1, items=[line])],
                   'Serial and Batch Bundle':[bundle], 'Stock Ledger Entry':[ledger]}
        def trace():
            rows = [dict(source='erp', entity=entity, native_id=doc['name'], ingested_at='t', payload=json.dumps(doc))
                    for entity, docs in records.items() for doc in docs]
            return shipment(SimpleNamespace(_rows=lambda:rows), 'erp', 'D')
        self.assertEqual(trace()['unresolved_order_lines'], ['L'])
        order_line = dict(name='OL', item_code='X', stock_uom='ea')
        order = dict(name='SO', company='C', customer='Customer', docstatus=1,
                     transaction_date='2027-01-02', items=[order_line])
        records['Sales Order'] = [order]
        records['Delivery Note'][0]['customer'] = 'Customer'
        line.update(against_sales_order='SO', so_detail='OL')
        self.assertEqual(trace()['sales_order_lines'], [
            dict(delivery_row='L', order=['erp', 'Sales Order', 'SO'], order_row='OL', item='X')])
        for target, field, wrong in [(order, 'customer', 'other'), (order, 'company', 'other'),
                                     (order, 'transaction_date', '2027-01-04'), (order, 'docstatus', 0),
                                     (order_line, 'item_code', 'other'), (order_line, 'stock_uom', 'kg'),
                                     (line, 'so_detail', 'other'), (order, 'items', [order_line, order_line])]:
            with self.subTest(field=field):
                original = target[field]
                target[field] = wrong
                self.assertEqual(trace()['unresolved_order_lines'], ['L'])
                self.assertEqual(trace()['sales_order_lines'], [])
                target[field] = original
        self.assertEqual(trace()['stock_ledger'][0]['quantity'], 3)
        self.assertEqual(len(trace()['lots']), 2)
        for field, wrong in [('actual_qty',-2), ('warehouse','other'), ('company','other'), ('posting_date','2027-01-04'), ('stock_uom','kg'),
                             ('item_code','other'), ('voucher_no','other'), ('voucher_detail_no','other'),
                             ('serial_and_batch_bundle','other'), ('is_cancelled',1), ('docstatus',2)]:
            original = ledger[field]; ledger[field] = wrong
            self.assertIn(['L','stock ledger issue'], trace()['unresolved'], field)
            ledger[field] = original
        records['Stock Ledger Entry'].append(dict(ledger, name='duplicate'))
        self.assertIn(['L','stock ledger issue'], trace()['unresolved'])
        records['Stock Ledger Entry'].clear()
        self.assertIn(['L','stock ledger issue'], trace()['unresolved'])
        records['Stock Ledger Entry'].append(ledger)
        bundle['entries'][1]['warehouse'] = 'other'
        self.assertIn(['L','stock ledger issue'], trace()['unresolved'])
        bundle['entries'][1]['warehouse'] = 'WH'
        line['stock_qty'] = 4
        self.assertIn(['L','delivery line stock quantity'], trace()['unresolved'])


if __name__ == '__main__':
    unittest.main()
