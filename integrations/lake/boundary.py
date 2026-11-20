"""Single-process, full-scan Frappe reader and append-only local Delta archive."""
import argparse
import fcntl
from decimal import Decimal
from hashlib import sha256
from urllib.error import HTTPError
from ipaddress import ip_address, ip_network
import json
import os
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from datetime import datetime, timezone

from integrations.qms.adapter import OP
from integrations.qms.sample_reference import (
    ENTITY as SAMPLE_ENTITY, KEY as SAMPLE_KEY, decode as sample_scope, decode_registered,
    reference_row as sample_row,
)

if __package__:
    from .adapter import Lake, canonical, now
    from .erp_scope import ERP_ANALYTICS_ROLES
else:
    from adapter import Lake, canonical, now
    from erp_scope import ERP_ANALYTICS_ROLES

WORKFLOW_ENTITY = 'Quality Inspection Workflow'
DISPOSITION_ENTITY = 'Norvale QMS Disposition'
DISPOSITION_FIELDS = ('inspection', 'inspection_version', 'disposition', 'item_code', 'batch_no',
                      'output_batch', 'spec_revision', 'lot_quantity', 'lot_uom', 'approver')
FIELDS = {
    'Sales Order': 'name modified owner company docstatus status transaction_date delivery_date customer po_no currency grand_total'.split(),
    'Purchase Order': 'name modified company docstatus status transaction_date schedule_date supplier currency grand_total'.split(),
    'Purchase Receipt': 'name modified company docstatus status posting_date supplier currency is_return return_against'.split(),
    'Purchase Invoice': 'name modified company docstatus status posting_date due_date supplier currency grand_total outstanding_amount is_return return_against'.split(),
    'Sales Invoice': 'name modified company docstatus status posting_date due_date customer currency grand_total outstanding_amount is_return return_against'.split(),
    'Bank Transaction': 'name modified company docstatus status date currency party_type party deposit withdrawal allocated_amount unallocated_amount'.split(),
    'Payment Entry': 'name modified company docstatus status posting_date clearance_date payment_type party_type party paid_amount received_amount paid_from_account_currency paid_to_account_currency'.split(),
    'GL Entry': 'name modified company posting_date voucher_type voucher_no account account_currency party_type party debit credit debit_in_account_currency credit_in_account_currency is_cancelled'.split(),
    'Stock Ledger Entry': 'name modified docstatus company posting_date posting_time voucher_type voucher_no voucher_detail_no item_code stock_uom warehouse actual_qty qty_after_transaction stock_value_difference is_cancelled serial_and_batch_bundle'.split(),
    'Norvale Dispatch Lease': 'name creation modified lease_state'.split(),
    SAMPLE_ENTITY: ['name', 'creation', 'modified'],
    DISPOSITION_ENTITY: ['name', 'creation', 'modified', *DISPOSITION_FIELDS],
    WORKFLOW_ENTITY: 'name creation modified reference_type reference_name content owner'.split(),
    'Serial and Batch Bundle': 'name modified creation docstatus item_code voucher_type voucher_no voucher_detail_no warehouse type_of_transaction is_cancelled'.split(),
    'Non Conformance': 'name modified creation status procedure subject custom_qms_batch custom_failed_inspection custom_delivery_source custom_delivery_note custom_delivery_row custom_delivery_customer custom_delivery_batch custom_delivery_qms_source custom_delivery_inspection custom_withdrawal_release custom_withdrawal_approver'.split(),
    'Quality Action': 'name modified creation status corrective_preventive procedure custom_qms_batch custom_non_conformance custom_effectiveness_inspection custom_effectiveness_result'.split(),
    'BOM': 'name modified creation owner company docstatus item uom quantity is_active custom_norvale_revision_key'.split(),
    'Work Order': ('name modified creation company docstatus status production_item bom_no sales_order qty produced_qty stock_uom planned_start_date actual_start_date actual_end_date').split(),
    'Stock Entry': ('name modified creation company docstatus purpose stock_entry_type work_order bom_no fg_completed_qty posting_date').split(),
    'Delivery Note': ('name modified creation company docstatus status customer currency posting_date is_return return_against').split(),
    'Quality Inspection': ('name modified creation docstatus status report_date reference_type reference_name child_row_reference item_code batch_no inspection_type inspected_by manual_inspection workflow_state modified_by custom_spec_revision custom_lot_quantity custom_lot_uom custom_retest_of custom_rework_input_batch custom_rework_salvage_quantity custom_rework_execution custom_rework_output_batch custom_decision_rationale').split(),
}
CHILD = {'resolutions': 'name problem resolution status'.split(), 'entries': 'batch_no serial_no qty warehouse'.split(), 'items': 'name item_code qty uom stock_uom stock_qty batch_no serial_and_batch_bundle s_warehouse t_warehouse warehouse is_finished_item against_sales_order'.split(),
         'readings': 'name specification status numeric manual_inspection formula_based_criteria min_value max_value reading_1'.split()}
CHILD_FOR = {'BOM': ('items',), 'Quality Action': ('resolutions',), 'Serial and Batch Bundle': ('entries',), 'Stock Entry': ('items',), 'Delivery Note': ('items',), 'Quality Inspection': ('readings',)}
EFFECTIVE = {'Stock Entry':'posting_date', 'Delivery Note':'posting_date', 'Quality Inspection':'report_date', 'Work Order':'planned_start_date',
             'InvenTree Publication':'effective_on', 'InvenTree Part':'effective_on', 'InvenTree Change':'on'}

for entity in ('Sales Order', 'Purchase Order', 'Purchase Receipt', 'Sales Invoice', 'Purchase Invoice'):
    CHILD_FOR[entity] = ('items',)
CHILD['items'] += 'schedule_date delivery_date delivered_qty received_qty returned_qty sales_order so_detail delivery_note dn_detail purchase_order purchase_order_item po_detail purchase_receipt pr_detail'.split()
for entity in ('Purchase Order', 'Purchase Invoice'):
    FIELDS[entity].append('payment_terms_template')
    CHILD_FOR[entity] += ('payment_schedule',)
CHILD['payment_schedule'] = 'name payment_term due_date invoice_portion payment_amount outstanding'.split()
for entity in ('Sales Invoice', 'Purchase Invoice'):
    FIELDS[entity] += ['update_stock', 'rounded_total', 'disable_rounded_total', 'party_account_currency']
FIELDS['Purchase Invoice'] += ['on_hold', 'release_date']
FIELDS['Purchase Receipt'].append('currency')
CHILD['references'] = 'name reference_doctype reference_name allocated_amount payment_term due_date'.split()
CHILD_FOR['Payment Entry'] = ('references',)
CHILD_FOR['Bank Transaction'] = ('payment_entries',)
CHILD['payment_entries'] = 'name payment_document payment_entry allocated_amount clearance_date'.split()
EFFECTIVE.update({entity: 'transaction_date' if entity in ('Sales Order','Purchase Order') else 'posting_date'
                  for entity in ERP_ANALYTICS_ROLES})
EFFECTIVE['Bank Transaction'] = 'date'
COMPLAINT_BINDING = ('custom_delivery_batch', 'custom_delivery_qms_source', 'custom_delivery_inspection')

def scalars(obj, keys):
    return {k: obj[k] for k in keys if type(obj.get(k)) in (str, int, float, bool)}


def project(entity, doc):
    if entity not in FIELDS or not isinstance(doc.get('name'), str) or not doc['name']:
        raise ValueError('unsupported entity or missing name')
    result = {k: doc[k] for k in FIELDS[entity] if k in doc}
    if entity == SAMPLE_ENTITY:
        scope = decode_registered(doc)
        if scope:
            row = sample_row(doc, scope, company=scope['company'], warehouse=scope['warehouse'])
            result.update(scalars(scope, ('erp_site', 'qms_site', 'writer', 'analyst')))
            result['inspection'] = doc['registered_inspection']
        else:
            scope = sample_scope(doc)
            row = sample_row(doc, scope)
        result.update(scalars(scope, ('erp_source', 'source_type', 'source_id', 'source_row',
                                      'batch', 'item', 'qty', 'uom')))
        result['qms_row'] = row['name']
    if entity == DISPOSITION_ENTITY:
        if (not all(isinstance(result.get(field), str) and result[field] for field in DISPOSITION_FIELDS
                    if field != 'lot_quantity')
                or result['disposition'] not in ('Released', 'Revoked')
                or type(result.get('lot_quantity')) not in (int, float)
                or not 0 < result['lot_quantity'] < float('inf')):
            raise ValueError('Invalid native QMS disposition')
        datetime.fromisoformat(result['inspection_version'])
    if entity == 'Norvale Dispatch Lease':
        request = json.loads(doc['request_json'])
        if request['id'] != doc['name']:
            raise ValueError('Dispatch lease identity differs from its request')
        if request.get('kind') == 'release_transfer':
            result['kind'] = 'release_transfer'
            fields = ('erp_source', 'stock_entry', 'row', 'company', 'source', 'target', 'posting_date')
            result['transfer'] = scalars(request['transfer'], fields)
            if doc.get('commit_json'):
                commit = json.loads(doc['commit_json'])
                result['commit'] = dict(transfer=scalars(commit['transfer'], fields),
                                        quantity=commit['quantity'],
                                        legs={direction: scalars(leg, ('ledger', 'bundle'))
                                              for direction, leg in commit['legs'].items()})
        elif request.get('kind', 'dispatch') == 'dispatch' and not request.get('transfer'):
            result['delivery'] = request['delivery']
        else:
            raise ValueError('Unknown QMS lease purpose')
        result['scopes'] = [{key: row[key] for key in ('batch', 'item', 'inspection', 'version',
                              'revision', 'uom', 'qty', 'dispatch_qty', 'approver', 'output_batch', 'retest_of',
                              'rework_execution', 'report_date') if key in row} for row in request['scopes']]
    for key in CHILD_FOR.get(entity, ()):
        if key in doc:
            fields = CHILD[key]
            if key == 'items' and entity == 'Stock Entry':
                fields = [*fields, 'transfer_qty', 'conversion_factor']
            if key == 'items' and entity in ('Sales Order', 'Purchase Order', 'Purchase Receipt', 'Purchase Invoice',
                                             'Sales Invoice', 'Delivery Note'):
                fields = [*fields, 'rate']
            result[key] = [{k: row[k] for k in fields if k in row} for row in doc[key]]
    if entity == "BOM" and doc.get("custom_norvale_receiver_marker"):
        from integrations.revisions import valid_receiver_marker
        marker = doc["custom_norvale_receiver_marker"]
        if not valid_receiver_marker("erp", marker):
            raise ValueError("Invalid ERPNext BOM receiver marker")
        result["receiver_marker"] = marker
    # Retain the structured evidence placed on native records by CompanyERP.
    # Arbitrary free-text remarks and additional nested fields stay in the source.
    field = 'custom_norvale_definition' if entity == 'Work Order' else 'remarks'
    if entity == 'Delivery Note' and doc.get('custom_norvale_quality_releases'):
        field = 'custom_norvale_quality_releases'
    if entity in ('Work Order', 'Stock Entry', 'Delivery Note') and isinstance(doc.get(field), str):
        try:
            value = json.loads(doc[field])
        except ValueError:
            value = None
        if entity == 'Work Order' and isinstance(value, dict) and value.get('version'):
            result['definition'] = scalars(value, ('id','version','erp_bom','item','effective_from','approval_id','inspection_spec','plm_source','erp_released_on','replenishes_sales_order','replenishes_line'))
            if 'receiver_markers' in value:
                from integrations.revisions import valid_receiver_marker
                markers = value['receiver_markers']
                if (not isinstance(markers, dict) or not markers
                        or not all(valid_receiver_marker(k, v) for k, v in markers.items())):
                    raise ValueError("Invalid Work Order receiver markers")
                result['definition']['receiver_markers'] = markers
        elif (entity == 'Stock Entry' and doc.get('stock_entry_type') == 'Material Transfer'
              and isinstance(value, dict) and value.get('status') == 'released'):
            result['release_disposition'] = scalars(value, ('id','version','qms_source','batch','qms_batch',
                                                            'item','qty','uom','approver','authorized','retest_of','rework_execution',
                                                            'report_date','spec_revision'))
            if isinstance(value.get('qms_transfer_lease'), str) and value['qms_transfer_lease']:
                result['qms_transfer_lease'] = value['qms_transfer_lease']
        elif entity == 'Stock Entry' and isinstance(value, dict) and value.get('completion_id'):
            result['completion'] = scalars(value, ('completion_id','version','dispatch_id','mes_source','output_lot_id'))
            if 'intermediate_inputs' in value:
                result['intermediate_inputs'] = [
                    {**scalars(row, ('batch','mes_lot_id','erp_manufacture','qms_release','qms_version','qms_source')),
                     'mes_move': scalars(row['mes_move'], ('native_id','source_id','entity'))}
                    for row in value['intermediate_inputs']]
        elif entity == 'Stock Entry' and isinstance(value, dict) and value.get('rework') and isinstance(value.get('qms_authority'), dict):
            result['rework'] = {**scalars(value, ('rework','rework_version','qms_source','scrap_id','scrapped','uom')),
                                'qms_authority': scalars(value['qms_authority'], ('batch','inspection','version','approver','case','quantity'))}
        elif (entity == 'Delivery Note' and doc.get('docstatus') == 1 and doc.get('is_return')
              and isinstance(value, dict) and isinstance(value.get('customer_return'), dict)):
            result['customer_return'] = scalars(value['customer_return'],
                ('complaint','qms_source','erp_source','release','delivery','line','batch','qms_batch','observed_on','quantity'))
        elif entity == 'Delivery Note' and isinstance(value, list):
            result['quality_releases'] = []
            for row in value:
                if isinstance(row, dict) and isinstance(row.get('disposition'), dict):
                    result['quality_releases'].append({**scalars(row, ('batch','qty','qms_lease')),
                        'disposition': scalars(row['disposition'], ('id','version','item','batch','qty','status','qms_source','qms_batch'))})
    return result

class NoSourceRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Source API redirected an authenticated read")


class Frappe:
    def __init__(self, url, key, secret, site=None, *, private_origin=None):
        u = urlparse(url)
        private = False
        if private_origin == url and u.hostname and not u.username and not u.password:
            address = ip_address(u.hostname)
            private = any(address in ip_network(cidr) for cidr in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
        if u.scheme != 'https' and not (u.scheme == 'http' and (private or u.hostname in ('localhost', '127.0.0.1', '::1'))):
            raise ValueError('HTTPS, loopback, or the configured private Compose origin is required')
        if not key or not secret: raise ValueError('Frappe credentials required')
        self.opener = build_opener(NoSourceRedirect())
        self.url = url.rstrip('/')
        self.headers = {'Authorization': 'token ' + key + ':' + secret, 'Accept': 'application/json'}
        if site:
            self.headers['X-Frappe-Site-Name'] = site
    def response(self, path):
        req = Request(self.url + path, headers=self.headers)
        with self.opener.open(req, timeout=30) as response: return json.load(response)
    def get(self, path):
        return self.response(path)['data']
    def documents(self, entity, *, registered_sample=None, primary_sample=True):
        if entity not in FIELDS: raise ValueError('unsupported entity')
        if entity == WORKFLOW_ENTITY:
            for inspection in self.names('Quality Inspection'):
                query = urlencode({'doctype': 'Quality Inspection', 'name': inspection})
                info = self.response('/api/method/frappe.desk.form.load.get_docinfo?' + query)['docinfo']
                if (info['doctype'], info['name']) != ('Quality Inspection', inspection):
                    raise ValueError('Workflow timeline belongs to another record')
                for event in info['workflow_logs']:
                    yield {**event, 'reference_type': 'Quality Inspection', 'reference_name': inspection}
            return
        if entity == SAMPLE_ENTITY:
            seen = set()
            for inspection in self.documents('Quality Inspection'):
                reference = inspection.get('reference_name')
                if (inspection.get('docstatus') != 1 or inspection.get('reference_type') != 'Stock Entry'
                        or not reference):
                    continue
                doc = self.get('/api/resource/Stock%20Entry/' + quote(reference, safe=''))
                registered = registered_sample(doc) if registered_sample else None
                if registered:
                    scope = registered
                    if (doc.get(SAMPLE_KEY) or inspection.get('owner') != scope['analyst']
                            or inspection.get('inspected_by') != scope['analyst']):
                        raise ValueError('Inspection differs from the registered lab sample')
                    row = sample_row(doc, scope, company=scope['company'], warehouse=scope['warehouse'])
                elif primary_sample and doc.get(SAMPLE_KEY):
                    scope = sample_scope(doc)
                    row = sample_row(doc, scope)
                else:
                    continue
                # The salvage retest references the failed sample's original receipt.
                # Its measured quantity belongs to the authorized output lot.
                if any(inspection.get(k) for k in ('custom_retest_of', 'custom_rework_execution', 'custom_rework_output_batch')):
                    parent = inspection.get('custom_retest_of')
                    if not isinstance(parent, str) or not parent or parent == inspection['name']:
                        raise ValueError('Missing original inspection for the salvage retest')
                    original = self.get('/api/resource/Quality%20Inspection/' + quote(parent, safe=''))
                    quantity = inspection.get('custom_lot_quantity')
                    same = ('reference_name', 'reference_type', 'child_row_reference', 'batch_no',
                            'item_code', 'custom_spec_revision', 'custom_lot_uom')
                    if (original.get('name') != parent or original.get('docstatus') != 1
                            or original.get('status') != 'Rejected' or original.get('workflow_state') != 'Rework Authorized'
                            or original.get('custom_retest_of') or original.get('custom_rework_input_batch') != scope['batch']
                            or not all(inspection.get(k) and inspection[k] == original.get(k) for k in same)
                            or not inspection.get('custom_rework_execution') or not inspection.get('custom_rework_output_batch')
                            or inspection['custom_rework_output_batch'] == scope['batch']
                            or type(quantity) not in (int, float) or not 0 < quantity <= scope['qty']
                            or quantity != original.get('custom_rework_salvage_quantity')):
                        raise ValueError('Salvage retest differs from the authorized original sample')
                    inspection = original
                if registered:
                    if (inspection.get(OP) != scope['operation'] or inspection.get('owner') != scope['analyst']
                            or inspection.get('inspected_by') != scope['analyst']):
                        raise ValueError('Original inspection differs from the registered lab sample')
                    doc = dict(doc, registered_inspection=inspection['name'])
                if (doc.get('name'), row['name'], scope['batch'], scope['item'], scope['qty'], scope['uom']) != (
                        reference, inspection.get('child_row_reference'), inspection.get('batch_no'),
                        inspection.get('item_code'), inspection.get('custom_lot_quantity'),
                        inspection.get('custom_lot_uom')):
                    raise ValueError('Inspection does not identify its commissioned sample reference')
                if reference not in seen:
                    seen.add(reference)
                    yield doc
            return
        site_marker = None
        for name in self.names(entity):
            doc = self.get('/api/resource/' + quote(entity, safe='') + '/' + quote(name, safe=''))
            if doc.get('name') != name:
                raise ValueError('Native response belongs to another record')
            if entity == 'BOM' and (marker := doc.get('custom_norvale_receiver_marker')):
                if site_marker is None:
                    docs = self.response('/api/method/frappe.desk.form.load.getdoctype?doctype=BOM')['docs']
                    from integrations.revisions.receivers import erp_receiver_marker
                    site_marker = erp_receiver_marker(next(row for row in docs if row['name'] == 'BOM'))
                if site_marker != marker:
                    raise ValueError('ERPNext BOM marker differs from its native site')
            yield doc
    def missing(self, entity, name):
        """Confirm an absent former record through its authenticated resource URL."""
        path = '/api/resource/' + quote(entity, safe='') + '/' + quote(name, safe='')
        try:
            return self.get(path)
        except HTTPError as exc:
            if exc.code == 403:
                return None
            if exc.code != 404:
                raise
            error = json.load(exc)
            expected = f'{entity} {name} not found'
            messages = [json.loads(m).get('message') for m in json.loads(error.get('_server_messages', '[]'))]
            if error.get('exc_type') != 'DoesNotExistError' or expected not in messages:
                raise
            return {'name': name, 'source_absent': True}

    def names(self, entity, *, page_size=100):
        # A deletion before the cursor must not shift unseen names off the next page.
        if type(page_size) is not int or page_size < 1:
            raise ValueError('Expected a positive page size')
        after = None
        while True:
            params = {'fields': json.dumps(['name']), 'order_by':'name asc', 'limit_page_length':page_size}
            if after is not None:
                params['filters'] = json.dumps([['name', '>', after]])
            try:
                page = self.get('/api/resource/' + quote(entity, safe='') + '?' + urlencode(params))
            except HTTPError as exc:
                if entity not in (DISPOSITION_ENTITY, 'Norvale Dispatch Lease') or after is not None or exc.code != 404:
                    raise
                error = json.load(exc)
                messages = [json.loads(m).get('message') for m in json.loads(error.get('_server_messages', '[]'))]
                if (error.get('exc_type') != 'DoesNotExistError'
                        or f'DocType {entity} not found' not in messages):
                    raise
                # An older QMS site may lack the commissioned evidence type.
                return
            if after is not None and any(row['name'] == after for row in page):
                raise ValueError('Source list did not advance past its cursor')
            for row in page:
                yield row['name']
            if len(page) < page_size:
                return
            after = page[-1]['name']

def scan_key(source, entity, scope=None):
    if scope is not None:
        return canonical([source, entity, scope])
    return entity if source == "erpnext" else canonical([source, entity])


def ingest(root, entity, fetch=None, *, source="erpnext", projection=project, reader=None, scope=None,
           open_reader=None):
    if not isinstance(source, str) or not source.strip():
        raise ValueError("A source instance name is required")
    root = Path(root); root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    with (root/'writer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = root/'state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        key = scan_key(source, entity, scope)
        entry = state.setdefault(key, {})
        if scope is not None:
            entry['scope'] = scope
        entry.update(source=source, entity=entity)
        entry['last_attempt'] = now()
        try:
            if open_reader is not None:
                reader = open_reader()
                records = reader.documents(entity)
            else:
                records = fetch()
            docs = [projection(entity, d) for d in records]
            lake = Lake(root/'delta', {})
            latest = lake.latest_hashes(source, entity)
            if entity == DISPOSITION_ENTITY:
                hashes = {doc['name']: sha256(canonical(doc).encode()).hexdigest() for doc in docs}
                if len(hashes) != len(docs) or any(hashes.get(name) != digest for name, digest in latest.items()):
                    raise ValueError('Native QMS disposition history changed or disappeared')
            elif reader and entity not in (WORKFLOW_ENTITY, SAMPLE_ENTITY):
                observed = {doc['name'] for doc in docs}
                for name in latest.keys() - observed:
                    tombstone = {'name': name, 'source_absent': True}
                    if latest[name] == sha256(canonical(tombstone).encode()).hexdigest():
                        continue
                    record = reader.missing(entity, name)
                    if record is not None:
                        docs.append(tombstone if record == tombstone else projection(entity, record))
            count = lake.ingest(source, entity, docs, EFFECTIVE.get(entity), latest=latest)
            marks = [str(d.get('modified') or '') for d in docs]
            entry.update(last_success=now(), watermark=max([entry.get('watermark', '')] + marks), error=None, records=len(docs))
        except Exception as exc:
            # Avoid storing source error bodies, which can include credentials or sensitive records.
            entry['error'] = type(exc).__name__
            raise
        finally:
            tmp = state_path.with_suffix('.tmp')
            tmp.write_text(canonical(state)); os.chmod(tmp, 0o600); os.replace(tmp, state_path)
        return count

def trace(docs):
    edges = set()
    for entity, rows in docs.items():
        for doc in rows:
            name = doc['name']
            for field, target in [('work_order','Work Order'),('sales_order','Sales Order'),('bom_no','BOM')]:
                if doc.get(field): edges.add((entity,name,target,doc[field]))
            if doc.get('reference_type') and doc.get('reference_name'):
                edges.add((entity,name,doc['reference_type'],doc['reference_name']))
            for field, target in (('custom_qms_batch','Batch'),('custom_retest_of','Quality Inspection'),
                                  ('custom_effectiveness_inspection','Quality Inspection'),('custom_delivery_inspection','Quality Inspection'),('custom_failed_inspection','Quality Inspection'),('custom_non_conformance','Non Conformance')):
                if doc.get(field): edges.add((entity,name,target,doc[field]))
            if entity == DISPOSITION_ENTITY:
                edges.add((entity, name, 'Quality Inspection', doc['inspection']))
            if doc.get('batch_no'): edges.add((entity,name,'Batch',doc['batch_no']))
            if entity == 'InvenTree Change':
                edges.add((entity, name, 'InvenTree Part', str(doc['part_id'])))
            if entity in ('InvenTree Publication', 'InvenTree BOM', 'InvenTree Part'):
                if doc.get('revision_of') is not None:
                    edges.add((entity,name,'InvenTree Part',str(doc['revision_of'])))
                for component in doc['components']:
                    line_id = str(component['native_id'])
                    edges.add((entity,name,'InvenTree BOM Line',line_id))
                    edges.add(('InvenTree BOM Line',line_id,'InvenTree Part',str(component['component_id'])))
            if entity == 'stock.move' and doc.get('receipt'):
                edges.add((entity, name, 'mrp.production', str(doc['production_id'])))
                edges.add((entity, name, 'stock.move.line', str(doc['line_id'])))
                edges.add(('stock.move.line', str(doc['line_id']), 'stock.lot', str(doc['lot_id'])))
            if entity == 'mrp.production':
                if doc.get('backorder_id'):
                    edges.add((entity,name,entity,doc['backorder_id']))
                if doc.get('hold_location_id'):
                    edges.add((entity,name,'stock.location',doc['hold_location_id']))
                for operation in doc.get('work_order_ids', []):
                    edges.add((entity,name,'mrp.workorder',operation))
                for movement in doc['inputs'] + [doc['output']]:
                    edges.add((entity,name,'stock.move.line',movement['line_id']))
                    edges.add(('stock.move.line',movement['line_id'],'stock.lot',movement['lot_id']))
                edges.add((entity,name,'mrp.bom',doc['bom_id']))
            if entity == 'Serial and Batch Bundle':
                if doc.get('voucher_type') and doc.get('voucher_no'):
                    edges.add((entity,name,doc['voucher_type'],doc['voucher_no']))
                for entry in doc.get('entries', []):
                    if entry.get('batch_no'):
                        edges.add((entity,name,'Batch',entry['batch_no']))
            if entity in ('GL Entry', 'Stock Ledger Entry') and doc.get('voucher_type') and doc.get('voucher_no'):
                edges.add((entity,name,doc['voucher_type'],doc['voucher_no']))
            if entity == 'Bank Transaction':
                for allocation in doc.get('payment_entries', []):
                    if allocation.get('payment_document') == 'Payment Entry' and allocation.get('payment_entry'):
                        edges.add((entity,name,'Payment Entry',allocation['payment_entry']))
            if entity == 'Payment Entry':
                for row in doc.get('references', []):
                    if row.get('reference_doctype') and row.get('reference_name'):
                        edges.add((entity,name,row['reference_doctype'],row['reference_name']))
            if doc.get('is_return') and doc.get('return_against'):
                edges.add((entity,name,entity,doc['return_against']))
            for item in doc.get('items', []):
                if item.get('serial_and_batch_bundle'):
                    edges.add((entity,name,'Serial and Batch Bundle',item['serial_and_batch_bundle']))
                if item.get('batch_no'): edges.add((entity,name,'Batch',item['batch_no']))
                for field, target in [('against_sales_order','Sales Order'), ('sales_order','Sales Order'),
                                      ('delivery_note','Delivery Note'), ('purchase_order','Purchase Order'),
                                      ('purchase_receipt','Purchase Receipt')]:
                    if item.get(field):
                        edges.add((entity,name,target,item[field]))
    return sorted(edges)

def reference_index(sources):
    """Index native references by source, version and business scope."""
    from collections import defaultdict
    index = defaultdict(set)
    shipped_batches = defaultdict(set)
    inward = {}
    for source, entities in sources.items():
        for bundle in entities.get('Serial and Batch Bundle', []):
            if (bundle.get('docstatus') == 1 and not bundle.get('is_cancelled')
                    and bundle.get('voucher_type') == 'Delivery Note'):
                key = (source, bundle['name'], bundle.get('voucher_no'), bundle.get('voucher_detail_no'))
                if bundle.get('type_of_transaction') == 'Inward':
                    inward[key] = bundle
                elif bundle.get('type_of_transaction') == 'Outward':
                    shipped_batches[key].update(e['batch_no'] for e in bundle.get('entries', [])
                                                if e.get('batch_no') and e.get('qty',0) < 0)
    for source, entities in sources.items():
        for entity, docs in entities.items():
            for doc in docs:
                target = (source, entity, doc['name'])
                if entity in ('Purchase Receipt', 'Stock Entry') and doc.get('docstatus') == 1:
                    for row in doc.get('items', []):
                        if not row.get('s_warehouse') and (row.get('warehouse') or row.get('t_warehouse')):
                            qty = row.get('transfer_qty') or row.get('stock_qty') or row.get('qty')
                            index['sample_erp_row', source, entity, doc['name'], row.get('name'),
                                  row.get('batch_no'), row.get('item_code'), qty, row.get('stock_uom')].add(target)
                if entity == SAMPLE_ENTITY:
                    ref = ('sample_reference', source, doc['name'], doc['qms_row'],
                           doc['batch'], doc['item'], doc['qty'], doc['uom'])
                    if 'inspection' in doc:
                        ref += (doc['inspection'],)
                    index[ref].add(target)
                if entity == DISPOSITION_ENTITY and doc.get('disposition') == 'Released':
                    identity = (doc.get('inspection'), doc.get('inspection_version'), doc.get('batch_no'),
                                doc.get('output_batch'), doc.get('item_code'), doc.get('lot_quantity'),
                                doc.get('lot_uom'), doc.get('approver'))
                elif (entity == 'Quality Inspection' and doc.get('docstatus') == 1
                        and doc.get('workflow_state') == 'Released' and doc.get('status') == 'Accepted'):
                    identity = (doc['name'], doc.get('modified'), doc.get('batch_no'),
                                doc.get('custom_rework_output_batch') or doc.get('batch_no'),
                                doc.get('item_code'), doc.get('custom_lot_quantity'), doc.get('custom_lot_uom'),
                                doc.get('modified_by'))
                else:
                    identity = None
                if identity and all(identity):
                    inspection = (source, 'Quality Inspection', identity[0])
                    index['release_transfer', *identity].add(inspection)
                    index['release_transfer', source, *identity].add(inspection)
                if entity == 'Delivery Note' and doc.get('docstatus') == 1 and not doc.get('is_return'):
                    for line in doc.get('items', []):
                        line_id = line.get('name')
                        key = (source, line.get('serial_and_batch_bundle'), doc['name'], line_id)
                        for batch in shipped_batches[key]:
                            index[('shipment',source,doc['name'],line_id,doc.get('customer'),batch)].add(target)
                if (entity == 'Quality Inspection' and doc.get('docstatus') == 1
                        and doc.get('status') == 'Accepted' and doc.get('workflow_state') in ('Released', 'Revoked')):
                    original = doc.get('batch_no')
                    index[('complaint_release',source,doc['name'],original,
                           doc.get('custom_rework_output_batch') or original)].add(target)
                if (entity == 'Non Conformance' and doc.get('custom_withdrawal_approver')
                        and (not any(doc.get(f) for f in COMPLAINT_BINDING) or (
                            doc['custom_delivery_qms_source'] == source
                            and doc.get('custom_delivery_batch')
                            and doc.get('custom_delivery_inspection') == doc.get('custom_withdrawal_release')))):
                    index[('return_case',source,doc['name'],doc.get('custom_delivery_source'),
                           doc.get('custom_delivery_note'),doc.get('custom_delivery_row'),
                           doc.get('custom_delivery_customer'),doc.get('custom_delivery_batch') or doc.get('custom_qms_batch'),
                           doc.get('custom_qms_batch'),doc.get('custom_withdrawal_release'))].add(target)
                if entity == 'mrp.production':
                    index[('completion', doc['completion_ref'], doc['modified'])].add(target)
                    for identifier in (doc['name'], doc['completion_ref']):
                        index[('completion',source,identifier,doc['modified'])].add(target)
                elif entity == 'InvenTree Publication':
                    ref = (str(doc['name']), doc['version'])
                    index[('publication', *ref)].add(target)
                    index[('publication', source, *ref)].add(target)
                elif entity == 'Quality Inspection' and doc.get('docstatus') == 1 and doc.get('status') == 'Accepted':
                    batch = doc.get('batch_no')
                    shipped_batch = doc.get('custom_rework_output_batch') or batch
                    index[('inspection_scope', source, doc['name'], doc.get('item_code'), batch, shipped_batch)].add(target)
                    if doc.get('workflow_state') != 'Released':
                        continue
                    ref = (doc['name'], doc.get('modified'), shipped_batch, doc.get('item_code'))
                    index[('release', *ref)].add(target)
                    index[('release', source, *ref)].add(target)
    return index, inward


def release_transfer_reference(doc):
    disposition = doc['release_disposition']
    batch = disposition.get('batch')
    scope = (disposition['qms_source'],) if 'qms_source' in disposition else ()
    ref = ('release_transfer', *scope, disposition.get('id'), disposition.get('version'),
           disposition.get('qms_batch', batch), batch, disposition.get('item'), disposition.get('qty'),
           disposition.get('uom'), disposition.get('approver'))
    rows = doc.get('items', [])
    row = rows[0] if len(rows) == 1 else {}
    valid = (doc.get('docstatus') == 1 and doc.get('stock_entry_type') == 'Material Transfer'
             and disposition.get('authorized') is True and row.get('item_code') == disposition.get('item')
             and row.get('stock_uom') == disposition.get('uom') and row.get('batch_no') == batch
             and row.get('s_warehouse') and row.get('t_warehouse') and row['s_warehouse'] != row['t_warehouse']
             and type(row.get('transfer_qty')) in (int, float) and type(disposition.get('qty')) in (int, float)
             and 0 < row['transfer_qty'] <= disposition['qty'])
    return ref, bool(valid)


def crosslinks(sources):
    """Resolve native references only when the available source records identify a single match."""
    index, inward = reference_index(sources)
    edges, unresolved = set(), set()
    for source, entities in sources.items():
        for entity, docs in entities.items():
            for doc in docs:
                refs = []
                if entity == SAMPLE_ENTITY:
                    refs.append(('sample_erp_row', doc['erp_source'], doc['source_type'], doc['source_id'],
                                 doc['source_row'], doc['batch'], doc['item'], doc['qty'], doc['uom']))
                if (entity == 'Quality Inspection' and doc.get('reference_type') == 'Stock Entry'
                        and doc.get('reference_name') and doc.get('docstatus') == 1 and not doc.get('custom_retest_of')):
                    ref = ('sample_reference', source, doc['reference_name'], doc.get('child_row_reference'),
                           doc.get('batch_no'), doc.get('item_code'), doc.get('custom_lot_quantity'),
                           doc.get('custom_lot_uom'))
                    qualified = (*ref, doc['name'])
                    if qualified in index:
                        refs.append(qualified)
                    elif ref in index:
                        refs.append(ref)
                if entity == 'Non Conformance' and doc.get('custom_delivery_note'):
                    refs.append(('shipment',doc.get('custom_delivery_source'),doc['custom_delivery_note'],
                                 doc.get('custom_delivery_row'),doc.get('custom_delivery_customer'),
                                 doc.get('custom_delivery_batch') or doc.get('custom_qms_batch')))
                    if any(doc.get(field) for field in COMPLAINT_BINDING):
                        binding = ('complaint_release', doc.get('custom_delivery_qms_source'),
                                   doc.get('custom_delivery_inspection'), doc.get('custom_qms_batch'),
                                   doc.get('custom_delivery_batch'))
                        if binding[1] == source:
                            refs.append(binding)
                        else:
                            refs.clear()
                            unresolved.add(((source,entity,doc['name']), json.dumps(binding), 0))
                if entity == 'Delivery Note' and (receipt := doc.get('customer_return')):
                    ref = ('return_case',receipt.get('qms_source'),receipt.get('complaint'),source,
                           receipt.get('delivery'),receipt.get('line'),doc.get('customer'),
                           receipt.get('batch'),receipt.get('qms_batch', receipt.get('batch')),receipt.get('release'))
                    rows = doc.get('items', [])
                    row = rows[0] if len(rows) == 1 else {}
                    bundle = inward.get((source,row.get('serial_and_batch_bundle'),doc['name'],row.get('name')), {})
                    entries = bundle.get('entries', [])
                    if (doc.get('docstatus') == 1 and doc.get('is_return') and entries
                            and receipt.get('erp_source') == source
                            and receipt.get('delivery') == doc.get('return_against')
                            and receipt.get('line') == row.get('dn_detail')
                            and bundle.get('item_code') == row.get('item_code')
                            and bundle.get('warehouse') == row.get('warehouse')
                            and all(e.get('batch_no') == receipt.get('batch') and not e.get('serial_no')
                                    and e.get('qty',0) > 0 for e in entries)
                            and type(receipt.get('quantity')) in (int, float)
                            and sum(Decimal(str(e['qty'])) for e in entries) == Decimal(str(receipt['quantity']))):
                        refs.append(ref)
                    else:
                        unresolved.add(((source,entity,doc['name']), json.dumps(ref), 0))
                if entity == 'Stock Entry' and doc.get('docstatus') == 1 and doc.get('release_disposition'):
                    ref, valid = release_transfer_reference(doc)
                    if valid:
                        refs.append(ref)
                    else:
                        unresolved.add(((source, entity, doc['name']), json.dumps(ref), 0))
                if entity == 'Work Order' and (pin := doc.get('definition')):
                    scope = (pin['plm_source'],) if 'plm_source' in pin else ()
                    refs.append(('publication', *scope, str(pin.get('id')), pin.get('version')))
                if entity == 'Stock Entry' and (report := doc.get('completion')):
                    parts = (report['mes_source'],) if report.get('mes_source') else ()
                    refs.append(('completion', *parts, report.get('completion_id'), report.get('version')))
                for row in doc.get('quality_releases', []):
                    d = row['disposition']
                    if d.get('status') == 'released' and d.get('batch') == row.get('batch'):
                        scope = (d['qms_source'],) if 'qms_source' in d else ()
                        refs.append(('release',*scope,d.get('id'),d.get('version'),row.get('batch'),d.get('item')))
                origin = (source, entity, doc['name'])
                for ref in refs:
                    matches = index[ref] if None not in ref else set()
                    if len(matches) == 1:
                        edges.add((origin, ref[0], next(iter(matches))))
                    else:
                        unresolved.add((origin, json.dumps(ref), len(matches)))
    return {'edges': sorted(edges), 'unresolved': sorted(unresolved)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    sub = parser.add_subparsers(dest='cmd', required=True)
    run = sub.add_parser('ingest'); run.add_argument('--offline', help='filtered native-records.json'); run.add_argument('--url'); run.add_argument('--entity', choices=FIELDS, action='append')
    run.add_argument('--site', help='Frappe site name when connecting through a shared listener')
    run.add_argument('--source', default='erpnext', help='Stable application instance name')
    status = sub.add_parser('status'); status.add_argument('--max-age', type=int, default=86400, help='stale after this many seconds')
    export = sub.add_parser('export'); export.add_argument('--entity', required=True)
    sub.add_parser('trace')
    sub.add_parser('crosslinks')
    genealogy = sub.add_parser('genealogy')
    genealogy.add_argument('--source', required=True)
    target = genealogy.add_mutually_exclusive_group(required=True)
    target.add_argument('--delivery')
    target.add_argument('--receipt', help='Submitted inbound Purchase Receipt')
    target.add_argument('--transfer', help='Stock Entry releasing a lot from quarantine')
    args = parser.parse_args(); root = Path(args.root)
    if args.cmd == 'ingest':
        if bool(args.offline) == bool(args.url): parser.error('choose exactly one of --offline and --url')
        data = json.loads(Path(args.offline).read_text()) if args.offline else None
        client = Frappe(args.url, os.getenv('FRAPPE_API_KEY'), os.getenv('FRAPPE_API_SECRET'), args.site) if args.url else None
        failed = False
        for entity in args.entity or FIELDS:
            try:
                count = ingest(root, entity, lambda: data.get(entity, {}).values() if data is not None else client.documents(entity), source=args.source, reader=client)
                print(json.dumps({'entity':entity, 'appended':count}))
            except Exception as exc:
                failed = True; print(json.dumps({'entity':entity,'error':type(exc).__name__}))
        if failed: raise SystemExit(1)
    elif args.cmd == 'status':
        state = json.loads((root/'state.json').read_text()) if (root/'state.json').exists() else {}
        for entry in state.values():
            last = entry.get('last_success')
            entry['age_seconds'] = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() if last else None
            entry['stale'] = bool(entry.get('error')) or not last or entry['age_seconds'] > args.max_age
        print(json.dumps(state))
    else:
        lake = Lake(root/'delta', {})
        if args.cmd == 'genealogy':
            from genealogy import ShipmentHistory
            history = ShipmentHistory(lake._rows(), args.source)
            if args.transfer:
                result = history.transfer_lineage(args.transfer)
                if result is None:
                    parser.exit(1, 'Release transfer lacks matched QMS, bundle or ledger evidence\n')
            elif args.receipt:
                result = history.purchase_receipt(args.receipt)
            else:
                result = history.trace(args.delivery)
            print(json.dumps(result))
            if args.transfer and result['authorization_at_transfer'] != 'confirmed':
                parser.exit(1, 'Release lineage found. Authorization at transfer remains unresolved.\n')
            return
        if args.cmd == 'export':
            print(json.dumps(lake._rows(entity=args.entity)))
            return
        rows = lake._rows()
        # latest observation by ingestion, never conflate separate source versions
        latest = {}
        for row in sorted(rows, key=lambda r: r['ingested_at']):
            latest[(row['source'], row['entity'], row['native_id'])] = json.loads(row['payload'])
        sources = {}
        for (source, entity, _), doc in latest.items():
            sources.setdefault(source, {}).setdefault(entity, []).append(doc)
        if args.cmd == 'crosslinks':
            # A shipment may cite a past release that has since been revoked.
            history = {}
            for row in rows:
                history.setdefault(row['source'], {}).setdefault(row['entity'], []).append(json.loads(row['payload']))
            print(json.dumps(crosslinks(history)))
        else:
            print(json.dumps({source: trace(docs) for source, docs in sources.items()}))

if __name__ == '__main__': main()
