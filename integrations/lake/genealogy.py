"""Reconstruct a shipped lot's production and disposition from observable native records."""
from collections import defaultdict
from functools import cached_property
from decimal import Decimal
from datetime import date
import json

from boundary import reference_index


def observations_disagree(first, second):
    """Compare shared observable fields across projections of the same native version."""
    if isinstance(first, dict) and isinstance(second, dict):
        return any(observations_disagree(first[k], second[k]) for k in first.keys() & second.keys())
    if isinstance(first, list) and isinstance(second, list):
        return len(first) != len(second) or any(observations_disagree(a, b) for a, b in zip(first, second))
    return first != second


def verified_output(report, source, posting, batch, quantity_posted):
    output = report.get('output', {})
    lot_id = output.get('lot_id')
    return bool(lot_id and posting.get('work_order')
                and report.get('erp_work_ref') == posting['work_order']
                and posting['completion'].get('completion_id') in (report.get('completion_ref'), report.get('name'))
                and posting['completion'].get('mes_source', source) == source
                and (posting['completion'].get('output_lot_id') or posting.get('output_lot_id')) == f'{source}:stock.lot:{lot_id}'
                and output.get('item_code') == batch['item']
                and output.get('quantity') == quantity_posted == posting.get('fg_completed_qty')
                and quantity_posted >= batch['quantity'])


def publication_matches(publication, work, report, posting, item, *, erp_bom=None):
    """Require both native receivers and business-date evidence in one observation."""
    pin = work.get('definition') or {}
    acknowledgments = publication.get('acknowledgments') or {}
    erp_ack, mes_ack = acknowledgments.get('erp') or {}, acknowledgments.get('mes') or {}
    markers = {system: ack['receiver_marker'] for system, ack in acknowledgments.items()
               if 'receiver_marker' in ack}
    if pin.get('receiver_markers', {}) != markers:
        return False
    if markers:
        from integrations.revisions import valid_receiver_marker
        observed = {'erp': (erp_bom or {}).get('receiver_marker'), 'mes': report.get('receiver_marker')}
        if any(not valid_receiver_marker(system, marker) or observed.get(system) != marker
               for system, marker in markers.items()):
            return False
    release = pin.get('erp_released_on') or (work.get('creation') or '')[:10]
    try:
        released = date.fromisoformat(release)
        dates = [publication.get('approved_on'), publication.get('effective_on'),
                 (publication.get('approval') or {}).get('on'), erp_ack.get('on'), mes_ack.get('on')]
        if any(date.fromisoformat(d) > released for d in dates):
            return False
    except (TypeError, ValueError):
        return False
    return bool(work.get('docstatus') == 1 and item and work.get('production_item') == item
        and pin.get('item') == item and publication.get('product') == item
        and work.get('bom_no') and work['bom_no'] == pin.get('erp_bom') == erp_ack.get('native_id')
        and report.get('bom_id') and str(report['bom_id']) == str(mes_ack.get('native_id'))
        and report.get('erp_work_ref') == work.get('name')
        and report.get('output', {}).get('item_code') == item
        and (not work.get('company') or work['company'] == posting.get('company'))
        and publication.get('effective_on') == pin.get('effective_from')
        and pin.get('approval_id') and (publication.get('approval') or {}).get('evidence') == pin.get('approval_id')
        and (publication.get('approval') or {}).get('sha256') == pin.get('version')
        and all(str(a.get('plm_native_id')) == str(pin.get('id'))
                and a.get('plm_source_version') == pin.get('version') for a in (erp_ack, mes_ack)))


def shipment(lake, erp, delivery_id):
    return ShipmentHistory(lake._rows(), erp).trace(delivery_id)


class ShipmentHistory:
    """Resolve multiple deliveries against one immutable archive read."""
    def __init__(self, rows, erp):
        history, latest = {}, {}
        self.inspections = defaultdict(list)
        for row in sorted(rows, key=lambda r: r['ingested_at']):
            payload = json.loads(row['payload'])
            if row['entity'] == 'Quality Inspection' and payload.get('modified'):
                self.inspections[row['source'], row['native_id'], payload['modified']].append(payload)
            history.setdefault(row['source'], {}).setdefault(row['entity'], []).append(payload)
            latest[row['source'], row['entity'], row['native_id']] = payload
        self.erp, self.history, self.latest = erp, history, latest
        self.index, _ = reference_index(history)
        self.bundles = {k[2]: v for k, v in latest.items() if k[:2] == (erp, 'Serial and Batch Bundle')}
        self.ledger = defaultdict(list)
        for key, doc in latest.items():
            if key[:2] == (erp, 'Stock Ledger Entry') and doc.get('voucher_type') == 'Delivery Note':
                self.ledger[doc.get('voucher_no'), doc.get('voucher_detail_no')].append(doc)
        self.receipt_ledger = defaultdict(list)
        for (site, entity, _), entry in latest.items():
            if (site == erp and entity == 'Stock Ledger Entry' and entry.get('voucher_type') == 'Purchase Receipt'
                    and not entry.get('is_cancelled')):
                self.receipt_ledger[entry.get('voucher_no'), entry.get('voucher_detail_no')].append(entry)
        self.leases = defaultdict(list)
        for key, doc in latest.items():
            if key[1] == 'Norvale Dispatch Lease':
                self.leases[key[2]].append((key, doc))
        produced = defaultdict(list)
        for key, doc in latest.items():
            if key[:2] != (erp,'Stock Entry') or doc.get('docstatus') != 1 or doc.get('stock_entry_type') not in ('Manufacture', 'Repack'):
                continue
            for row in doc.get('items', []):
                if row.get('is_finished_item') or (doc.get('stock_entry_type') == 'Repack' and row.get('t_warehouse')):
                    for batch, _ in self.movements(doc,row,'Inward','Stock Entry'):
                        produced[row['item_code'],batch].append(doc)
        self.produced = produced
        from integrations.lake.shipment_billing import billing_links
        self.billing = billing_links(latest, erp)

    def matches(self, kind, source, *identity):
        scope = (source,) if source is not None else ()
        return self.index.get((kind, *scope, *identity), set()) if None not in identity else set()

    def transfer(self, transfer):
        """Return a transfer only with contemporaneous authorization evidence."""
        traced = self.transfer_lineage(transfer)
        if traced and traced['authorization_at_transfer'] == 'confirmed':
            return traced
        return None

    def transfer_lineage(self, transfer):
        """Resolve a posted release transfer and both stock legs from this archive."""
        from integrations.lake.boundary import release_transfer_reference
        from integrations.company_run.consumption import amount, bundle_entry
        if not transfer:
            return None
        source = self.erp
        doc = self.latest.get((source, 'Stock Entry', transfer), {})
        pin = doc.get('release_disposition')
        if not pin:
            return None
        ref, valid = release_transfer_reference(doc)
        matches = self.index.get(ref, set())
        if not valid or len(matches) != 1:
            return None
        site, _, inspection = next(iter(matches))
        assay_scopes = set()
        observations = []
        for observation in self.history[site].get('Quality Inspection', []):
            if observation['name'] != inspection or observation.get('modified') != pin['version']:
                continue
            assay_scopes.add((observation.get('report_date'), observation.get('custom_spec_revision')))
            observations.append(observation)
            if observation.get('report_date'):
                try:
                    if date.fromisoformat(observation['report_date']) > date.fromisoformat(doc['posting_date']):
                        return None
                except (KeyError, ValueError, TypeError):
                    return None
        row, = doc['items']
        moves = [d for (site, entity, _), d in self.latest.items() if site == source
                 and entity == 'Stock Ledger Entry' and d.get('voucher_type') == 'Stock Entry'
                 and d.get('voucher_no') == transfer and not d.get('is_cancelled')]
        if len(moves) != 2:
            return None
        legs = {}
        try:
            qty = amount(row.get('transfer_qty'))
            if qty != amount(row.get('qty')) or amount(row.get('conversion_factor', 1)) != 1:
                return None
            for warehouse, direction, sign in ((row['s_warehouse'], 'Outward', -1), (row['t_warehouse'], 'Inward', 1)):
                move, = (d for d in moves if d.get('warehouse') == warehouse)
                identifier = move.get('serial_and_batch_bundle')
                entry = bundle_entry(doc, dict(row, serial_and_batch_bundle=identifier), self.bundles,
                                     direction, warehouse)
                if (move.get('docstatus') != 1 or move.get('is_cancelled') != 0
                        or move.get('voucher_detail_no') != row['name'] or move.get('item_code') != pin['item']
                        or not doc.get('company') or move.get('company') != doc['company']
                        or not doc.get('posting_date') or move.get('posting_date') != doc['posting_date']
                        or move.get('stock_uom') != pin['uom']
                        or amount(move['actual_qty']) != sign * amount(row['transfer_qty'])
                        or amount(entry['qty']) != amount(move['actual_qty'])):
                    return None
                legs[direction] = {'ledger': move['name'], 'bundle': identifier}
        except (ValueError, KeyError, TypeError, ArithmeticError):
            return None
        traced = dict(transfer=[source, 'Stock Entry', transfer], release=[site, 'Quality Inspection', inspection, pin['version']],
                      batch=pin['batch'], item=pin['item'], uom=pin['uom'], qty=row['transfer_qty'],
                      source=row['s_warehouse'], target=row['t_warehouse'], disposition=pin, legs=legs,
                      authorization_at_transfer='unresolved')
        receipt_id = doc.get('qms_transfer_lease')
        receipt_key = (site, 'Norvale Dispatch Lease', receipt_id)
        receipt = self.latest.get(receipt_key, {})
        binding = dict(erp_source=source, stock_entry=transfer, row=row['name'], company=doc['company'],
                       posting_date=doc['posting_date'],
                       source=row['s_warehouse'], target=row['t_warehouse'])
        scopes = receipt.get('scopes', [])
        scope = scopes[0] if len(scopes) == 1 else {}
        dated_scope = (len(assay_scopes) == 1 and all(next(iter(assay_scopes)))
                       and next(iter(assay_scopes))[1] == scope.get('revision'))
        original = pin.get('qms_batch', pin['batch'])
        rework_ok = (original == pin['batch'] and
                     not any(scope.get(k) for k in ('output_batch', 'retest_of', 'rework_execution', 'report_date')) and
                     not any(s.get('custom_rework_execution') or s.get('custom_rework_output_batch') for s in observations))
        if original != pin['batch'] and len(observations) == 1:
            sample = observations[0]
            posts = self.produced.get((pin['item'], pin['batch']), [])
            if len(posts) == 1 and posts[0].get('stock_entry_type') == 'Repack':
                from rework_reader import resolve
                candidate = dict(batch=pin['batch'], item=pin['item'], unit=pin['uom'], qms_source=site,
                                 quality_release=[site, 'Quality Inspection', inspection, pin['version']])
                errors = []
                resolve(candidate, posts[0], source, self.history, self.latest, self.movements, errors, disputed=self.disputed_stock_entries)
                rework_ok = (not errors and bool(candidate.get('rework')) and
                    sample.get('custom_retest_of') and sample.get('custom_rework_execution') and
                    all(scope.get(k) == v for k, v in {
                        'output_batch': pin['batch'], 'retest_of': sample.get('custom_retest_of'),
                        'rework_execution': sample.get('custom_rework_execution'),
                        'report_date': sample.get('report_date')}.items()) and
                    all(pin.get(k) == v for k, v in {
                        'retest_of': sample.get('custom_retest_of'),
                        'rework_execution': sample.get('custom_rework_execution'),
                        'report_date': sample.get('report_date'),
                        'spec_revision': sample.get('custom_spec_revision')}.items()))
        if (dated_scope and rework_ok and receipt_id and pin.get('qms_source') == site and receipt.get('name') == receipt_id
                and receipt.get('kind') == 'release_transfer' and receipt.get('lease_state') == 'submitted'
                and not receipt.get('delivery') and receipt.get('transfer') == binding
                and receipt.get('commit') == dict(transfer=binding, quantity=row['transfer_qty'], legs=legs)
                and all(scope.get(field) == expected for field, expected in {
                    'inspection': inspection, 'version': pin['version'], 'batch': original,
                    'item': pin['item'], 'uom': pin['uom'], 'qty': pin['qty'], 'approver': pin['approver'],
                    'dispatch_qty': row['transfer_qty'],
                }.items())):
            traced.update(authorization_at_transfer='confirmed', authorization_receipt=list(receipt_key))
        return traced

    @cached_property
    def disputed_stock_entries(self):
        disputed = set()
        for entity in ('Stock Entry', 'Serial and Batch Bundle', 'Stock Ledger Entry'):
            versions = defaultdict(list)
            for doc in self.history.get(self.erp, {}).get(entity, []):
                name = doc['name']
                if (doc.get('modified') == self.latest[self.erp, entity, name].get('modified')
                        and doc not in versions[name]):
                    versions[name].append(doc)
            for name, docs in versions.items():
                if any(observations_disagree(a, b) for i, a in enumerate(docs) for b in docs[i+1:]):
                    if entity == 'Stock Entry':
                        disputed.add(name)
                    else:
                        disputed.update(d['voucher_no'] for d in docs
                                        if d.get('voucher_type') == 'Stock Entry' and d.get('voucher_no'))
        return disputed

    @cached_property
    def supplier_billing(self):
        from integrations.lake.shipment_billing import billing_links
        return billing_links(self.latest, self.erp, 'Purchase Invoice')

    def purchase_receipt(self, name):
        from integrations.company_run.consumption import amount
        ref = (self.erp, 'Purchase Receipt', name)
        doc = self.latest.get(ref, {})
        if doc.get('docstatus') != 1 or doc.get('is_return') or not doc.get('items'):
            raise ValueError('Expected a submitted incoming Purchase Receipt')
        result = dict(receipt=list(ref), supplier=doc.get('supplier'), lots=[], unresolved=[],
                      invoice_lines=self.supplier_billing.get(name, []))
        for row in doc['items']:
            order = self.latest.get((self.erp, 'Purchase Order', row.get('purchase_order')), {})
            linked = [item for item in order.get('items', []) if item['name'] == row.get('purchase_order_item')]
            if (order.get('docstatus') != 1 or len(linked) != 1
                    or any(not doc.get(k) or order.get(k) != doc[k] for k in ('supplier','company','currency'))
                    or any(not row.get(k) or linked[0].get(k) != row[k] for k in ('item_code','stock_uom'))):
                result['unresolved'].append([row.get('name'), 'purchase order line'])
                continue
            bundle = self.bundles.get(row.get('serial_and_batch_bundle'), {})
            ledger = self.receipt_ledger[name, row['name']]
            try:
                move, = ledger
                lots = self.movements(doc, row, 'Inward', 'Purchase Receipt')
                declared = row.get('batch_no')
                valid = (lots and len(lots) == len(bundle['entries'])
                         and (not declared or all(batch == declared for batch, _ in lots))
                         and row.get('warehouse') and bundle.get('warehouse') == row['warehouse']
                         and all(e.get('warehouse') == row['warehouse'] for e in bundle['entries'])
                         and move.get('docstatus') == 1 and move.get('is_cancelled') == bundle.get('is_cancelled') == 0
                         and all(move.get(k) == row[k] for k in ('item_code','warehouse','stock_uom','serial_and_batch_bundle'))
                         and move.get('company') == doc['company'] and doc.get('posting_date') == move['posting_date']
                         and sum(amount(qty) for _, qty in lots) == amount(move['actual_qty']) == amount(row['stock_qty']))
            except (ValueError, KeyError, ArithmeticError, TypeError):
                valid = False
            if not valid:
                result['unresolved'].append([row['name'], 'purchase receipt stock movement'])
                continue
            for batch, qty in lots:
                result['lots'].append(dict(batch=batch, quantity=qty, item=row['item_code'], unit=row['stock_uom'],
                    receipt_row=row['name'], purchase_order=[self.erp,'Purchase Order',order['name']],
                    order_row=linked[0]['name'], ledger=move['name'], bundle=bundle['name'], warehouse=row['warehouse']))
        return result

    def movements(self, doc, row, direction, entity):
        bundle = self.bundles.get(row.get('serial_and_batch_bundle'))
        if not bundle or (bundle.get('docstatus'), bundle.get('is_cancelled',0), bundle.get('voucher_type'),
                          bundle.get('voucher_no'),bundle.get('voucher_detail_no'),bundle.get('item_code'),
                          bundle.get('type_of_transaction')) != (1,0,entity,doc['name'],row['name'],
                                                                 row['item_code'],direction):
            return []
        return [(entry['batch_no'],entry['qty']) for entry in bundle['entries'] if entry.get('batch_no')
                and (Decimal(str(entry['qty'])) > 0 if direction == 'Inward' else Decimal(str(entry['qty'])) < 0)]

    def trace(self, delivery_id):
        erp, history, latest = self.erp, self.history, self.latest
        movements, produced = self.movements, self.produced
        note = latest[erp, 'Delivery Note', delivery_id]
        if note.get('docstatus') != 1 or note.get('is_return'):
            raise ValueError('Expected a submitted outbound Delivery Note')
        result = {'shipment':[erp,'Delivery Note',delivery_id], 'customer':note.get('customer'), 'lots':[], 'unresolved':[]}
        result['invoice_lines'] = self.billing.get(delivery_id, [])
        result['sales_order_lines'] = []
        result['unresolved_order_lines'] = []
        for row in note['items']:
            order_ref = (erp, 'Sales Order', row.get('against_sales_order'))
            order = latest.get(order_ref, {})
            order_row = row.get('so_detail')
            linked = [item for item in order.get('items', []) if order_row and item.get('name') == order_row]
            if (order.get('docstatus') == 1 and len(linked) == 1
                    and all(note.get(field) and note[field] == order.get(field) for field in ('company', 'customer'))
                    and all(row.get(field) and row[field] == linked[0].get(field) for field in ('item_code', 'stock_uom'))
                    and order.get('transaction_date') and note.get('posting_date')
                    and order['transaction_date'] <= note['posting_date']):
                result['sales_order_lines'].append(dict(delivery_row=row['name'], order=list(order_ref),
                                                       order_row=order_row, item=row['item_code']))
            else:
                result['unresolved_order_lines'].append(row['name'])
            observed = movements(note,row,'Outward','Delivery Note')
            if not observed:
                result['unresolved'].append([row['name'],'outbound batch bundle'])
            else:
                stock_qty = row.get('stock_qty')
                if stock_qty is None and row.get('stock_uom') and row.get('uom') == row['stock_uom']:
                    stock_qty = row.get('qty')
                if stock_qty is None or Decimal(str(stock_qty)) != sum(-Decimal(str(q)) for _, q in observed):
                    result['unresolved'].append([row['name'], 'delivery line stock quantity'])
            ledger = self.ledger.get((delivery_id, row['name']), [])
            bundle = self.bundles.get(row.get('serial_and_batch_bundle'), {})
            entries = bundle.get('entries', [])
            try:
                total = sum((Decimal(str(e['qty'])) for e in entries), Decimal(0))
                valid = (len(ledger) == 1 and observed and len(observed) == len(entries)
                         and row.get('warehouse') and row.get('stock_uom')
                         and bundle.get('is_cancelled') == 0
                         and bundle.get('warehouse') == row['warehouse']
                         and all(e.get('warehouse') == row['warehouse'] for e in entries)
                         and ledger[0].get('docstatus') == 1 and ledger[0].get('is_cancelled') == 0
                         and note.get('company') and ledger[0].get('company') == note['company']
                         and note.get('posting_date') and ledger[0].get('posting_date') == note['posting_date']
                         and ledger[0].get('item_code') == row.get('item_code')
                         and ledger[0].get('warehouse') == row['warehouse']
                         and ledger[0].get('stock_uom') == row['stock_uom']
                         and ledger[0].get('serial_and_batch_bundle') == row.get('serial_and_batch_bundle')
                         and Decimal(str(ledger[0]['actual_qty'])) == total and total < 0)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                valid = False
            if valid:
                result.setdefault('stock_ledger', []).append(dict(row=row['name'], entry=ledger[0]['name'],
                    warehouse=row['warehouse'], quantity=-ledger[0]['actual_qty'], unit=row['stock_uom'],
                    bundle=row['serial_and_batch_bundle']))
            else:
                result['unresolved'].append([row['name'], 'stock ledger issue'])
            for batch, qty in observed:
                lot = dict(batch=batch,item=row['item_code'],delivery_row=row['name'],quantity=-qty,
                           unit=row.get('stock_uom'), bundle=row['serial_and_batch_bundle'])
                result['lots'].append(lot)
                releases = [r for r in note.get('quality_releases', []) if r.get('batch') == batch
                            and r.get('disposition',{}).get('item') == row['item_code']]
                targets = set()
                if len(releases) == 1:
                    cited = releases[0]['disposition']
                    authorized = cited.get('status') == 'released' and cited.get('batch') == batch
                    if authorized:
                        targets = self.matches('release', cited.get('qms_source'), cited.get('id'),
                                               cited.get('version'), batch, row['item_code'])
                    lease_id = releases[0].get('qms_lease')
                    if not lease_id and len(targets) == 1:
                        site, _, name = next(iter(targets))
                        snapshots = self.inspections[site, name, cited['version']]
                        if not snapshots or not all(
                            doc.get('docstatus') == 1 and doc.get('workflow_state') == 'Released'
                            and doc.get('status') == 'Accepted' and doc.get('item_code') == lot['item']
                            and (doc.get('custom_rework_output_batch') or doc.get('batch_no')) == batch
                            and doc.get('batch_no') == cited.get('qms_batch', doc.get('batch_no'))
                            and lot['unit'] and doc.get('custom_lot_uom') == lot['unit']
                            and doc.get('custom_lot_quantity') == snapshots[0].get('custom_lot_quantity')
                            and type(doc.get('custom_lot_quantity')) in (int, float)
                            and 0 < -qty <= doc['custom_lot_quantity'] < float('inf')
                            and cited.get('qty', doc['custom_lot_quantity']) == doc['custom_lot_quantity']
                            for doc in snapshots
                        ):
                            targets = set()
                    if lease_id:
                        targets = set()
                        receipts = [key for key, receipt in self.leases.get(lease_id, [])
                                    if authorized
                                    and (not cited.get('qms_source') or key[0] == cited['qms_source'])
                                    and receipt.get('kind', 'dispatch') == 'dispatch'
                                    and receipt.get('lease_state') == 'submitted' and receipt.get('delivery') == delivery_id
                                    and any(s['batch'] == cited.get('qms_batch', batch) and s['item'] == row['item_code']
                                            and s['inspection'] == cited['id'] and s['version'] == cited['version']
                                            and s['uom'] == lot['unit'] and s['dispatch_qty'] == -qty
                                            and type(s.get('qty')) in (int,float) and s['qty'] == cited.get('qty')
                                            and 0 < -qty <= s['qty']
                                            for s in receipt['scopes'])]
                        if len(receipts) == 1:
                            receipt, = receipts
                            lot['dispatch_lease'] = list(receipt)
                            inspection = (receipt[0], 'Quality Inspection', cited['id'])
                            if self.matches('inspection_scope', receipt[0], cited['id'], row['item_code'],
                                            cited.get('qms_batch', batch), batch):
                                # The QMS checked this exact inspection version while acquiring the lease.
                                targets = {inspection}
                        else:
                            result['unresolved'].append([batch,'submitted QMS dispatch lease'])
                if len(targets) == 1 and len(releases)==1 and releases[0].get('qty') == -qty:
                    lot['quality_release'] = [*next(iter(targets)),releases[0]['disposition']['version']]
                else:
                    result['unresolved'].append([batch,'shipment release version'])
                postings = produced[row['item_code'],batch]
                if len(postings)!=1:
                    result['unresolved'].append([batch,'manufacture posting'])
                    continue
                doc, = postings
                if doc.get('stock_entry_type') == 'Repack':
                    from rework_reader import resolve
                    resolve(lot, doc, erp, history, latest, movements, result['unresolved'], disputed=self.disputed_stock_entries)
                    if 'rework' not in lot:
                        continue
                else:
                    lot['stock_entry'] = doc['name']
                    lot['work_order'] = doc.get('work_order')
                    completion = doc.get('completion', {})
                    mes = self.matches('completion', completion.get('mes_source'),
                                       completion.get('completion_id'), completion.get('version'))
                    if len(mes)==1:
                        native, = mes
                        version = doc['completion']['version']
                        candidates = [r for r in history[native[0]][native[1]]
                                      if r['name'] == native[2] and r['modified'] == version]
                        quantities = [amount for line in doc['items'] if line.get('is_finished_item')
                                      and line['item_code'] == lot['item']
                                      for name, amount in movements(doc, line, 'Inward', 'Stock Entry') if name == batch]
                        # Native timestamps can have coarse precision. Contradictory observations
                        # of one completion version cannot establish the input genealogy.
                        if (len(quantities) != 1 or not candidates
                                or not all(verified_output(r, native[0], doc, lot, quantities[0])
                                           and r['inputs'] == candidates[0]['inputs'] for r in candidates)):
                            result['unresolved'].append([batch,'MES output lot, work and production quantity'])
                            continue
                        report = candidates[-1]
                        lot.update(mes=list(native), mes_version=version, inputs=report['inputs'], output=report['output'])
                        for pin in doc.get('intermediate_inputs', []):
                            ref = pin['mes_move']
                            source = native[0]
                            consumed = [m for m in report['inputs'] if str(ref.get('native_id')) == m['line_id']
                                        and f"{source}:stock.lot:{m['lot_id']}" == pin['mes_lot_id']]
                            upstream = latest.get((erp,'Stock Entry',pin['erp_manufacture']), {})
                            bulk_ref = upstream.get('completion', {})
                            outputs = [m for m in history.get(source, {}).get('mrp.production', [])
                                       if m['completion_ref'] == bulk_ref.get('completion_id')
                                       and m['modified'] == bulk_ref.get('version')]
                            quality = [(site,d) for site, tables in history.items()
                                       if not pin.get('qms_source') or site == pin['qms_source']
                                       for d in tables.get('Quality Inspection', [])
                                       if d.get('name') == pin['qms_release'] and d.get('modified') == pin.get('qms_version')
                                       and d.get('batch_no') == pin['batch'] and d.get('workflow_state') == 'Released']
                            if (len(consumed) != 1 or len(outputs) != 1 or len(quality) != 1
                                    or not any(pin['batch'] == b for r in upstream.get('items', []) if r.get('is_finished_item')
                                               for b, _ in movements(upstream,r,'Inward','Stock Entry'))
                                    or not any(pin['batch'] == b and Decimal(str(-quantity)) == Decimal(str(consumed[0]['quantity']))
                                               for r in doc['items'] if r.get('s_warehouse')
                                               for b, quantity in movements(doc,r,'Outward','Stock Entry'))
                                    or outputs[0]['output']['lot_id'] != consumed[0]['lot_id']
                                    or outputs[0]['output']['unit_id'] != consumed[0]['unit_id']
                                    or quality[0][1].get('item_code') != consumed[0]['item_code']):
                                result['unresolved'].append([pin['batch'],'bulk manufacture, release and packing consumption'])
                                continue
                            lot.setdefault('bulk_inputs', []).append(dict(batch=pin['batch'], stock_entry=upstream['name'],
                                quantity=consumed[0]['quantity'], unit=consumed[0]['unit'],
                                mes=[source,'mrp.production',outputs[0]['name'],outputs[0]['modified']],
                                inputs=outputs[0]['inputs'], consuming_move=consumed[0]['line_id'],
                                release=[quality[0][0],'Quality Inspection',pin['qms_release'],pin['qms_version']]))
                    else:
                        result['unresolved'].append([batch,'MES completion'])
                work = latest.get((erp, 'Work Order', lot['work_order']), {})
                pin = work.get('definition') or {}
                plm = self.matches('publication', pin.get('plm_source'), str(pin.get('id')), pin.get('version'))
                if 'rework' in lot:
                    mes_ref = lot['rework']['original_mes']
                    posting = latest.get((erp, 'Stock Entry', lot['rework']['original_stock_entry']), {})
                else:
                    mes_ref = [*lot.get('mes', []), lot.get('mes_version')]
                    posting = doc
                reports = [r for r in history.get(mes_ref[0], {}).get('mrp.production', [])
                           if len(mes_ref) == 4 and r.get('name') == mes_ref[2]
                           and r.get('modified') == mes_ref[3]] if mes_ref and mes_ref[0] else []
                verified = set()
                erp_bom = latest.get((erp, 'BOM', work.get('bom_no')))
                for target in plm:
                    complete = [p for p in history[target[0]][target[1]]
                                if p.get('name') == target[2] and p.get('version') == pin.get('version')
                                and (p.get('acknowledgments') or {}).get('erp')
                                and (p.get('acknowledgments') or {}).get('mes')]
                    if reports and complete and all(publication_matches(p, work, r, posting, lot['item'], erp_bom=erp_bom)
                                                    for r in reports for p in complete):
                        verified.add(target)
                if len(verified) == 1:
                    lot['publication'] = list(next(iter(verified)))
                    lot['publication_version'] = pin['version']
                    if (erp_bom and erp_bom.get('docstatus') == 1 and erp_bom.get('item') == lot['item']
                            and erp_bom.get('company') and erp_bom['company'] == work.get('company')
                            and erp_bom.get('owner') and erp_bom.get('modified')):
                        lot['erp_bom'] = dict(record=[erp, 'BOM', work['bom_no'], erp_bom['modified']],
                                              owner=erp_bom['owner'])
                    if pin.get('receiver_markers'):
                        lot['publication_receiver_markers'] = pin['receiver_markers']
                else:
                    result['unresolved'].append([batch,'PLM revision'])
        return result
