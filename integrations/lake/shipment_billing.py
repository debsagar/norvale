"""Index observable invoice lines, voucher allocations and bank statement links for one ERP."""
from collections import defaultdict
from native.build_evidence import INVOICES, matches_bank_clearance, shipment_rows


def billing_links(latest, source, invoice_type='Sales Invoice'):
    party = INVOICES[invoice_type][0]
    incoming = party == 'Customer'
    direction = 'Receive' if incoming else 'Pay'
    row_field = 'delivery_row' if incoming else 'receipt_row'
    docs = {(entity, name): doc for (site, entity, name), doc in latest.items()
            if site == source and doc.get('docstatus') == 1}
    statements = defaultdict(list)
    for (entity, name), doc in docs.items():
        if entity == 'Bank Transaction':
            for ref in doc.get('payment_entries', []):
                if ref.get('payment_document') == 'Payment Entry':
                    statements[ref.get('payment_entry')].append(doc)

    receipts = defaultdict(list)
    for (entity, name), voucher in docs.items():
        if entity != 'Payment Entry' or voucher.get('payment_type') != direction or voucher.get('party_type') != party:
            continue
        for allocation in voucher.get('references', []):
            if allocation.get('reference_doctype') != invoice_type:
                continue
            invoice = docs.get((invoice_type, allocation.get('reference_name')), {})
            currency = voucher.get('paid_from_account_currency' if incoming else 'paid_to_account_currency')
            if (not voucher.get('company') or invoice.get('company') != voucher['company']
                    or not voucher.get('party') or invoice.get(party.lower()) != voucher['party']
                    or not currency or currency != (invoice.get('party_account_currency') or invoice.get('currency'))
                    or not allocation.get('name') or (allocation.get('allocated_amount') or 0) <= 0):
                continue
            matching = [bank['name'] for bank in statements[name]
                        if bank.get('company') == voucher['company'] and matches_bank_clearance(voucher, bank)]
            receipt = dict(payment=[source, entity, name], allocation=allocation['name'],
                           invoice_amount=allocation['allocated_amount'], currency=currency)
            if len(matching) == 1:
                receipt['bank_statement'] = [source, 'Bank Transaction', matching[0]]
            receipts[invoice['name']].append(receipt)

    by_document = defaultdict(list)
    for (entity, name), invoice in docs.items():
        if entity != invoice_type or invoice.get('is_return'):
            continue
        for invoice_row, _, parent, row in shipment_rows(dict(invoice, doctype=entity), docs):
            by_document[parent].append({row_field: row, 'invoice': [source, entity, name],
                                        'invoice_row': invoice_row, 'invoice_payments': receipts[name]})
    return by_document
