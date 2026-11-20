"""Recurring MES/QMS/PLM handoffs backed by ERPNext 15 stock documents."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from fractions import Fraction
import hashlib
import json
import math
from urllib.error import URLError
from uuid import uuid4

from norvale.loader.erp_client import ErpError


class MailReadbackError(RuntimeError):
    """An existing mail operation could not be reconciled with its intended delivery."""


class Pending(ValueError):
    """The dependent operation must wait for authoritative evidence or reconciliation."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def quantity(value, *, zero=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (not zero and not value):
        raise Pending('Invalid stock quantity')
    return value

def stock_steps(value, step, rounding=ROUND_FLOOR):
    return float((Decimal(str(round(float(value), 8))) / Decimal(str(step)))
                 .to_integral_value(rounding=rounding) * Decimal(str(step)))



def ids(record, *keys):
    return isinstance(record, dict) and all(isinstance(record.get(k), str) and record[k].strip() for k in keys)


class CompanyERP:
    def __init__(self, engine, authority):
        self.e = engine
        self.authority = authority
        from native.company_maintenance import Equipment
        self.equipment = Equipment(self)

    @property
    def state(self):
        # Engine.step reloads the private checkpoint while holding execution.lock.
        return self.e.state.setdefault('company_erp', {
            'lots': {}, 'work': {}, 'completions': {}, 'moves': {}, 'shipments': {},
        })

    @property
    def namespace(self):
        if 'namespace' not in self.state:
            self.state['namespace'] = uuid4().hex
            self.e.persist()
        return self.state['namespace']

    def attempt(self, key, role, action, *, defer_transport=True):
        try:
            result = action()
        except (Pending, ErpError, URLError, TimeoutError, ConnectionError) as exc:
            if not defer_transport and not isinstance(exc, Pending):
                raise
            self.state.setdefault('pending', {})[key] = {
                'role': role, 'last_error': str(exc), 'next_day': self.e.day + 1,
            }
            self.e.persist()
            self.e.log('company_erp_pending', operation=key, role=role, error=str(exc))
            return None
        if self.state.setdefault('pending', {}).pop(key, None):
            self.e.persist()
        return result

    @staticmethod
    def match_supplier_bill(order, receipt, invoice):
        """Authorize ordinary receipt-based billing of the received purchase lines."""
        if (order.get('docstatus') != 1 or receipt.get('docstatus') != 1
                or receipt.get('is_return') or invoice.get('is_return') or invoice.get('update_stock')):
            raise Pending('Expected a submitted purchase, a receipt and a supplier bill without a stock movement')
        for field in ('supplier', 'company', 'currency'):
            if not order.get(field) or not order[field] == receipt.get(field) == invoice.get(field):
                raise Pending('Supplier bill differs from purchase or receipt ' + field)
        ordered = {line['name']: line for line in order['items']}
        received = {line['name']: line for line in receipt['items']}
        billed = set()
        total_received = {}
        for line in invoice['items']:
            ref = line.get('pr_detail')
            po_id = line.get('po_detail')
            actual = received.get(ref)
            agreed = ordered.get(po_id)
            if (not actual or not agreed or ref in billed
                    or line.get('purchase_receipt') != receipt['name']
                    or line.get('purchase_order') != order['name']
                    or actual.get('purchase_order') != order['name'] or actual.get('purchase_order_item') != po_id
                    or quantity(line.get('qty')) != actual.get('qty')
                    or any(not agreed.get(k) or agreed[k] != actual.get(k) or agreed[k] != line.get(k)
                           for k in ('item_code','uom'))
                    or quantity(line.get('rate'), zero=True) != actual.get('rate') or line.get('rate') != agreed.get('rate')):
                raise Pending('Invoice line differs from the ordered price or received quantity and source')
            billed.add(ref)
            total_received[po_id] = total_received.get(po_id, 0) + actual['qty']
        if not billed or billed != set(received) or any(
                qty > ordered[key]['qty'] + 1e-8 for key, qty in total_received.items()):
            raise Pending('Invoice does not cover exactly the received purchase lines')

    def finance_order(self, key, receipt):
        try:
            return self.e.get(key, 'FINANCE')
        except ErpError as exc:
            if exc.status != 403:
                raise
        order = self.e.get(key, 'BUYER')
        if (order.get('docstatus') != 1 or receipt.get('docstatus') != 1
                or order.get('supplier') != receipt.get('supplier')
                or not receipt['items'] or any(r.get('purchase_order') != order['name'] for r in receipt['items'])):
            raise Pending('Buyer cannot link this receipt to its approved order')
        self.e.api['BUYER'].call('frappe.share.add', dict(doctype='Purchase Order', name=order['name'],
            user='finance@norvale.test', read=1, write=0, submit=0, share=0, everyone=0, notify=0))
        return self.e.get(key, 'FINANCE')

    def invoice_purchase(self, key):
        e = self.e
        receipt = e.get('receipt:' + key, 'FINANCE')
        op = 'invoice:' + key
        invoice = e.mapped('FINANCE', 'erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice',
                           existing=(op, 'Purchase Invoice'), source_name=receipt['name'])
        terms = self.finance_order(key, receipt).get('payment_terms_template')
        if terms and not invoice.get('name') and not invoice.get('payment_terms_template'):
            # ERPNext's receipt mapper supplies an immediate-payment default.
            # Let native invoice validation rebuild the schedule from the signed PO template.
            invoice.update(payment_terms_template=terms, payment_schedule=[], due_date=None)
        def verify(doc):
            current_receipt = e.get('receipt:' + key, 'FINANCE')
            order = self.finance_order(key, current_receipt)
            self.match_supplier_bill(order, current_receipt, doc)
            if order.get('payment_terms_template') and doc.get('payment_terms_template') != order['payment_terms_template']:
                raise Pending('Supplier invoice terms differ from the purchase commitment')
            if order.get('payment_terms_template') and doc.get('name'):
                def portions(record):
                    return sorted((r.get('payment_term') or '', r.get('invoice_portion'))
                                  for r in record.get('payment_schedule', []))
                if not order.get('payment_schedule') or portions(order) != portions(doc):
                    raise Pending('Supplier invoice installments differ from the purchase commitment')
        verify(invoice)
        actual = e.write(op, 'Purchase Invoice', invoice, 'FINANCE', True, before_submit=verify)
        verify(actual)
        if actual.get('docstatus') != 1 or not actual.get('due_date'):
            raise Pending('Expected a submitted supplier invoice with its native due date')
        # Use the native calendar even when resuming a prior-day submission.
        due = (date.fromisoformat(actual['due_date']) - date.fromisoformat(e.dated(0))).days
        e.state['ops']['invoices'][op] = dict(due=due)
        e.persist()
        return actual['name']

    @staticmethod
    def verify_allocation(invoice, payment, amount, cash_account):
        kind = invoice['doctype']
        incoming = kind == 'Sales Invoice'
        party_type = 'Customer' if incoming else 'Supplier'
        party = invoice.get(party_type.lower())
        currency = invoice.get('party_account_currency') or invoice.get('currency')
        refs = payment.get('references', [])
        side = 'paid_from' if incoming else 'paid_to'
        if (kind not in ('Sales Invoice','Purchase Invoice') or not party or not currency
                or payment.get('payment_type') != ('Receive' if incoming else 'Pay')
                or payment.get('company') != invoice['company']
                or (payment.get('party_type'), payment.get('party')) != (party_type, party)
                or payment.get(side + '_account_currency') != currency
                or payment.get('paid_amount' if incoming else 'received_amount') != amount
                or payment.get('paid_to' if incoming else 'paid_from') != cash_account
                or len(refs) != 1 or refs[0].get('allocated_amount') != amount
                or (refs[0].get('reference_doctype'), refs[0].get('reference_name')) != (kind, invoice['name'])):
            raise Pending('Payment does not match intended invoice, party, currency or allocation')

    def settle(self, invoice_key):
        """Submit the mapped cash entry once and confirm its allocation as Finance."""
        e = self.e
        key = 'pay:' + invoice_key
        if invoice_key not in e.docs:
            raise Pending('Invoice reference is missing')
        invoice = e.get(invoice_key, 'FINANCE')
        kind = invoice['doctype']
        if (kind not in ('Sales Invoice', 'Purchase Invoice') or invoice.get('docstatus') != 1
                or invoice.get('is_return') or invoice.get('company') != e.company):
            raise Pending('Expected a submitted company invoice for settlement')
        incoming = kind == 'Sales Invoice'
        party_type = 'Customer' if incoming else 'Supplier'
        party = invoice.get(party_type.lower())
        currency = invoice.get('party_account_currency') or invoice.get('currency')
        outstanding = invoice.get('outstanding_amount')
        if not party or not currency or type(outstanding) not in (int, float) or not math.isfinite(outstanding):
            raise Pending('Invoice lacks a finite balance, currency or counterparty')
        cash_account = e.config.get('settlement_cash_account') or f'Cash - {e.abbr}'
        previous = e.recover_company_doc(key, 'Payment Entry', 'FINANCE')
        if not previous and outstanding <= 0:
            raise Pending('Invoice has no collectible balance; reconcile existing settlement')
        entry = previous or e.mapped('FINANCE','erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry',
                                     dt=kind, dn=invoice['name'], bank_account=cash_account)
        intent = self.state.setdefault('settlement_amounts', {})
        if key not in intent:
            expected = outstanding
            if previous and previous.get('docstatus') == 1:
                # Legacy run interrupted after native submission but before the local paid flag.
                expected = previous.get('paid_amount' if incoming else 'received_amount')
                if previous.get('reference_no') != key:
                    raise Pending('Existing submitted payment lacks the expected operation reference')
            if type(expected) not in (int,float) or not math.isfinite(expected) or expected <= 0:
                raise Pending('No positive settlement amount could be verified')
            intent[key] = expected
            e.persist()

        def verify(doc):
            self.verify_allocation(invoice, doc, intent[key], cash_account)

        def before_submit(draft):
            verify(draft)
            current = e.get(invoice_key, 'FINANCE')
            if current.get('docstatus') != 1 or current.get('outstanding_amount') != intent[key]:
                raise Pending('Invoice balance changed before settlement')
        verify(entry)
        if not previous:
            entry.update(reference_no=key, reference_date=e.date())
        actual = e.write(key, 'Payment Entry', entry, 'FINANCE', True, before_submit=before_submit)
        verify(actual)
        current = e.get(invoice_key, 'FINANCE')
        if actual.get('docstatus') != 1 or current.get('docstatus') != 1 or current.get('outstanding_amount') != 0:
            raise Pending('Submitted payment and settled invoice have not both been confirmed')
        e.log('settled_invoice', doc=current, payment=actual['name'])
        return actual['name']

    def batch_row(self, row, batch):
        return {**row, 'use_serial_batch_fields': 1, 'batch_no': batch,
                'serial_no': None, 'serial_and_batch_bundle': None}

    def batch(self, source, item):
        api = self.e.api['IT_ADMIN']
        batch = 'NV-' + digest([self.namespace, source, item])[:32]
        if not api.list('Batch', {'name': batch}):
            api.create('Batch', {'batch_id': batch, 'item': item})
        if api.get('Batch', batch)['item'] != item:
            raise Pending('Batch readback item differs')
        return batch

    def prepare_receipt(self, key, doctype, doc, role):
        for index, row in enumerate(doc['items']):
            batch = self.batch([key, index], row['item_code'])
            doc['items'][index] = self.batch_row(row, batch)
        return self.e.write(key, doctype, doc, role, True)

    def receive(self, doc, kind, role):
        for index, row in enumerate(doc['items']):
            batch = row['batch_no']
            warehouse = row.get('t_warehouse') or row.get('warehouse')
            qty = row.get('transfer_qty') or row.get('stock_qty') or row['qty']
            self.verify_row(doc, row, batch, qty, warehouse, 'Inward', role)
            quality_scope = {}
            if kind == 'incoming' and self.e.config.get('supplier_quality'):
                if not doc.get('supplier') or not doc.get('posting_date'):
                    raise Pending('Supplier quality requires a dated supplier receipt')
                quality_scope = dict(supplier=doc['supplier'], received_on=doc['posting_date'], receipt_index=index)
            self.register(batch, row['item_code'], qty, row['stock_uom'], kind, doc,
                          source_row=row['name'], **quality_scope,
                          **({k: row.get(k) for k in ('purchase_order', 'purchase_order_item')}
                             if kind == 'incoming' else {}))
        self.e.persist()

    def register(self, batch, item, qty, uom, kind, doc, **source):
        lot = {'batch': batch, 'item': item, 'qty': qty, 'uom': uom, 'kind': kind,
               'source_type': doc['doctype'], 'source_id': doc['name'], **source}
        if self.state['lots'].setdefault(batch, lot) != lot:
            raise Pending('Lot provenance changed')

    def verify_row(self, doc, row, batch, qty, warehouse, direction, role, *, exact=False):
        api = self.e.api[role]
        ledger = api.list('Stock Ledger Entry', {
            'voucher_type': doc['doctype'], 'voucher_no': doc['name'], 'voucher_detail_no': row['name'],
            'warehouse': warehouse, 'is_cancelled': 0,
        }, ['name', 'actual_qty', 'serial_and_batch_bundle', *(
            ['docstatus', 'company', 'posting_date', 'item_code', 'stock_uom'] if exact else [])])
        sign = -1 if direction == 'Outward' else 1
        if (doc['docstatus'] != 1 or len(ledger) != 1 or not ledger[0]['serial_and_batch_bundle']
                or not math.isclose(ledger[0]['actual_qty'], sign * qty, abs_tol=1e-8)):
            raise Pending('Missing or mismatched submitted stock ledger row')
        bundle = api.get('Serial and Batch Bundle', ledger[0]['serial_and_batch_bundle'])
        entries = bundle['entries']
        if (bundle['docstatus'] != 1 or bundle['voucher_no'] != doc['name']
                or bundle['voucher_detail_no'] != row['name']
                or bundle['item_code'] != row['item_code'] or bundle['warehouse'] != warehouse
                or bundle['type_of_transaction'] != direction
                or any(entry['batch_no'] != batch or entry.get('serial_no') for entry in entries)
                or not math.isclose(sum(entry['qty'] for entry in entries), sign * qty, abs_tol=1e-8)):
            raise Pending('Submitted bundle differs from pinned stock movement')
        if exact:
            leg = ledger[0]
            expected = sign * Decimal(str(qty))
            if (leg.get('docstatus') != 1 or leg.get('company') != doc['company']
                    or leg.get('posting_date') != doc['posting_date']
                    or leg.get('item_code') != row['item_code'] or leg.get('stock_uom') != row['stock_uom']
                    or bundle.get('voucher_type') != doc['doctype'] or bundle.get('is_cancelled') != 0
                    or len(entries) != 1 or entries[0].get('warehouse') != warehouse
                    or Decimal(str(leg['actual_qty'])) != expected
                    or Decimal(str(entries[0]['qty'])) != expected):
                raise Pending('Release commit readback differs from the exact native stock leg')
        return ledger[0], bundle

    def balance(self, lot, warehouse, role):
        return quantity(self.e.api[role].call('erpnext.stock.doctype.batch.batch.get_batch_qty', {
            'batch_no': lot['batch'], 'item_code': lot['item'], 'warehouse': warehouse,
        }), zero=True)

    def disposition(self, lot, role):
        if lot.get('rework_key'):
            resolver = getattr(self.authority, 'rework_for', None)
            bridge = resolver(lot) if resolver else getattr(self.authority, 'rework', None)
            return bridge.disposition(self, lot) if bridge else None
        if self.state.get('rework', {}).get('rework:' + digest(lot['batch']), {}).get('authority'):
            return None
        d = self.authority.disposition(deepcopy(lot), as_of=self.e.date(), role=role)
        if d is None:
            return None
        if (not ids(d, 'id', 'version', 'approver', 'evidence') or d.get('batch') != lot['batch'] or d.get('item') != lot['item'] or d.get('uom') != lot['uom']
                or d.get('status') not in ('released', 'hold', 'rejected', 'rework')
                or d.get('authorized') is not True):
            raise Pending('No valid current authorized disposition')
        cap = quantity(d.get('qty'), zero=True)
        if cap > lot['qty'] or (d['status'] != 'released' and cap):
            raise Pending('Disposition quantity exceeds its scope')
        return deepcopy(d)

    def submit_operation(self, key, doctype, data, role, operation, validate):
        previously_started = operation.get('submit_started')
        def guard(draft):
            validate(draft)
            operation['submit_started'] = True
            self.e.persist()
        try:
            return self.e.write(key, doctype, data, role, True, before_submit=guard)
        except ErpError as exc:
            if not previously_started and 400 <= exc.status < 500 and exc.status not in (408, 429):
                operation.pop('submit_started', None)
                self.e.persist()
            raise

    def reconcile_moves(self, lot):
        for key, move in self.state['moves'].items():
            if move['batch'] != lot['batch'] or move.get('done') or move.get('abandoned'):
                continue
            doc = self.e.recover_company_doc(key, 'Stock Entry', 'QA')
            if move.get('lease_id'):
                if doc and doc['docstatus'] == 1:
                    self.confirm_move(move, doc)
                elif move.get('submit_started') or (doc and doc['docstatus'] != 0):
                    raise Pending('Uncertain release submit requires native ERP reconciliation')
                else:
                    self.submit_release_transfer(key, lot, move)
            elif doc and doc['docstatus'] == 1:
                self.confirm_move(move, doc)
            elif doc and doc['docstatus'] == 0 and move.get('submit_started'):
                doc = self.submit_operation(key, 'Stock Entry', doc, 'QA', move,
                                            lambda draft: self.check_move(lot, move, draft))
                self.confirm_move(move, doc)
            elif move.get('submit_started') or (doc and doc['docstatus'] != 0):
                raise Pending('Unknown or cancelled release movement requires reconciliation')
            else:
                # A fresh authority read and fresh native balance determine any retry.
                move['abandoned'] = True
                self.e.persist()

    def verify_move_intent(self, lot, move, doc):
        try:
            disposition = json.loads(doc['remarks'])
            row, = doc['items']
        except (KeyError, TypeError, ValueError) as exc:
            raise Pending('Release movement lacks its native disposition or stock row') from exc
        expected = move['disposition']
        if move.get('lease_id'):
            expected = {**expected, 'qms_transfer_lease': move['lease_id']}
        if (disposition != expected or doc.get('stock_entry_type') != 'Material Transfer'
                or doc.get('company') != self.e.company
                or row.get('s_warehouse') != move['source'] or row.get('t_warehouse') != move['target']
                or row.get('item_code') != lot['item'] or row.get('qty') != move['qty']
                or row.get('transfer_qty', row['qty']) != move['qty']
                or row.get('stock_uom', lot['uom']) != lot['uom']):
            raise Pending('Release movement differs from pinned QMS disposition or stock allocation')
        if move.get('lease_id') and (not ids(doc, 'name', 'posting_date') or not ids(row, 'name')
                or row.get('batch_no') != lot['batch'] or row.get('transfer_qty') != move['qty']
                or row.get('stock_uom') != lot['uom'] or row.get('conversion_factor') != 1):
            raise Pending('Release movement lacks exact batch and stock units')
        return row

    def check_move(self, lot, move, draft):
        row = self.verify_move_intent(lot, move, draft)
        if row.get('batch_no') != lot['batch'] or row.get('serial_and_batch_bundle'):
            raise Pending('Release draft differs from intended movement')
        reserved = self.return_reservation(lot)
        if (move['source'] == self.e.wh['hold'] and reserved
                and round(self.balance(lot, move['source'], 'QA') - reserved, 8) < move['qty']):
            raise Pending('Quarantine release would consume customer returns')
        if self.disposition(lot, 'QA') != move['disposition']:
            raise Pending('Current QMS disposition changed during release sync')
        return row

    def transfer_binding(self, move, doc, row):
        try:
            posting = date.fromisoformat(doc['posting_date'])
        except (KeyError, TypeError, ValueError) as exc:
            raise Pending('Release transfer lacks an ERP business date') from exc
        if posting.isoformat() != doc['posting_date'] or doc['posting_date'] > self.e.date():
            raise Pending('Release transfer posting date exceeds the business cutoff or has invalid format')
        return dict(erp_source=self.authority.release_transfer_source(self.e.api['QA']),
                    stock_entry=doc['name'], row=row['name'], company=doc['company'], posting_date=doc['posting_date'],
                    source=move['source'], target=move['target'])

    def transfer_lease(self, request, outcome, *, commit=None):
        change = getattr(self.authority, 'release_transfer_lease', None)
        if not callable(change):
            raise Pending('Ordinary quarantine release requires the native QMS lease adapter')
        receipt = change(deepcopy(request), outcome=outcome, role='QA', commit=commit)
        if (not isinstance(receipt, dict) or receipt.get('id') != request['id']
                or receipt.get('transfer') != request['transfer'] or receipt.get('delivery')):
            raise Pending('QMS transfer lease receipt differs from the request')
        return receipt

    def submit_release_transfer(self, key, lot, move):
        def guard(draft):
            if move.get('submit_started'):
                raise Pending('Release submit permission has already been consumed')
            row = self.check_move(lot, move, draft)
            request = dict(id=move['lease_id'], kind='release_transfer',
                           transfer=self.transfer_binding(move, draft, row),
                           scopes=[dict(lot=lot, disposition=move['disposition'], dispatch_qty=move['qty'])])
            if move.setdefault('lease_request', deepcopy(request)) != request:
                raise Pending('Release draft differs from its original QMS lease request')
            self.e.persist()
            receipt = self.transfer_lease(request, 'active')
            if receipt.get('state') in ('denied', 'withdrawn', 'aborted'):
                move['abandoned'] = True
                self.e.persist()
            if receipt.get('state') != 'active':
                raise Pending('QMS transfer lease is ' + str(receipt.get('state')))
            receipt = self.transfer_lease(request, 'armed')
            if receipt.get('state') != 'armed' or receipt.get('submit_permitted') is not True:
                raise Pending('QMS transfer lease did not issue a fresh submit permission')
            actual = self.e.api['QA'].get('Stock Entry', draft['name'])
            actual_row = self.verify_move_intent(lot, move, actual)
            if (actual['docstatus'] != 0 or actual_row.get('serial_and_batch_bundle')
                    or self.transfer_binding(move, actual, actual_row) != request['transfer']):
                raise Pending('Release draft changed while arming its QMS lease')
            # Persist the single permission before the PUT. Even a rejected or
            # lost reply is reconciled only by native readback after this point.
            move['submit_started'] = True
            self.e.persist()

        disposition = {**move['disposition'], 'qms_transfer_lease': move['lease_id']}
        row = self.batch_row(dict(item_code=lot['item'], qty=move['qty'],
                                  s_warehouse=move['source'], t_warehouse=move['target']), lot['batch'])
        doc = self.e.write(key, 'Stock Entry', dict(company=self.e.company, stock_entry_type='Material Transfer',
                                                   items=[row], remarks=json.dumps(disposition, sort_keys=True)),
                           'QA', True, before_submit=guard)
        self.confirm_move(move, doc)
        return doc

    def confirm_move(self, move, doc):
        lot = self.state['lots'][move['batch']]
        guarded = bool(move.get('lease_id'))
        request = move.get('lease_request')
        if guarded:
            if not request or not move.get('submit_started') or doc['name'] != request['transfer']['stock_entry']:
                raise Pending('Submitted Stock Entry has no matching armed QMS transfer attempt')
            # Always fetch the submitted document independently of its mutation reply.
            doc = self.e.api['QA'].get('Stock Entry', request['transfer']['stock_entry'])
        row = self.verify_move_intent(lot, move, doc)
        legs = {}
        for direction, warehouse in (('Outward', move['source']), ('Inward', move['target'])):
            evidence = self.verify_row(doc, row, move['batch'], move['qty'], warehouse, direction, 'QA',
                                       **({'exact': True} if guarded else {}))
            if guarded:
                ledger, _ = evidence
                legs[direction] = dict(ledger=ledger['name'], bundle=ledger['serial_and_batch_bundle'])
        if guarded:
            if self.transfer_binding(move, doc, row) != request['transfer']:
                raise Pending('ERP commit does not match the pinned QMS receiver')
            observed = self.e.api['QA'].list('Stock Ledger Entry', {
                'voucher_type': 'Stock Entry', 'voucher_no': doc['name'], 'is_cancelled': 0,
            }, ['name'])
            if sorted(d['name'] for d in observed) != sorted(leg['ledger'] for leg in legs.values()):
                raise Pending('Unexpected active release stock ledger rows')
            commit = dict(transfer=request['transfer'], quantity=move['qty'], legs=legs)
            if self.transfer_lease(request, 'submitted', commit=commit).get('state') != 'submitted':
                raise Pending('QMS transfer finalization is unresolved')
        move.update(done=True, target_id=doc['name'])
        self.e.persist()

    def used(self, lot):
        if lot['kind'] == 'finished':
            return sum(row['qty'] for p in self.state['shipments'].values() if p.get('target_id')
                       for row in p['rows'] if row['batch'] == lot['batch'])
        return sum(row['qty'] for p in self.state['completions'].values() if p.get('target_id')
                   for row in p['report']['inputs'] if row['batch'] == lot['batch'])

    def return_reservation(self, lot):
        # ERP may have posted an unresolved receipt before its reply was lost.
        return math.fsum(entry['receipt']['quantity']
                         for entry in self.state.get('customer_returns', {}).values()
                         if entry['receipt']['batch'] == lot['batch'])

    def sync(self, lot):
        self.reconcile_moves(lot)
        d = self.disposition(lot, 'QA')
        eligible = self.e.wh['fg' if lot['kind'] == 'finished' else 'rm']
        hold = self.e.wh['hold']
        current = self.balance(lot, eligible, 'QA')
        held = self.balance(lot, hold, 'QA')
        desired = min(current + max(0, held - self.return_reservation(lot)),
                      max(0, (d['qty'] if d else 0) - self.used(lot)))
        if lot['kind'] != 'finished':
            # Issued allocations stay physically available for posting reported MES usage.
            issued = sum(row['qty'] - w.get('used', {}).get(lot['batch'], 0)
                         for w in self.state['work'].values()
                         if (w.get('dispatch') or w.get('request')) and w.get('produced', 0) + w.get('rejected_remainder', 0) < w['qty'] - 1e-8
                         for row in w['inputs'] if row['batch'] == lot['batch'])
            desired = max(desired, min(current, issued))
        delta = round(desired - current, 8)
        if delta:
            source, target = (hold, eligible) if delta > 0 else (eligible, hold)
            moves = self.state['moves']
            key = 'lot-sync:' + digest([lot['batch'], len(moves)])
            move = {'batch': lot['batch'], 'source': source, 'target': target,
                    'qty': abs(delta), 'disposition': d}
            if delta > 0 and (getattr(self.e, 'config', None) or {}).get('company_operating'):
                move['lease_id'] = 'release-' + digest([self.namespace, key])
            moves[key] = move
            self.e.persist()
            if move.get('lease_id'):
                doc = self.submit_release_transfer(key, lot, move)
            else:
                row = self.batch_row({'item_code': lot['item'], 'qty': abs(delta),
                                      's_warehouse': source, 't_warehouse': target}, lot['batch'])
                doc = self.submit_operation(key, 'Stock Entry', {
                    'company': self.e.company, 'stock_entry_type': 'Material Transfer',
                    'items': [row], 'remarks': json.dumps(d, sort_keys=True),
                }, 'QA', move, lambda draft: self.check_move(lot, move, draft))
                self.confirm_move(move, doc)
        lot_sync = self.state.setdefault('synced', {})
        lot_sync[lot['batch']] = d
        self.e.persist()
        if delta:
            self.e.log('lot_sync_confirmed', batch=lot['batch'], disposition=d, stock_entry=doc['name'],
                       quantity=abs(delta), source=source, target=target)
        return True

    def inspections(self):
        # Resolve unknown submits before calculating a cumulative release budget.
        self.recover_shipments()
        self.poll_work(poll=False)
        request_path = getattr(self.e, 'run', None)
        operating = (getattr(self.e, 'config', None) or {}).get('company_operating')
        if not operating and (self.state.get('rework') or (request_path and
                (request_path/'rework-requests.json').is_file())):
            self.attempt('rework-reconciliation', 'PRODUCTION', self.poll_rework)
        uncertain = self.reserved_dispatch_batches()
        for lot in list(self.state['lots'].values()):
            if lot['batch'] not in uncertain:
                self.attempt('sync:' + lot['batch'], 'QA', lambda: self.sync(lot))

    def poll_rework(self):
        bridge = getattr(self.authority, 'rework', None)
        if bridge is None:
            raise Pending('Recurring rework requests require a configured native rework bridge')
        bridge.poll(self, self.rework_requests())

    def rework_requests(self):
        path = self.e.run/'rework-requests.json'
        try:
            requests = json.loads(path.read_text()) if path.is_file() else []
        except (OSError, ValueError) as exc:
            raise Pending('Invalid recurring rework request file') from exc
        if not isinstance(requests, list):
            raise Pending('Rework requests must be an array')
        return requests

    def available(self, lot, role):
        warehouse = self.e.wh['fg' if lot['kind'] == 'finished' else 'rm']
        balance = self.balance(lot, warehouse, role)
        d = self.disposition(lot, role)
        if (not d or d != self.state.get('synced', {}).get(lot['batch'])
                or 'sync:' + lot['batch'] in self.state.get('pending', {})):
            return 0, d
        return min(balance, max(0, d['qty'] - self.used(lot))), d

    @staticmethod
    def components(bom):
        base = Fraction(str(quantity(bom['quantity'])))
        components = {}
        for row in bom['items']:
            item = row['item_code']
            uom = row.get('stock_uom') or row.get('uom')
            previous_uom, previous_qty = components.get(item, (uom, 0))
            if not uom or uom != previous_uom:
                raise Pending('ERP BOM component does not have one stock UOM')
            components[item] = (uom, previous_qty + Fraction(str(quantity(row.get('stock_qty') or row['qty']))) / base)
        if not components:
            raise Pending('ERP BOM has no stock components')
        return components

    def definition(self, item):
        revision = self.equipment.native(self.authority.definition, item, as_of=self.e.date(), role='PLANNER')
        if (not ids(revision, 'id', 'version', 'erp_bom', 'approval_id', 'erp_ack_id', 'mes_ack_id',
                    'effective_from') or revision.get('item') != item
                or revision.get('approved') is not True or revision.get('acknowledged') is not True):
            raise Pending('No acknowledged effective approved recipe')
        try:
            start = date.fromisoformat(revision['effective_from'])
            end = date.fromisoformat(revision['effective_until']) if revision.get('effective_until') else date.max
        except (TypeError, ValueError) as exc:
            raise Pending('Invalid definition effectivity') from exc
        if not start <= date.fromisoformat(self.e.date()) < end:
            raise Pending('Approved recipe is outside its effective period')
        bom = self.e.api['PLANNER'].get('BOM', revision['erp_bom'])
        if bom['item'] != item or bom['docstatus'] != 1 or not bom.get('is_active'):
            raise Pending('Approved ERP BOM is unavailable or does not match item')
        expected_site = revision.get('receiver_markers', {}).get('erp')
        if expected_site is not None:
            from integrations.revisions.receivers import ERP_SITE
            if bom.get(ERP_SITE) != expected_site:
                raise Pending('Planning ERP BOM belongs to a different receiver')
        components = self.components(bom)
        bulk = revision.get('intermediate_inputs', [])
        if (any(code not in components for code in [*bulk, *revision.get('input_steps', {})])
                or item in components):
            raise Pending('Intermediate profile conflicts with the approved ERP BOM')
        if revision.get('material_kind') == 'intermediate' or bulk:
            units = {uom: self.e.api['PLANNER'].get('UOM', uom) for uom in {u for u, _ in components.values()}}
            revision = deepcopy(revision)
            for code, (uom, _) in components.items():
                erp_step = Fraction(1) if units[uom].get('must_be_whole_number') else Fraction(1, 100_000_000)
                mes_step = Fraction(str(revision.get('input_steps', {}).get(code, erp_step)))
                revision.setdefault('input_steps', {})[code] = float(Fraction(
                    math.lcm(erp_step.numerator, mes_step.numerator),
                    math.gcd(erp_step.denominator, mes_step.denominator)))
        if revision.get('material_kind') == 'intermediate':
            master = self.e.api['PLANNER'].get('Item', item)
            unit = self.e.api['PLANNER'].get('UOM', revision['stock_uom'])
            step = quantity(revision.get('stock_step'))
            if (master['stock_uom'] != revision['stock_uom'] or not master.get('has_batch_no')
                    or unit.get('must_be_whole_number') or step < 1e-8):
                raise Pending('Intermediate must use a tracked, decimal native stock UOM')
        return deepcopy(revision), components

    @staticmethod
    def work_step(recipe, output_step):
        revision, components = recipe
        step = Fraction(str(output_step))
        # Select a quantity that is representable in the output and every input UOM.
        for code, stock_increment in revision.get('input_steps', {}).items():
            ratio = Fraction(components[code][1])
            required = Fraction(str(stock_increment)) / ratio
            step = Fraction(math.lcm(step.numerator, required.numerator),
                            math.gcd(step.denominator, required.denominator))
        return float(step)

    def forecast_components(self, item):
        revision, components = self.definition(item)
        if not revision.get('intermediate_inputs'):
            return None
        purchased = {}
        for code, (uom, per) in components.items():
            if code in revision['intermediate_inputs']:
                bulk, inputs = self.definition(code)
                if (bulk.get('material_kind') != 'intermediate' or bulk['stock_uom'] != uom
                        or bulk.get('intermediate_inputs')):
                    raise Pending('Invalid intermediate definition for forecast')
                for raw, (_, raw_per) in inputs.items():
                    purchased[raw] = purchased.get(raw, 0) + per * raw_per
            else:
                purchased[code] = purchased.get(code, 0) + per
        return purchased

    @staticmethod
    def input_lot(lot, item, uom, intermediate):
        return ((lot['item'], lot['uom']) == (item, uom)
                and (lot['kind'] == 'intermediate' and lot.get('mes_lot_id') if intermediate
                     else lot['kind'] not in ('finished', 'intermediate')))

    def reserved_inputs(self):
        reserved = {}
        for work in self.state['work'].values():
            if work.get('produced', 0) + work.get('rejected_remainder', 0) >= work['qty'] - 1e-8:
                continue
            for row in work['inputs']:
                reserved[row['batch']] = reserved.get(row['batch'], 0) + row['qty'] - work.get('used', {}).get(row['batch'], 0)
        return reserved

    def released_batches(self, warehouse):
        return {move['batch'] for move in self.state['moves'].values()
                if move.get('done') and move['source'] == self.e.wh['hold']
                and move['target'] == self.e.wh[warehouse]}

    def finished_supply(self):
        # Fulfillment pools finished lots by item. Planning must use that same pool.
        supply = {}
        for work in self.state['work'].values():
            if work.get('order'):
                remaining = work['qty'] - work.get('produced', 0) - work.get('rejected_remainder', 0)
                supply[work['item']] = supply.get(work['item'], 0) + max(0, remaining)
        repacked = {op['request']['batch'] for op in self.state.get('rework', {}).values() if op.get('receipt')}
        rejected = self.e.state.get('operating_lot_rejections', {})
        released_before = self.released_batches('fg')
        synced = self.state.get('synced', {})
        shipped = {}
        for delivery in self.state['shipments'].values():
            if delivery.get('target_id'):
                for row in delivery['rows']:
                    shipped[row['batch']] = shipped.get(row['batch'], 0) + row['qty']
        for batch, lot in self.state['lots'].items():
            if lot['kind'] == 'finished' and batch not in rejected and batch not in repacked:
                disposition = synced.get(batch)
                if disposition:
                    covering = disposition['qty'] if disposition['status'] == 'released' else 0
                else:
                    covering = 0 if batch in released_before else lot['qty']
                supply[lot['item']] = supply.get(lot['item'], 0) + max(0, covering - shipped.get(batch, 0))
        return supply

    def retire_overplanned_work(self):
        # A previous planner could count an order's output twice after another
        # order shipped it. Retain its proposal and release the reserved inputs
        # only after verifying that ERP rejected creation of the Work Order.
        for key, work in list(self.state['work'].items()):
            operation = 'mes-dispatch:' + key
            rejection = self.state.get('pending', {}).get(operation, {})
            if (work.get('request') or 'OverProductionError' not in rejection.get('last_error', '')
                    or self.e.recover_company_doc(key, 'Work Order', 'PLANNER') is not None):
                continue
            self.state.setdefault('withdrawn_work', {})[key] = {
                'proposal': work, 'reason': rejection['last_error'], 'day': self.e.day}
            del self.state['work'][key], self.state['pending'][operation]
            self.e.persist()

    def build_plan(self):
        e = self.e
        work = self.state['work']
        reserved = self.reserved_inputs()
        free, capacity, planned_inputs = {}, {}, {}
        proposals, shortages, bulk_demand, definitions = [], set(), {}, {}

        def definition(item):
            if item not in definitions:
                definitions[item] = self.attempt('definition:' + item, 'PLANNER', lambda: self.definition(item))
            return definitions[item]

        def released(item, uom, intermediate=False):
            key = item, uom, intermediate
            if key not in free:
                free[key] = 0
                for lot in self.state['lots'].values():
                    if not self.input_lot(lot, item, uom, intermediate):
                        continue
                    eligible = self.attempt('plan-lot:' + lot['batch'], 'PLANNER',
                                            lambda: self.available(lot, 'PLANNER'))
                    if eligible:
                        free[key] += max(0, eligible[0] - reserved.get(lot['batch'], 0))
            return max(0, free[key] - planned_inputs.get(key, 0))

        def propose(key, order, item, target, recipe, step):
            revision, components = recipe

            def equipment():
                center = self.equipment.native(self.authority.work_center, revision, role='PLANNER')
                return self.equipment.available(center, 'PLANNER')

            center = self.attempt('equipment:' + item, 'PLANNER', equipment)
            if center is None:
                return 0
            limit = capacity.setdefault(center, e.config['capacity_units'] - sum(
                w['qty'] for w in work.values() if w.get('slot_day') == e.day and w.get('equipment') == center))
            bulk = set(revision.get('intermediate_inputs', []))
            inputs = {(code, uom, code in bulk): per for code, (uom, per) in components.items()}
            legacy = not bulk and revision.get('material_kind') != 'intermediate'
            qty = stock_steps(max(0, min(target, limit, *(released(*component) / per
                                  for component, per in inputs.items()))), step)
            if qty <= 0 or (not legacy and qty < min(target, max(limit, 0))):
                threshold = min(target, step) if legacy else min(target, max(limit, 0))
                shortages.update(component[0] for component, per in inputs.items()
                                 if not component[2] and released(*component) + 1e-8 < per * threshold)
            if qty <= 0:
                return 0
            proposals.append(dict(key=key, order=order, item=item, qty=qty, day=e.day,
                                  revision=revision, equipment=center))
            capacity[center] -= qty
            for component, per in inputs.items():
                planned_inputs[component] = planned_inputs.get(component, 0) + qty * per
            return qty

        supply = self.finished_supply()
        for key, order in e.state['ops']['orders'].items():
            order_doc = e.get(key, 'PLANNER')
            if order_doc.get('docstatus', 1) != 1 or order_doc.get('status') in ('Closed', 'On Hold', 'Completed'):
                continue
            row, = order_doc['items']
            outstanding = max(0, row['qty'] - row.get('delivered_qty', 0))
            item = order['item']
            covered = min(outstanding, supply.get(item, 0))
            supply[item] = supply.get(item, 0) - covered
            remaining = outstanding - covered
            if outstanding <= 0 or remaining <= 0:
                continue
            item = order['item']
            recipe = definition(item)
            if not recipe:
                continue
            revision, components = recipe
            if revision.get('material_kind') == 'intermediate':
                continue
            bulk_codes = revision.get('intermediate_inputs', [])
            packing = propose(f'd{e.day:04}-work-' + key, key, item, remaining, recipe,
                              self.work_step(recipe, 1) if bulk_codes else
                              (1 if order.get('production_profile') or e.config.get('company_operating') else 12))
            for code in bulk_codes:
                uom, per = components[code]
                bulk_demand[code, uom] = bulk_demand.get((code, uom), 0) + (remaining - packing) * per

        if bulk_demand:
            rejected = e.state.get('operating_lot_rejections', {})
            previously_released = self.released_batches('rm')

        for (code, uom), need in bulk_demand.items():
            in_process = sum(max(0, w['qty'] - w.get('produced', 0) - w.get('rejected_remainder', 0))
                             for w in work.values() if w['item'] == code and w['uom'] == uom
                             and w.get('order') is None)
            # Posted lots awaiting their first disposition still cover future demand.
            awaiting = sum(lot['qty'] for lot in self.state['lots'].values()
                           if lot['kind'] == 'intermediate' and (lot['item'], lot['uom']) == (code, uom)
                           and lot['batch'] not in rejected and lot['batch'] not in previously_released
                           and self.state.get('synced', {}).get(lot['batch']) is None)
            deficit = need - released(code, uom, intermediate=True) - in_process - awaiting
            if deficit > 1e-8:
                bulk = definition(code)
                if (bulk and bulk[0].get('material_kind') == 'intermediate'
                        and bulk[0]['stock_uom'] == uom and not bulk[0].get('intermediate_inputs')):
                    step = self.work_step(bulk, bulk[0]['stock_step'])
                    propose(f'd{e.day:04}-bulk-' + digest(code), None, code,
                            stock_steps(deficit, step, ROUND_CEILING), bulk, step)
        return {'work': proposals, 'shortages': sorted(shortages)}

    def order_covers(self, op, quantity_needed, role):
        order = self.e.get(op['order'], role)
        if (order.get('name') != op['sales_order'] or order.get('docstatus') != 1
                or order.get('status') in ('On Hold', 'Closed', 'Completed', 'Cancelled')):
            return False
        lines = [r for r in order['items'] if r['name'] == op['sales_order_item']]
        if len(lines) != 1:
            return False
        line = lines[0]
        if line.get('item_code') != op['item'] or line.get('conversion_factor', 1) != 1:
            return False
        open_qty = quantity(line['qty'], zero=True) - quantity(line.get('delivered_qty', 0), zero=True)
        return quantity_needed <= open_qty + 1e-8

    def pin_work(self, plan):
        revision, components = self.definition(plan['item'])
        if revision != plan['revision']:
            raise Pending('Approved recipe changed after capacity planning')
        intermediate = revision.get('material_kind') == 'intermediate'
        if intermediate != (plan['order'] is None):
            raise Pending('Only intermediate work may omit a sales line')
        sales = self.e.get(plan['order'], 'PLANNER') if not intermediate else None
        sales_row = sales['items'][0] if sales else None
        reserved = self.reserved_inputs()
        inputs = []
        for item, (uom, unit_qty) in components.items():
            need = unit_qty * plan['qty']
            for lot in self.state['lots'].values():
                if not self.input_lot(lot, item, uom, item in revision.get('intermediate_inputs', [])):
                    continue
                eligibility = self.attempt('plan-lot:' + lot['batch'], 'PLANNER',
                                           lambda: self.available(lot, 'PLANNER'))
                if eligibility is None:
                    continue
                available, disposition = eligibility
                take = round(min(need, max(0, available - reserved.get(lot['batch'], 0))), 8)
                if take > 0:
                    inputs.append({'batch': lot['batch'], 'item': lot['item'], 'qty': take,
                                   'uom': lot['uom'], 'disposition': disposition,
                                   **({'mes_lot_id': lot['mes_lot_id']} if lot['kind'] == 'intermediate' else {})})
                    reserved[lot['batch']] = reserved.get(lot['batch'], 0) + take
                    need -= take
                if need <= 1e-8:
                    break
            if need > 1e-8:
                raise Pending('Insufficient authorized input lots for ' + item)
        work = {**plan, 'sales_order': sales['name'] if sales else None,
                'sales_order_item': sales_row['name'] if sales_row else None,
                'revision': revision, 'inputs': inputs, 'equipment': plan['equipment'],
                'uom': revision['stock_uom'] if intermediate else sales_row['stock_uom']}
        if sales:
            linked = sum(other['qty'] for other in self.state['work'].values()
                         if other.get('sales_order_item') == sales_row['name'] and not other.get('stock_replenishment'))
            if plan['qty'] + max(linked, sales_row.get('work_order_qty') or 0) > sales_row['qty'] + 1e-8:
                # Replacing rejected/scrapped units still serves real open demand.
                # ERP's order-linked quota includes the original authorization.
                work['stock_replenishment'] = True
        work['scoped_mes_staging'] = True
        self.state['work'][plan['key']] = work
        self.e.persist()
        return work

    def dispatch(self, work):
        e = self.e
        if work.get('dispatch'):
            return
        key = work['key']
        self.recover_dispatch(work)
        if work.get('dispatch'):
            return
        previous = e.recover_company_doc(key, 'Work Order', 'PLANNER')
        revision = work['revision']
        if previous is not None and previous.get('docstatus') == 2:
            raise Pending('Cancelled Work Order cannot dispatch')
        released = previous is not None and previous.get('docstatus') == 1
        blocked_order = (not released and work.get('order')
                         and not self.order_covers(work, work['qty'], 'PLANNER'))
        if not released and (blocked_order or self.definition(work['item'])[0] != revision):
            reason = ('Sales order no longer covers planned work' if blocked_order
                      else 'Definition superseded before ERP work release')
            if previous is None and not work.get('request'):
                # No external work was created. Free the original input lots so
                # the next plan can reserve against the newly approved recipe.
                self.state.setdefault('withdrawn_work', {})[key] = {
                    'proposal': work, 'reason': reason, 'day': e.day}
                del self.state['work'][key]
                e.persist()
                return
            raise Pending(reason)
        center = self.equipment.native(self.authority.work_center, revision, role='PRODUCTION')
        if work.get('equipment') in (None, 'Filler') and (
                not work.get('request') or self.authority.legacy_equipment() == center):
            work['equipment'] = center
            e.persist()
        if work.get('equipment') != center:
            raise Pending('Pinned work equipment differs from the acknowledged MES route')
        self.equipment.available(center, 'PRODUCTION')
        dispatched_today = sum(w['qty'] for w in self.state['work'].values()
                               if w.get('slot_day') == e.day and w.get('equipment') == center and w is not work)
        if dispatched_today + work['qty'] > e.config['capacity_units']:
            raise Pending('Equipment dispatch capacity exhausted for this shift')
        order_link = None if work.get('stock_replenishment') else work['sales_order']
        line_link = None if work.get('stock_replenishment') else work['sales_order_item']
        definition = dict(work['revision'])
        if work.get('stock_replenishment'):
            definition.update(replenishes_sales_order=work['sales_order'], replenishes_line=work['sales_order_item'])
        if previous is None:
            definition['erp_released_on'] = e.date()
        else:
            try:
                native_pin = json.loads(previous['custom_norvale_definition'])
                if 'erp_released_on' in native_pin:
                    definition['erp_released_on'] = native_pin['erp_released_on']
            except (KeyError, TypeError, ValueError) as exc:
                raise Pending('Native Work Order definition is missing or unreadable') from exc
            if native_pin != definition:
                raise Pending('Native Work Order definition differs from the reserved work')
        # A submitted work order owns its original definition. Recheck its
        # native BOM fingerprint in the bridge even if a newer revision is effective.
        def before_submit(draft):
            if work.get('order') and not self.order_covers(work, draft['qty'], 'PLANNER'):
                raise Pending('Sales order no longer covers the draft Work Order')

        wo = previous if released else e.write(key, 'Work Order', {
            'production_item': work['item'], 'bom_no': work['revision']['erp_bom'],
            'qty': work['qty'], 'company': e.company, 'use_multi_level_bom': 0,
            'skip_transfer': 1, 'source_warehouse': e.wh['rm'], 'wip_warehouse': e.wh['wip'],
            'fg_warehouse': e.wh['hold'], 'planned_start_date': e.date() + ' 08:00:00',
            'sales_order': order_link, 'sales_order_item': line_link,
            'custom_norvale_definition': json.dumps(definition, sort_keys=True),
        }, 'PLANNER', True, before_submit=before_submit)
        if (wo['bom_no'] != work['revision']['erp_bom'] or wo.get('company') != e.company
                or (wo.get('sales_order') or None) != order_link
                or (wo.get('sales_order_item') or None) != line_link
                or wo['production_item'] != work['item'] or wo['qty'] != work['qty']
                or wo.get('custom_norvale_definition') != json.dumps(definition, sort_keys=True)):
            raise Pending('Work Order readback differs from pinned request')
        for row in work['inputs']:
            available, disposition = self.available(self.state['lots'][row['batch']], 'PLANNER')
            if available + 1e-8 < row['qty'] or disposition != row['disposition']:
                raise Pending('Pinned input disposition changed before MES dispatch')
        try:
            release_day = date.fromisoformat(definition.get('erp_released_on') or str(wo['planned_start_date'])[:10])
            if not date.fromisoformat(revision.get('effective_from', release_day.isoformat())) <= release_day <= date.fromisoformat(e.date()):
                raise ValueError('Invalid release day')
        except (KeyError, ValueError) as exc:
            raise Pending('Work Order lacks a consistent release date') from exc
        request = work.get('request') or {
            **{k: work[k] for k in ('sales_order', 'sales_order_item', 'item', 'uom',
                                    'qty', 'revision', 'equipment', 'inputs')},
            'key': self.namespace + ':' + key, 'work_order': wo['name'], 'as_of': e.date(),
            'released_on': release_day.isoformat(),
        }
        work.update(request=request, slot_day=e.day)
        e.persist()
        ack = self.equipment.native(self.authority.dispatch, deepcopy(request), role='PRODUCTION')
        self.confirm_dispatch(work, ack)

    def recover_dispatch(self, work):
        if work.get('request') and not work.get('dispatch'):
            found = self.equipment.native(self.authority.find_dispatch, work['request']['key'], role='PRODUCTION')
            if found is not None:
                self.confirm_dispatch(work, found)

    def confirm_dispatch(self, work, ack):
        if not ids(ack, 'id', 'version') or ack.get('request') != work['request']:
            raise Pending('MES dispatch readback differs from pinned request')
        source = work['request'].get('revision', {}).get('mes_source')
        if ((source is not None and source != ack.get('source'))
                or ('source' in ack and not ids(ack, 'source'))):
            raise Pending('MES dispatch came from a different source instance')
        work['dispatch'] = {k: ack[k] for k in ('id', 'version', 'source') if k in ack}
        self.e.persist()

    def poll_work(self, *, poll=True):
        if poll:
            for work in self.state['work'].values():
                self.attempt('mes-reconcile:' + work['key'], 'PRODUCTION', lambda: self.recover_dispatch(work))
                if work.get('dispatch'):
                    self.attempt('mes-poll:' + work['key'], 'PRODUCTION', lambda: self.equipment.native(self.collect, work))
        for key, operation in list(self.state['completions'].items()):
            if operation.get('done'):
                continue
            self.attempt(key, 'PRODUCTION', lambda: self.post_completion(key, operation))

    def collect(self, work):
        for report in self.authority.completions(dict(work['dispatch']), as_of=self.e.date(), role='PRODUCTION'):
            if not ids(report, 'id', 'version'):
                raise Pending('Missing MES completion identity')
            source = work['dispatch'].get('source')
            if source != report.get('source'):
                raise Pending('MES completion came from a different source instance')
            key = 'mes-completion:' + digest([source, report['id']] if source else report['id'])
            operation = {'work': work['key'], 'report': deepcopy(report)}
            existing = self.state['completions'].setdefault(key, operation)
            if existing['work'] != work['key'] or existing['report'] != report:
                raise Pending('MES changed an immutable completion report')
            self.e.persist()

    @staticmethod
    def match_batch_rows(actual, expected, batch_field):
        by_batch = {row.get('batch_no'): row for row in actual}
        expected_batches = {row[batch_field] for row in expected}
        if len(by_batch) != len(actual) or len(actual) != len(expected) or by_batch.keys() != expected_batches:
            raise Pending('ERP document batch rows differ from allocation')
        return [(by_batch[row[batch_field]], row) for row in expected]

    def post_completion(self, key, operation):
        e = self.e
        work = self.state['work'][operation['work']]
        report = operation['report']
        self.equipment.guard_execution(report.get('id'))
        wo = work['request']['work_order']
        if (report.get('dispatch_id') != work['dispatch']['id'] or report.get('work_order') != wo
                or report.get('revision') != work['revision'] or report.get('authorized') is not True
                or not isinstance(report.get('inputs'), list)
                or not ids(report.get('output'), 'lot_id', 'item', 'uom')):
            raise Pending('MES completion does not match authorized work and pinned revision')
        other = [p['report'] for k, p in self.state['completions'].items()
                 if k != key and p['work'] == work['key'] and p.get('target_id')]
        inputs = {row['batch']: row for row in work['inputs']}
        rows = []
        seen = set()
        for row in report['inputs']:
            if not ids(row, 'batch', 'item', 'uom'):
                raise Pending('MES input lot identity or UOM is missing')
            batch = row['batch']
            pinned = inputs.get(batch)
            used = sum(r['qty'] for p in other for r in p['inputs'] if r['batch'] == batch)
            qty = quantity(row.get('qty'))
            if (not pinned or batch in seen or row['item'] != pinned['item'] or row['uom'] != pinned['uom']
                    or used + qty > pinned['qty'] + 1e-8
                    or row.get('mes_lot_id') != pinned.get('mes_lot_id')
                    or (pinned.get('mes_lot_id') and not isinstance(row.get('mes_move'), dict))):
                raise Pending('MES consumption exceeds or differs from pinned input allocation')
            seen.add(batch)
            rows.append(self.batch_row({'item_code': row['item'], 'qty': qty,
                                        's_warehouse': e.wh['rm']}, batch))
        output = report['output']
        qty = quantity(output.get('qty'))
        if (not seen or output['item'] != work['item']
                or output['uom'] != work['uom']
                or abs(qty - stock_steps(qty, work['revision'].get('stock_step', 1))) > 1e-8
                or qty + sum(p['output']['qty'] for p in other) > work['qty'] + 1e-8):
            raise Pending('MES output exceeds or differs from pinned work')
        revision = work['revision']
        intermediate = revision.get('material_kind') == 'intermediate'
        if intermediate or revision.get('intermediate_inputs'):
            # The approved BOM basis accounts for process yield in native units.
            expected, consumed = {}, {}
            for row in inputs.values():
                expected[row['item']] = expected.get(row['item'], 0) + row['qty'] * qty / work['qty']
            for row in report['inputs']:
                consumed[row['item']] = consumed.get(row['item'], 0) + row['qty']
            if any(consumed.get(item, 0) + 1e-8 < need for item, need in expected.items()):
                raise Pending('MES consumption cannot support output at the pinned BOM yield')
        source = work['dispatch'].get('source')
        if source != report.get('source'):
            raise Pending('MES posting source differs from dispatch')
        lot_identity = ['MES', source, output['lot_id']] if source else ['MES', output['lot_id']]
        batch = self.batch(lot_identity, output['item'])
        existing = self.state['lots'].get(batch)
        if existing and existing.get('completion_id') != report['id']:
            raise Pending('MES output lot already belongs to another completion')
        rows.append(self.batch_row({'item_code': output['item'], 'qty': qty,
                                    'is_finished_item': 1, 't_warehouse': e.wh['hold']}, batch))
        doc = e.recover_company_doc(key, 'Stock Entry', 'PRODUCTION')
        if not doc or doc['docstatus'] == 0:
            for row in report['inputs']:
                if self.balance(self.state['lots'][row['batch']], e.wh['rm'], 'PRODUCTION') + 1e-8 < row['qty']:
                    raise Pending('Actual MES input lot insufficient in ERP; reconcile consumption')
        remarks = {
            'completion_id': report['id'], 'version': report['version'],
            'dispatch_id': report['dispatch_id'], 'revision': revision,
            'output_lot_id': output['lot_id'],
            **({'mes_source': report['source']} if report.get('source') else {}),
        }
        previously_submitted = bool(doc and doc['docstatus'] == 1)
        if revision.get('intermediate_inputs'):
            remarks['intermediate_inputs'] = [
                dict(batch=r['batch'], mes_lot_id=r['mes_lot_id'], mes_move=r['mes_move'],
                     erp_manufacture=self.state['lots'][r['batch']]['source_id'],
                     qms_release=inputs[r['batch']]['disposition']['id'],
                     qms_version=inputs[r['batch']]['disposition']['version'],
                     qms_source=inputs[r['batch']]['disposition'].get('qms_source'))
                for r in report['inputs'] if r.get('mes_lot_id')
            ]
        def check_pin(record, status):
            if (record.get('docstatus') != status or record.get('company') != e.company
                    or record.get('stock_entry_type') != 'Manufacture'
                    or record.get('bom_no') != revision['erp_bom']):
                raise Pending('Manufacture posting differs from the pinned company, BOM or status')
            try:
                saved = json.loads(record.get('remarks') or '{}')
            except (ValueError, TypeError) as exc:
                raise Pending('Manufacture posting lacks readable MES provenance') from exc
            if saved == remarks:
                return
            if previously_submitted and 'intermediate_inputs' in remarks:
                legacy = deepcopy(remarks)
                for row in legacy['intermediate_inputs']:
                    row.pop('qms_version')
                    row.pop('qms_source')
                if saved == legacy:
                    return
            raise Pending('Manufacture posting differs from the pinned MES completion')

        def before_submit(draft):
            check_pin(draft, 0)
            self.check_completion_draft(draft, wo, qty, rows)

        if not doc or doc['docstatus'] == 0:
            doc = e.write(key, 'Stock Entry', {
                'company': e.company, 'stock_entry_type': 'Manufacture', 'work_order': wo,
                'bom_no': revision['erp_bom'], 'from_bom': 1, 'fg_completed_qty': qty,
                'items': rows, 'remarks': json.dumps(remarks, sort_keys=True),
            }, 'PRODUCTION', True, before_submit=before_submit)
        check_pin(doc, 1)
        if doc['work_order'] != wo or doc['fg_completed_qty'] != qty or len(doc['items']) != len(rows):
            raise Pending('MES posting readback differs from intended work and quantities')
        matched = self.match_batch_rows(doc['items'], rows, 'batch_no')
        for actual, expected in matched:
            if actual['item_code'] != expected['item_code']:
                raise Pending('MES posting item differs from report')
            source = expected.get('s_warehouse')
            self.verify_row(doc, actual, expected['batch_no'], expected['qty'],
                            source or e.wh['hold'], 'Outward' if source else 'Inward', 'PRODUCTION')
        self.register(batch, output['item'], qty, output['uom'],
                      'intermediate' if intermediate else 'finished', doc,
                      completion_id=report['id'], completion_version=report['version'],
                      mes_lot_id=output['lot_id'], revision=work['revision'], work_order=wo,
                      source_row=matched[-1][0].get('name'))
        operation.update(target_id=doc['name'], done=True)
        work['produced'] = qty + sum(p['output']['qty'] for p in other)
        work['used'] = {b: sum(r['qty'] for p in [*other, report] for r in p['inputs'] if r['batch'] == b)
                        for b in inputs}
        e.state['ops']['meter'] = sum(w.get('produced', 0) for w in self.state['work'].values()
                                      if w.get('revision', {}).get('material_kind') != 'intermediate')
        e.state['physical'][key] = {'produced': qty, 'completed_day': e.day, 'awaits_inspection': True}
        e.persist()
        e.log('physical_production', native_work_order=wo, native_stock_entry=doc['name'],
              lot=batch, item=output['item'], qty=qty, completion_id=report['id'])

    def check_completion_draft(self, draft, wo, qty, rows):
        if (draft['work_order'] != wo or draft['fg_completed_qty'] != qty
                or len(draft['items']) != len(rows)):
            raise Pending('Manufacture draft differs from MES completion')
        for actual, expected in self.match_batch_rows(draft['items'], rows, 'batch_no'):
            for field in ('item_code', 'qty', 'batch_no', 's_warehouse', 't_warehouse', 'is_finished_item'):
                if (actual.get(field) or None) != (expected.get(field) or None):
                    raise Pending('Manufacture draft differs from MES lot allocation')
            if actual.get('transfer_qty', actual['qty']) != expected['qty']:
                raise Pending('ERP stock-unit transfer quantity differs from MES lot allocation')
            if actual.get('serial_and_batch_bundle'):
                raise Pending('Unexpected preassigned bundle on manufacture draft')

    def dispatch_lease(self, request, outcome):
        change = getattr(self.authority, 'dispatch_lease', None)
        if not callable(change):
            raise Pending('Recurring dispatch requires a native QMS lease adapter')
        ack = change(deepcopy(request), outcome=outcome, role='WAREHOUSE')
        closed = ('denied', 'aborted', 'withdrawn')
        permitted = {
            'active': ('active', 'armed', *closed),
            'armed': ('armed',),
            'status': ('active', 'armed', 'absent', 'submitted', *closed),
            'withdrawn': closed,
            'submitted': ('submitted',),
        }
        if (not isinstance(ack, dict) or ack.get('state') not in permitted[outcome]
                or ack.get('id') != request['id'] or ack.get('delivery') != request['delivery']):
            raise Pending('QMS dispatch lease receipt differs from the request')
        return deepcopy(ack)

    def abandon_lease(self, shipment, receipt):
        if (getattr(self.e, 'config', None) or {}).get('company_operating'):
            allocations = [{'batch': row['batch'], 'quantity': row['qty'],
                            'uom': self.state['lots'][row['batch']]['uom']} for row in shipment['rows']]
            shipment.setdefault('allocation_change', {
                'delivery': receipt['delivery'], 'day': self.e.day, 'allocations': allocations})
        shipment.update(abandoned=True, lease_receipt=receipt)
        self.e.persist()

    def withdraw_shipment(self, shipment):
        if shipment.get('submit_started'):
            raise Pending('An attempted submit requires ERP reconciliation')
        shipment['withdraw_requested'] = True
        self.e.persist()
        receipt = self.dispatch_lease(shipment['lease_request'], 'withdrawn')
        self.abandon_lease(shipment, receipt)

    def submit_shipment(self, shipment, data):
        def guard(draft):
            if shipment.get('submit_started'):
                raise Pending('Delivery submit already attempted')
            try:
                self.confirm_dispatch_eligibility(shipment, draft)
            except Pending:
                if shipment.get('lease_request'):
                    self.withdraw_shipment(shipment)
                raise
            request = {
                'id': shipment['lease_id'], 'delivery': draft['name'],
                'scopes': [
                    {'lot': self.state['lots'][row['batch']], 'disposition': row['disposition'],
                     'dispatch_qty': row['qty']}
                    for row in sorted(shipment['rows'], key=lambda row: row['batch'])
                ],
            }
            if shipment.setdefault('lease_request', deepcopy(request)) != request:
                raise Pending('Delivery draft differs from the original lease request')
            self.e.persist()
            receipt = self.dispatch_lease(request, 'active')
            shipment['lease_receipt'] = receipt
            if receipt['state'] not in ('active', 'armed'):
                self.abandon_lease(shipment, receipt)
                raise Pending('QMS dispatch lease ' + receipt['state'])
            needed = sum(row['qty'] for row in shipment['rows']) + self.reserved_delivery(
                shipment['sales_order'], shipment['sales_order_item'], excluding=shipment)
            if receipt['state'] == 'active' and not self.order_covers(shipment, needed, 'WAREHOUSE'):
                self.withdraw_shipment(shipment)
                raise Pending('Sales order no longer covers the allocated delivery')
            shipment['lease_receipt'] = self.dispatch_lease(request, 'armed')
            if shipment['lease_receipt'].get('submit_permitted') is not True:
                raise Pending('Native QMS arm already issued its submit permission')
            # Publish before sending the PUT. Even a crash between here and send
            # requires proof of quiescence before someone can release the lease.
            shipment['submit_started'] = True
            self.e.persist()

        doc = self.e.write(shipment['key'], 'Delivery Note', data, 'WAREHOUSE', True, before_submit=guard)
        self.confirm_shipment(shipment, doc)

    def unresolved_shipments(self, excluding=None):
        return (shipment for shipment in self.state['shipments'].values()
                if shipment is not excluding and not shipment.get('target_id')
                and not shipment.get('abandoned'))

    def reserved_delivery(self, sales_order, sales_order_item, excluding=None):
        return sum(row['qty'] for shipment in self.unresolved_shipments(excluding)
                   if (shipment['sales_order'], shipment['sales_order_item'])
                   == (sales_order, sales_order_item)
                   for row in shipment['rows'])

    def reserved_dispatch_batches(self, excluding=None):
        # QMS allows only one open lease per batch, even for a partial allocation.
        return {row['batch'] for shipment in self.unresolved_shipments(excluding)
                for row in shipment['rows']}

    def reconcile_shipment(self, shipment):
        self.attempt('delivery-reconciliation:' + shipment['key'], 'WAREHOUSE',
                     lambda: self.recover_shipment(shipment))

    def recover_shipments(self):
        for shipment in self.state['shipments'].values():
            self.reconcile_shipment(shipment)
        # Older checkpoints tracked pending reconciliation in a single entry.
        if self.state.get('pending', {}).pop('delivery-reconciliation', None) is not None:
            self.e.persist()

    def recover_shipment(self, shipment):
        # The terminal checkpoint can have persisted before attempt cleared its pending entry.
        if shipment.get('target_id') or shipment.get('abandoned'):
            return
        doc = self.e.recover_company_doc(shipment['key'], 'Delivery Note', 'WAREHOUSE')
        request = shipment.get('lease_request')
        if doc and doc['docstatus'] == 1:
            self.confirm_shipment(shipment, doc)
        elif shipment.get('withdraw_requested'):
            self.withdraw_shipment(shipment)
        elif request:
            receipt = self.dispatch_lease(request, 'status')
            if receipt['state'] in ('aborted', 'denied', 'withdrawn'):
                self.abandon_lease(shipment, receipt)
            elif (doc and doc['docstatus'] == 0 and not shipment.get('submit_started')
                  and receipt['state'] in ('active', 'absent')):
                self.submit_shipment(shipment, doc)
            else:
                raise Pending('Delivery outcome unknown; QMS lease retained for reconciliation')
        elif shipment.get('submit_started') or (doc and doc['docstatus'] != 0):
            raise Pending('Unknown or cancelled legacy delivery requires reconciliation')
        else:
            shipment['abandoned'] = True
            self.e.persist()

    @staticmethod
    def shipment_releases(shipment):
        lease_id = shipment.get('lease_id')
        if not lease_id:
            return shipment['rows']
        return [{**row, 'qms_lease': lease_id} for row in shipment['rows']]

    def verify_release_evidence(self, doc, shipment):
        raw = doc.get('custom_norvale_quality_releases') or doc.get('remarks')
        try:
            recorded = json.loads(raw)
        except (TypeError, ValueError):
            recorded = None
        if recorded != self.shipment_releases(shipment):
            raise Pending('Delivery Note lacks the exact allocated lot and QMS release evidence')

    def confirm_shipment(self, shipment, doc):
        self.verify_release_evidence(doc, shipment)
        rows = shipment['rows']
        if len(rows) != len(doc['items']):
            raise Pending('Delivery Note row count differs from allocation')
        for actual, row in self.match_batch_rows(doc['items'], rows, 'batch'):
            if (actual['against_sales_order'] != shipment['sales_order']
                    or actual['so_detail'] != shipment['sales_order_item']
                    or actual['item_code'] != shipment['item']):
                raise Pending('Delivery Note sales references differ from allocation')
            self.verify_row(doc, actual, row['batch'], row['qty'], self.e.wh['fg'], 'Outward', 'WAREHOUSE')
        request = shipment.get('lease_request')
        if shipment.get('lease_id') and not request:
            raise Pending('Submitted Delivery Note has no guarded dispatch attempt')
        if request:
            if not shipment.get('submit_started') or request['delivery'] != doc['name']:
                raise Pending('Submitted Delivery Note differs from the guarded attempt')
            shipment['lease_receipt'] = self.dispatch_lease(request, 'submitted')
        shipment['target_id'] = doc['name']
        shipment['departed_day'] = shipment['day']
        self.e.state['physical'][shipment['key']] = {
            'departed_day': shipment['day'], 'qty': sum(r['qty'] for r in rows),
            'item': shipment['item'], 'lots': [r['batch'] for r in rows],
        }
        self.e.persist()
        self.e.log('physical_dispatch', native_delivery=doc['name'], item=shipment['item'],
                   qty=sum(r['qty'] for r in rows), departed_day=shipment['day'],
                   lots=[r['batch'] for r in rows], qms_lease=shipment.get('lease_receipt'))

    def confirm_dispatch_eligibility(self, shipment, draft):
        self.verify_release_evidence(draft, shipment)
        if len(draft['items']) != len(shipment['rows']):
            raise Pending('Delivery draft row count differs from allocation')
        reserved = self.reserved_dispatch_batches(excluding=shipment)
        for actual, row in self.match_batch_rows(draft['items'], shipment['rows'], 'batch'):
            if row['batch'] in reserved:
                raise Pending('Batch reserved by an unresolved delivery')
            lot = self.state['lots'][row['batch']]
            if (actual['against_sales_order'] != shipment['sales_order']
                    or actual['so_detail'] != shipment['sales_order_item']
                    or actual['item_code'] != lot['item'] or actual['batch_no'] != lot['batch']
                    or actual['qty'] != row['qty'] or actual['warehouse'] != self.e.wh['fg']
                    or actual.get('serial_and_batch_bundle')):
                raise Pending('Delivery draft differs from pinned allocation')
            available, d = self.available(lot, 'WAREHOUSE')
            if d != row['disposition'] or available < row['qty']:
                if (getattr(self.e, 'config', None) or {}).get('company_operating'):
                    shipment.setdefault('allocation_change', {
                        'delivery': draft['name'], 'batch': lot['batch'], 'quantity': row['qty'],
                        'uom': lot['uom'], 'day': self.e.day})
                    self.e.persist()
                raise Pending('Current QMS disposition or lot quantity changed at dispatch')

    def ship(self, key, order):
        e = self.e
        sales = e.get(key, 'WAREHOUSE')
        if sales['docstatus'] != 1 or sales.get('status') in ('Closed', 'On Hold', 'Completed'):
            return
        sales_row, = sales['items']
        if (sales_row.get('item_code', order['item']) != order['item']
                or sales_row.get('conversion_factor', 1) != 1):
            raise Pending('Expected a single sales line in ERP stock UOM')
        need = sales_row['qty'] - sales_row.get('delivered_qty', 0) - self.reserved_delivery(
            sales['name'], sales_row['name'])
        reserved = self.reserved_dispatch_batches()
        rows = []
        awaiting_release = False
        for lot in self.state['lots'].values():
            if need <= 0:
                break
            if (lot['item'] != order['item'] or lot['kind'] != 'finished'
                    or lot['batch'] in reserved):
                continue
            eligibility = self.attempt('allocate-lot:' + lot['batch'], 'WAREHOUSE',
                                       lambda: self.available(lot, 'WAREHOUSE'))
            if eligibility is None:
                continue
            available, disposition = eligibility
            if self.used(lot) < lot['qty'] and (
                    not disposition or disposition['status'] != 'released'
                    or disposition != self.state.get('synced', {}).get(lot['batch'])):
                awaiting_release = True
            take = int(min(need, available))
            if take > 0:
                rows.append({'batch': lot['batch'], 'qty': take, 'disposition': disposition})
                need -= take
        if not rows:
            if awaiting_release:
                raise Pending('Dispatch awaits current QMS release and stock confirmation')
            return
        shipments = self.state['shipments']
        op = 'lot-delivery:' + digest([sales['name'], len(shipments)])
        mapped = e.mapped('WAREHOUSE', 'erpnext.selling.doctype.sales_order.sales_order.make_delivery_note',
                          source_name=sales['name'])
        template, = mapped['items']
        lease_id = digest([self.namespace, op])
        shipment = shipments[op] = {'key': op, 'order': key, 'sales_order': sales['name'],
                                    'sales_order_item': sales_row['name'], 'item': order['item'],
                                    'rows': rows, 'day': e.day,
                                    'lease_id': lease_id}
        e.persist()
        mapped['items'] = [self.batch_row({**template, 'qty': row['qty'], 'warehouse': e.wh['fg']}, row['batch'])
                           for row in rows]
        mapped['custom_norvale_quality_releases'] = json.dumps(self.shipment_releases(shipment), sort_keys=True)
        self.submit_shipment(shipment, mapped)

    def report_allocation_change(self, shipment):
        e = self.e
        change = shipment['allocation_change']
        key = 'allocation-review:' + shipment['key']
        order = e.get(shipment['order'], 'PLANNER')
        draft = e.api['PLANNER'].get('Delivery Note', change['delivery'])
        # Withdrawal applies to the entire draft even if one lot triggered the hold.
        allocations = [dict(batch=row['batch'], quantity=row['qty'],
                            uom=self.state['lots'][row['batch']]['uom']) for row in shipment['rows']]
        if (order['name'] != shipment['sales_order'] or draft.get('name') != change['delivery']
                or draft.get('docstatus') != 0
                or any(r.get('against_sales_order') != order['name']
                       or r.get('so_detail') != shipment['sales_order_item']
                       or r.get('item_code') != shipment['item'] or r.get('qty') != a['quantity']
                       for r, a in self.match_batch_rows(draft['items'], allocations, 'batch'))):
            raise Pending('Planning could not verify the withdrawn delivery allocation')
        if 'notice' not in change:
            lines = [row for row in order['items'] if row['name'] == shipment['sales_order_item']]
            if len(lines) != 1 or any(lines[0].get('stock_uom') != a['uom'] for a in allocations):
                raise Pending('Affected sales line or unit is unavailable to Planning')
            row = lines[0]
            remaining = row['qty'] - row.get('delivered_qty',0)
            due = row.get('delivery_date') or order.get('delivery_date')
            affected = ', '.join(f"{a['quantity']} {a['uom']} from lot {a['batch']}" for a in allocations)
            change['notice'] = (f"Allocation withdrawn on {e.dated(change['day'])} for {order['name']}, "
                f"line {row['name']}. Draft delivery {draft['name']} lost its allocation for "
                f"{shipment['item']}: {affected}. "
                f"Open at review on {e.date()}: {remaining} {row['stock_uom']}. "
                f"Recorded delivery date: {due or 'unspecified'}. Review the customer commitment.")
            e.persist()
        fields = dict(reference_type='Sales Order', reference_name=order['name'], allocated_to='sales@norvale.test',
                      description=change['notice'], date=e.dated(change['day']))
        task = e.write(key,'ToDo',fields,'PLANNER')
        record = e.api['SALES'].get('ToDo',task['name'])
        if any(record.get(k) != v for k,v in fields.items()):
            raise Pending('Sales could not confirm the planning handoff')
        try:
            e.mail(key, 'SALES_OPS_MGR', 'ACCT_EXEC', 'Delivery allocation changed for ' + order['name'],
                   change['notice'] + ' ERP task ' + record['name'] + '.')
        except MailReadbackError as exc:
            raise Pending(f'Sales task {record["name"]} awaits mail reconciliation: {exc}') from exc
        change['sales_task'] = record['name']
        e.persist()
        return True

    def fulfillment(self):
        e = self.e
        self.recover_shipments()
        shipments = self.state['shipments']
        for key, order in e.state['ops']['orders'].items():
            count = len(shipments)
            self.attempt('dispatch:' + key, 'WAREHOUSE', lambda: self.ship(key, order))
            if len(shipments) > count:
                shipment = next(reversed(shipments.values()))
                if not shipment.get('target_id') and not shipment.get('abandoned'):
                    self.reconcile_shipment(shipment)
        for shipment in shipments.values():
            change = shipment.get('allocation_change')
            if change and shipment.get('abandoned') and not change.get('sales_task'):
                self.attempt('sales-impact:' + shipment['key'], 'PLANNER', lambda:self.report_allocation_change(shipment))
            if shipment.get('target_id'):
                self.attempt('invoice:' + shipment['key'], 'FINANCE',
                             lambda: e.invoice_delivery(shipment['key'], shipment['departed_day']))

    def production(self, plan):
        for work in list(self.state['work'].values()):
            self.attempt('mes-dispatch:' + work['key'], 'PRODUCTION', lambda: self.dispatch(work))
        if plan == []:
            return
        for proposal in plan['work']:
            key = proposal['key']
            if key in self.state.get('withdrawn_work', {}):
                continue
            def dispatch_proposal():
                work = self.state['work'].get(key) or self.pin_work(proposal)
                self.dispatch(work)
            self.attempt('mes-dispatch:' + key, 'PRODUCTION', dispatch_proposal)
