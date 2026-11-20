import unittest

from integrations.lake.shipment_billing import billing_links


class ShipmentBillingTests(unittest.TestCase):
    def test_shared_invoice_payment_and_source_collisions(self):
        scope = dict(company='Co', customer='C', docstatus=1, currency='EUR')
        line = dict(name='r', item_code='S', qty=2, rate=5, uom='Nos')
        docs = {('east', 'Delivery Note', name): dict(scope, name=name, items=[line])
                for name in ('D1', 'D2')}
        docs['east', 'Sales Invoice', 'I'] = dict(scope, name='I', items=[
            dict(line, name='i'+name, delivery_note=name, dn_detail='r') for name in ('D1', 'D2')])
        voucher = dict(docstatus=1, name='P', company='Co', party='C', party_type='Customer',
                       payment_type='Receive', received_amount=12, paid_from_account_currency='EUR',
                       paid_to_account_currency='EUR', clearance_date='2027-01-08',
                       references=[dict(name='a', reference_doctype='Sales Invoice', reference_name='I', allocated_amount=12)])
        docs['west', 'Payment Entry', 'P'] = voucher
        self.assertFalse(billing_links(docs, 'east')['D1'][0]['invoice_payments'])
        docs['east', 'Payment Entry', 'P'] = voucher
        bank = dict(scope, name='B', date='2027-01-08', status='Reconciled', unallocated_amount=0,
                    deposit=12, withdrawal=0, payment_entries=[dict(payment_document='Payment Entry',
                                                                  payment_entry='P', allocated_amount=12)])
        docs['east', 'Bank Transaction', 'B'] = bank
        links = billing_links(docs, 'east')
        for name in ('D1', 'D2'):
            entry, = links[name]
            self.assertEqual(entry['invoice_row'], 'i'+name)
            payment, = entry['invoice_payments']
            self.assertEqual((payment['invoice_amount'], payment['bank_statement']), (12, ['east','Bank Transaction','B']))
        docs['east','Bank Transaction','other'] = dict(bank, name='other')
        self.assertNotIn('bank_statement', billing_links(docs, 'east')['D1'][0]['invoice_payments'][0])
        docs['east','Delivery Note','D1'] = dict(scope, name='D1', items=[dict(line,name='another')])
        self.assertFalse(billing_links(docs, 'east'))
