"""Read-only projection of ordinary company sales handoffs for shared extraction."""
from datetime import date

from engine import DAY

SYSTEM = 'company-sales'
EPOCH = date(2027, 1, 1)
SOURCES = {kind: f'{SYSTEM}/{doctype}' for kind, doctype in
           [('shipment', 'Delivery Note'), ('invoice', 'Sales Invoice'), ('payment', 'Payment Entry')]}


class SalesRecords:
    def __init__(self, company):
        self.company = company

    def bind(self, transport):
        for source in (SYSTEM, *SOURCES.values()):
            transport.bind(source, source, self)

    def confirmed(self):
        company = self.company
        invoices = company.state.get('ops', {}).get('invoices', {})
        for key, shipment in company.company_erp.state['shipments'].items():
            delivery_id = shipment.get('target_id')
            if not delivery_id:
                continue
            invoice_key = 'invoice:' + key
            yield delivery_id, 'shipment', shipment
            if invoice_key not in invoices:
                continue
            invoice_id = company.docs[invoice_key]['name']
            yield invoice_id, 'invoice', shipment
            for payment_id in invoices[invoice_key].get('payments', []):
                yield payment_id, 'payment', shipment

    def records(self):
        # The transport's bulk observation surface is separate from Finance capture.
        return ()

    def project(self, native_id, kind, shipment, *, system=SYSTEM):
        company = self.company
        order = company.get(shipment['order'], 'FINANCE')
        case = order.get('po_no') or order['name']
        delivery = company.get(shipment['key'], 'FINANCE')
        if delivery['docstatus'] != 1 or delivery['name'] != shipment['target_id']:
            raise ValueError('Confirmed shipment differs from native delivery')

        if (order.get('docstatus') != 1 or not delivery['items']
                or any(row.get('against_sales_order') != order['name'] for row in delivery['items'])):
            raise ValueError('Delivery lacks its submitted Sales Order link')
        if kind == 'shipment':
            items = delivery['items']
            doc = delivery
            fields = dict(order_id=order['name'], quantity=sum(row['qty'] for row in items),
                          uom=items[0]['uom'])
        else:
            invoice = company.get('invoice:' + shipment['key'], 'FINANCE')
            if (invoice['docstatus'] != 1 or
                    {row['delivery_note'] for row in invoice['items']} != {delivery['name']}):
                raise ValueError('Confirmed invoice differs from native delivery links')
            doc = invoice
            total = invoice.get('rounded_total') if not invoice.get('disable_rounded_total') else None
            fields = dict(shipment_id=delivery['name'], currency=invoice['currency'],
                          amount=invoice['grand_total'] if total is None else total)
            if kind == 'payment':
                doc = company.api['FINANCE'].get('Payment Entry', native_id)
                allocation, = doc['references']
                if (doc['docstatus'] != 1 or not doc.get('clearance_date')
                        or (allocation['reference_doctype'], allocation['reference_name'])
                        != ('Sales Invoice', invoice['name'])):
                    raise ValueError('Confirmed remittance differs from native allocation')
                from native.company_bank import ACCOUNT, FEED_USER, TRANSACTION
                api = company.api['FINANCE']
                scope = dict(docstatus=1, owner=FEED_USER, company=company.company,
                             bank_account=ACCOUNT, currency=invoice['currency'], party_type='Customer',
                             party=invoice['customer'], reference_number=invoice['name'],
                             transaction_id=doc['reference_no'])
                matches = api.list(TRANSACTION, scope, limit=2)
                if len(matches) != 1:
                    raise ValueError('Expected one matching submitted bank statement')
                bank = api.get(TRANSACTION, matches[0]['name'])
                amount = allocation['allocated_amount']
                refs = bank.get('payment_entries') or []
                if (any(bank.get(k) != v for k, v in scope.items()) or bank.get('status') != 'Reconciled'
                        or bank.get('date') != doc['clearance_date'] or bank.get('unallocated_amount') != 0
                        or bank.get('deposit') != amount or bank.get('withdrawal') != 0 or len(refs) != 1
                        or refs[0].get('payment_document') != 'Payment Entry'
                        or refs[0].get('payment_entry') != native_id or refs[0].get('allocated_amount') != amount):
                    raise ValueError('Bank statement does not confirm the native payment allocation')
                fields = dict(statement_id=bank['name'], clearance_date=bank['date'], invoice_id=invoice['name'], currency=invoice['currency'],
                              amount=allocation['allocated_amount'])
        if doc['name'] != native_id:
            raise ValueError('Sales record readback has a different identity')
        at = (date.fromisoformat(max(doc['posting_date'], fields.get('clearance_date', doc['posting_date']))) - EPOCH).days * DAY
        if system != SYSTEM and kind == 'invoice':
            fields['shipment_system'] = SOURCES['shipment']
        if system != SYSTEM and kind == 'payment':
            fields['invoice_system'] = SOURCES['invoice']
        return dict(fields, kind=kind, case=case, at=at, system=system, native_id=native_id)

    def read(self, system, native_id):
        identity, = (entry for entry in self.confirmed() if entry[0] == native_id
                     and system in (SYSTEM, SOURCES[entry[1]]))
        return self.project(*identity, system=system)

    def capture(self, clock):
        seen = clock.state.setdefault('company_sales_readbacks', [])
        if any(isinstance(entry, str) for entry in seen):
            # Old checkpoints only saved names. Recover their types from the
            # committed readbacks, even when two ERP doctypes use the same name.
            legacy = {entry for entry in seen if isinstance(entry, str)}
            migrated = [(SOURCES[row['kind']], row['native_id']) for row in clock.export()['records']
                        if row['system'] == SYSTEM and row['native_id'] in legacy]
            if {name for _, name in migrated} != legacy:
                raise ValueError('Legacy sales checkpoint lacks its confirmed native readback')
            seen[:] = [entry for entry in seen if not isinstance(entry, str)] + migrated
        event_id = clock.event['id']
        saved = clock.load_consumer_decision(event_id)
        if saved is None:
            prefix = clock.consumer_write_prefix(event_id)
            if prefix:
                # A pre-worklist handler already chose this immutable prefix.
                # Do not add records discovered after its original cutoff.
                for operation in prefix:
                    action = operation['action']
                    kind = action['kind']
                    system = operation['system']
                    native_id = operation.get('read_target')
                    if (kind not in SOURCES or system not in (SYSTEM, SOURCES[kind])
                            or not native_id):
                        raise ValueError('Invalid legacy sales readback intent')
                    fields = {key: value for key, value in action.items() if key != 'kind'}
                    clock.confirm(system, kind, native_id, fields)
                    identity = [SOURCES[kind], native_id]
                    if identity not in seen and tuple(identity) not in seen:
                        seen.append(identity)
                return
            known = {tuple(entry) for entry in seen}
            pending = [[SOURCES[kind], native_id] for native_id, kind, _ in self.confirmed()
                       if (SOURCES[kind], native_id) not in known]
            saved = clock.save_consumer_decision(event_id, {'input': pending})
        for system, native_id in saved['input']:
            record = self.read(system, native_id)
            fields = {key: value for key, value in record.items() if key not in ('system', 'native_id', 'kind')}
            clock.confirm(system, record['kind'], native_id, fields)
            seen.append([system, native_id])
