"""Recurring handoff of a held MES lot through QMS, MES, ERP and back to QMS."""

import json
import math
from xmlrpc.client import Fault

from integrations.qms.adapter import APIError, STATE
from .company_erp import Pending, digest, ids


def require(condition, message):
    if not condition:
        raise Pending(message)


class Rework:
    def __init__(self, mes, quality, uom_ids, *, qms_source=None):
        self.mes = mes
        self.quality = quality
        self.uom_ids = uom_ids
        self.qms_source = qms_source

    def poll(self, flow, requests):
        operations = flow.state.setdefault('rework', {})
        for request in requests:
            require(ids(request, 'batch', 'qms_batch', 'inspection')
                    and type(request.get('mes_order')) is int and request['mes_order'] > 0,
                    'Rework request needs the ERP batch, MES order and native QMS failure')
            key = 'rework:' + digest(request['batch'])
            operation = operations.setdefault(key, {'request': request})
            require(operation['request'] == request, 'Rework request changed for this lot')
            flow.e.persist()
        for key, operation in operations.items():
            if not operation.get('retest'):
                flow.attempt(key, 'PRODUCTION', lambda: self.reconcile(flow, key, operation))

    def reconcile(self, flow, key, operation):
        try:
            self.advance(flow, key, operation)
        except APIError as exc:
            raise Pending(str(exc)) from None
        except (Fault, RuntimeError):
            raise Pending('Native rework execution or readback could not be confirmed') from None

    def advance(self, flow, key, operation):
        e = flow.e
        request = operation['request']
        lot = flow.state['lots'].get(request['batch'])
        require(lot and lot['kind'] == 'finished' and not lot.get('rework_key')
                and lot.get('revision', {}).get('inspection_spec'),
                'Rework needs an original ERP lot posted from this MES')
        source_id = request['mes_order']
        try:
            source = self.mes.completion(source_id)
        except ValueError as exc:
            raise Pending('Original MES order has no completed output') from exc
        require(lot.get('completion_version') and lot['completion_version'] == source.get('recorded_at_utc'),
                'Original MES completion version differs from the held ERP lot or its pin is missing')
        original = source['output']
        native_lot = f"{self.mes.source_id}:stock.lot:{original['lot']['native_id']}"
        require(lot.get('completion_id') == source['completion_ref']
                and lot.get('mes_lot_id') == native_lot
                and original['item_code'] == lot['item'] and original['quantity'] == lot['qty']
                and original['uom']['native_id'] == self.uom_ids.get(lot['uom']),
                'MES output differs from the held ERP lot')

        def current_authority():
            return self.quality.authorization(request['qms_batch'], request['inspection'], lot['item'],
                                              lot['revision']['inspection_spec'], lot['qty'], lot['uom'],
                                              lot['batch'], e.date())

        native = self.mes.rework_record(source_id)
        if native is None:
            require(not operation.get('receipt'), 'MES execution missing after ERP receipt')
            flow.reconcile_moves(lot)
            require(not any(row['batch'] == lot['batch'] for shipment in flow.state['shipments'].values()
                            if not shipment.get('target_id') and not shipment.get('abandoned')
                            for row in shipment['rows']),
                    'Unresolved ERP shipment references the failed lot')
            require(flow.balance(lot, e.wh['hold'], 'PRODUCTION') == lot['qty']
                    and flow.balance(lot, e.wh['fg'], 'PRODUCTION') == 0
                    and not flow.used(lot),
                    'Full failed lot must be in ERP hold before rework')
            authority = current_authority()
            require(authority, 'No current QMS Rework Authorized decision for this scope')
            operation['authority'] = authority
            e.persist()
            marker = self.marker(request, authority)
            require(current_authority() == authority, 'QMS authority changed before MES execution')
            self.mes.execute_rework(source_id, marker, lot['qty'], authority['quantity'])
            native = self.mes.rework_record(source_id)
        authority = operation.get('authority')
        require(authority and native and native['norvale_rework_authority'] == self.marker(request, authority),
                'Native rework does not match checkpointed QMS authority')
        report = self.mes.completion(native['id'])
        out = self.verify_execution(source, native, report, authority['quantity'])
        batch = flow.batch(['MES', self.mes.source_id, str(out['lot']['native_id'])], lot['item'])
        require(batch != lot['batch'], 'Rework requires a distinct output batch')
        receipt = self.post(flow, key, lot, batch, out['quantity'], report, authority,
                            native['norvale_rework_scrap'])
        operation.update(receipt=receipt['name'], output_batch=batch, execution=report['completion_ref'],
                         work_order=lot.get('work_order'), scrapped=round(lot['qty'] - out['quantity'], 9))
        flow.register(batch, lot['item'], out['quantity'], lot['uom'], 'finished', receipt,
                      completion_id=report['completion_ref'], completion_version=report['recorded_at_utc'],
                      mes_lot_id=f"{self.mes.source_id}:stock.lot:{out['lot']['native_id']}",
                      revision=lot['revision'], work_order=lot.get('work_order'),
                      source_row=next(row['name'] for row in receipt['items'] if row['batch_no'] == batch),
                      rework_key=key)
        operation.setdefault('retest_day', e.date())
        e.persist()
        retest = self.quality.request_retest(key, authority, operation['execution'], batch,
                                             flow.state['lots'][batch], operation['retest_day'])
        operation['retest'] = retest['name']
        e.persist()
        e.log('physical_rework', failed_lot=lot['batch'], output_lot=batch, inspection=authority['inspection'],
              execution=operation['execution'], receipt=receipt['name'], retest=retest['name'])

    @staticmethod
    def marker(request, authority):
        return f"QMS:{authority['inspection']}:{digest([request, authority])}"

    def verify_execution(self, source, native, report, salvage):
        original, out = source['output'], report['output']
        rows = report['inputs']
        require(len(rows) == 1 and rows[0]['lot'] == original['lot'] and rows[0]['quantity'] == salvage
                and out['quantity'] == salvage and out['lot'] != original['lot']
                and all(row['uom'] == original['uom'] and row['product'] == original['product']
                        for row in [rows[0], out])
                and report['output_hold_location'] == source['output_hold_location']
                and report['revision'] == source['revision'] and not report['backorder'],
                'Native rework movements or hold differ from the authorized execution')
        hold = source['output_hold_location']['native_id']
        raw = self.mes.read('stock.move.line', rows[0]['record']['native_id'])
        require(raw['location_id'][0] == hold, 'MES rework did not consume from hold')
        loss = round(original['quantity'] - salvage, 9)
        scrap_id = native['norvale_rework_scrap']
        require(bool(scrap_id) == bool(loss), 'Native scrap differs from rework loss')
        if scrap_id:
            scrap = self.mes.read('stock.scrap', scrap_id[0])
            lines = self.mes._lines(scrap['move_ids'])
            require(scrap['state'] == 'done' and scrap['production_id'][0] == native['id']
                    and scrap['location_id'][0] == hold
                    and len(lines) == 1 and lines[0]['state'] == 'done'
                    and lines[0]['lot_id'][0] == original['lot']['native_id']
                    and lines[0]['product_id'][0] == original['product']['native_id']
                    and lines[0]['product_uom_id'][0] == original['uom']['native_id']
                    and lines[0]['location_id'][0] == hold
                    and lines[0]['location_dest_id'] == scrap['scrap_location_id']
                    and math.isclose(lines[0]['quantity'], loss, rel_tol=0, abs_tol=1e-9),
                    'Native scrap ledger differs from failed lot and quantity')
        return out

    def post(self, flow, key, lot, batch, salvage, report, authority, scrap):
        e, role = flow.e, 'PRODUCTION'
        rows = [
            flow.batch_row({'item_code': lot['item'], 'qty': lot['qty'], 's_warehouse': e.wh['hold']},
                           lot['batch']),
            flow.batch_row({'item_code': lot['item'], 'qty': salvage, 't_warehouse': e.wh['hold']}, batch),
        ]
        remarks = json.dumps({'rework': report['completion_ref'], 'rework_version': report['recorded_at_utc'],
                              'qms_authority': authority,
                              **({'qms_source': self.qms_source} if self.qms_source else {}),
                              'scrap_id': scrap[0] if scrap else None,
                              'scrapped': round(lot['qty'] - salvage, 9), 'uom': lot['uom']}, sort_keys=True)

        def check(doc):
            expected = json.loads(remarks)
            saved = json.loads(doc.get('remarks') or '{}')
            if doc['docstatus'] == 1:
                # Submitted receipts predate these pins; drafts must carry all current provenance.
                for field in ('rework_version', 'qms_source'):
                    if field not in saved:
                        expected.pop(field, None)
            require(doc['purpose'] == 'Repack' and saved == expected,
                    'ERP rework document purpose or provenance changed')
            matched = flow.match_batch_rows(doc['items'], rows, 'batch_no')
            for actual, expected in matched:
                require(all((actual.get(k) or None) == (expected.get(k) or None)
                            for k in ('item_code', 'qty', 'batch_no', 's_warehouse', 't_warehouse'))
                        and actual['stock_uom'] == lot['uom'] and actual['transfer_qty'] == expected['qty'],
                        'ERP rework document differs from conserved stock quantities')
            if doc['docstatus'] == 0:
                require(flow.balance(lot, e.wh['hold'], role) == lot['qty'],
                        'Failed ERP lot is no longer fully held')

        doc = e.recover_company_doc(key, 'Stock Entry', role)
        if not doc or doc['docstatus'] == 0:
            doc = e.write(key, 'Stock Entry', {
                'company': e.company, 'stock_entry_type': 'Repack', 'items': rows, 'remarks': remarks,
            }, role, True, before_submit=check)
        check(doc)
        by_batch = {row['batch_no']: row for row in doc['items']}
        flow.verify_row(doc, by_batch[lot['batch']], lot['batch'], lot['qty'], e.wh['hold'], 'Outward', role)
        flow.verify_row(doc, by_batch[batch], batch, salvage, e.wh['hold'], 'Inward', role)
        return doc

    def disposition(self, flow, lot):
        operation = flow.state['rework'][lot['rework_key']]
        authority = operation['authority']
        try:
            retest = self.quality.latest(authority['batch'])
        except APIError as exc:
            raise Pending(str(exc)) from None
        if (not retest or retest['name'] != operation.get('retest') or retest[STATE] != 'Released'
                or retest['docstatus'] != 1 or retest['status'] != 'Accepted'
                or not retest.get('report_date') or retest['report_date'] > flow.e.date()
                or retest['modified_by'] != self.quality.approver_user
                or (retest['custom_retest_of'], retest['custom_rework_execution'],
                    retest['custom_rework_output_batch'], retest['item_code'], retest['custom_spec_revision'],
                    retest['custom_lot_quantity'], retest['custom_lot_uom'])
                != (authority['inspection'], operation['execution'], lot['batch'], lot['item'],
                    lot['revision']['inspection_spec'], lot['qty'], lot['uom'])):
            return None
        try:
            if not self.quality.verifies_retest(retest, authority):
                return None
        except APIError as exc:
            raise Pending(str(exc)) from None
        return {'id': retest['name'], 'version': retest['modified'], 'approver': retest['modified_by'],
                'evidence': retest['name'], 'batch': lot['batch'], 'item': lot['item'],
                'qms_batch': authority['batch'], 'qms_source': self.qms_source,
                'retest_of': authority['inspection'], 'rework_execution': operation['execution'],
                'report_date': retest['report_date'], 'spec_revision': retest['custom_spec_revision'],
                'uom': lot['uom'], 'qty': lot['qty'], 'status': 'released', 'authorized': True}
