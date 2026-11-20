"""Run inside the isolated QMS bench. Reconcile sample references for actual ERP lots."""
import json
import os
import frappe

frappe.init(site='qms.localhost', sites_path='.')
frappe.connect()
frappe.set_user('Administrator')
try:
    parameter = os.environ.get('COMPANY_INCOMING_PARAMETER')
    if parameter and not frappe.db.exists('Quality Inspection Parameter', parameter):
        frappe.get_doc(dict(doctype='Quality Inspection Parameter', parameter=parameter)).insert()
    result = {}
    for lot in json.loads(os.environ['COMPANY_LOTS']):
        item, batch = lot['item'], lot['batch']
        if not frappe.db.exists('Item', item):
            frappe.get_doc(dict(doctype='Item', item_code=item, item_name=item,
                                stock_uom=lot['uom'], item_group='Raw Material',
                                is_stock_item=1, has_batch_no=1)).insert()
        if not frappe.db.exists('Batch', batch):
            frappe.get_doc(dict(doctype='Batch', batch_id=batch, item=item)).insert()
        note = 'ERP ' + lot['source_type'] + ' ' + lot['source_id'] + ' batch ' + batch
        name = frappe.db.get_value('Stock Entry', {'remarks': note}, 'name')
        if not name:
            doc = frappe.get_doc(dict(doctype='Stock Entry', stock_entry_type='Material Receipt',
                company='Norvale QMS', remarks=note, items=[dict(item_code=item, qty=lot['qty'],
                basic_rate=1, t_warehouse='QMS Quarantine - NQ', batch_no=batch)])).insert()
            name = doc.name
        result[batch] = name
    frappe.db.commit()
    print(json.dumps(result))
finally:
    frappe.destroy()
