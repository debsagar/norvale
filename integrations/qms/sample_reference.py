"""Wire contract for native QMS sample references."""
import json
from hashlib import sha256

KEY = 'custom_norvale_sample_key'
SCOPE = 'custom_norvale_sample_scope'
ENTITY = 'QMS Sample Reference'
COMPANY = 'Norvale QMS'
WAREHOUSE = 'QMS Quarantine - NQ'
INCOMING = 'Norvale Incoming Quality Index'
ORIGINS = ('Purchase Receipt', 'Stock Entry')
LOT_FIELDS = ('source_type', 'source_id', 'source_row', 'batch', 'item', 'qty', 'uom')
REGISTERED_PREFIX = 'Norvale registered sample v1: '
REGISTERED_FIELDS = (*LOT_FIELDS, 'erp_source', 'erp_site', 'qms_source', 'qms_site',
                     'writer', 'analyst', 'company', 'warehouse', 'operation')


def reference_key(batch):
    return sha256(batch.encode()).hexdigest()


def reference_row(doc, scope, *, company=COMPANY, warehouse=WAREHOUSE):
    rows = doc.get('items', [])
    if (doc.get('docstatus') != 0 or doc.get('company') != company
            or doc.get('stock_entry_type') != 'Material Receipt' or len(rows) != 1):
        raise ValueError('Invalid QMS sample receipt')
    row = rows[0]
    if (not row.get('name') or row.get('s_warehouse') or row.get('t_warehouse') != warehouse
            or tuple(row.get(k) for k in ('batch_no', 'item_code', 'qty', 'stock_uom')) !=
            tuple(scope[k] for k in ('batch', 'item', 'qty', 'uom'))):
        raise ValueError('QMS sample receipt row differs from its source')
    return row


def decode(doc):
    scope = json.loads(doc[SCOPE])
    required = ('erp_source', 'erp_instance', 'source_type', 'source_id', 'source_row',
                'batch', 'item', 'qty', 'uom', 'qms_source', 'qms_instance', 'writer')
    if (not isinstance(scope, dict) or set(scope) != set(required)
            or not all(type(scope[k]) is str and scope[k] for k in required if k != 'qty')
            or type(scope['qty']) not in (int, float) or not 0 < scope['qty'] < float('inf')
            or scope['source_type'] not in ORIGINS or doc.get('owner') != scope['writer']
            or doc.get(KEY) != reference_key(scope['batch'])):
        raise ValueError('Invalid QMS sample source binding')
    return scope


def registered_binding(erp_source, cfg, item):
    lab = cfg['recurring_lab']
    return dict(item=item, erp_source=erp_source, erp_site=cfg['erp']['site'],
                qms_source=cfg['qms_source'], qms_site=cfg['qms_reader']['site'],
                writer=lab['reference_user'], analyst=lab['analyst_user'],
                company=lab['company'], warehouse=lab['warehouse'])


def registered_note(scope):
    return REGISTERED_PREFIX + json.dumps(scope, sort_keys=True, separators=(',', ':'))


def decode_registered(doc):
    note = doc.get('remarks')
    if not isinstance(note, str) or not note.startswith(REGISTERED_PREFIX):
        return None
    scope = json.loads(note[len(REGISTERED_PREFIX):])
    if (not isinstance(scope, dict) or set(scope) != set(REGISTERED_FIELDS)
            or not all(type(scope[k]) is str and scope[k] for k in REGISTERED_FIELDS if k != 'qty')
            or type(scope['qty']) not in (int, float) or not 0 < scope['qty'] < float('inf')
            or scope['source_type'] not in ORIGINS or doc.get('owner') != scope['writer']
            or note != registered_note(scope)):
        raise ValueError('Invalid registered QMS sample source binding')
    return scope


def submitted_row(doc, scope, company):
    rows = [row for row in doc['items'] if row.get('name') == scope['source_row']]
    if (doc.get('doctype'), doc.get('name'), doc.get('company'), doc.get('docstatus')) != (
            scope['source_type'], scope['source_id'], company, 1) or len(rows) != 1:
        raise ValueError('Sample source is not the submitted ERP row')
    row = rows[0]
    qty = row.get('transfer_qty') or row.get('stock_qty') or row.get('qty')
    if (row.get('s_warehouse') or not (row.get('t_warehouse') or row.get('warehouse'))
            or (row.get('batch_no'), row.get('item_code'), qty, row.get('stock_uom')) !=
            tuple(scope[k] for k in ('batch', 'item', 'qty', 'uom'))):
        raise ValueError('Sample differs from the submitted ERP lot')
    return row
