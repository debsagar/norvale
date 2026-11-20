import json
import unittest

from genealogy import ShipmentHistory


class PurchaseReceiptHistory(unittest.TestCase):
    def test_source_scoped_native_rows_and_fractional_split_bundle(self):
        data = dict(company='Co', supplier='Supplier', currency='EUR', docstatus=1)
        line = dict(name='pr-line', item_code='X', qty=0.3, stock_qty=0.3, uom='Kg', stock_uom='Kg',
                    rate=2, serial_and_batch_bundle='bundle', warehouse='Hold',
                    purchase_order='PO', purchase_order_item='po-line')
        stock = dict(name='ledger', docstatus=1, is_cancelled=0, voucher_type='Purchase Receipt',
                     voucher_no='PR', voucher_detail_no='pr-line', item_code='X', warehouse='Hold')
        receipt = dict(data, name='PR', posting_date='2027-01-01', items=[line])
        moves = dict(stock, name='bundle', type_of_transaction='Inward',
                     entries=[dict(batch_no='A', qty=0.1, warehouse='Hold'),dict(batch_no='B', qty=0.2, warehouse='Hold')])
        records = [('Purchase Receipt', receipt),('Purchase Order',dict(data, name='PO', items=[dict(line,name='po-line')])),
                   ('Serial and Batch Bundle',moves),('Stock Ledger Entry',dict(stock, stock_uom='Kg',company='Co',
                       serial_and_batch_bundle='bundle',posting_date='2027-01-01',actual_qty=0.3)),
                   ('Purchase Invoice',dict(data,name='PI',items=[dict(line, name='pi-line',purchase_receipt='PR',pr_detail='pr-line')])),
                   ('Payment Entry',dict(data,name='PAY',payment_type='Pay',party_type='Supplier',party='Supplier',
                       paid_to_account_currency='EUR',references=[dict(name='allocation',reference_doctype='Purchase Invoice',
                       reference_name='PI',allocated_amount=0.6)]))]
        def read():
            rows = [dict(source='erp', entity=e, native_id=d['name'], ingested_at='1',payload=json.dumps(d)) for e,d in records]
            other = dict(rows[3], source='other')
            return ShipmentHistory(rows+[other],'erp').purchase_receipt('PR')
        proof = read()
        self.assertEqual([lot['batch'] for lot in proof['lots']], ['A','B'])
        self.assertFalse(proof['unresolved'])
        self.assertEqual(proof['invoice_lines'][0]['invoice_payments'][0]['payment'], ['erp','Payment Entry','PAY'])
        records[3][1]['actual_qty'] = 0.2
        self.assertFalse(read()['lots'])
        records[3][1]['actual_qty'] = 0.3
        line['purchase_order_item'] = 'wrong'
        self.assertEqual(read()['unresolved'], [['pr-line','purchase order line']])
