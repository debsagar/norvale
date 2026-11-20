"""Seed synthetic native master data and a local inspection reference."""

import contextlib
import json
import sys

import frappe

from configure import ensure


def seed():
    company = "Norvale QMS"
    item = "NV-QMS-SHAMPOO"
    procedure = "Norvale Shampoo Lot Investigation"
    parameter = "Norvale Shampoo pH"
    ensure("Warehouse", "QMS Quarantine - NQ", warehouse_name="QMS Quarantine", company=company)
    ensure("Item", item, item_code=item, item_name="Norvale Shampoo (fixture)",
           item_group="Raw Material", stock_uom="Nos", is_stock_item=1, has_batch_no=1)
    ensure("Quality Procedure", procedure, quality_procedure_name=procedure)
    ensure("Quality Inspection Parameter", parameter, parameter=parameter)
    batch = "NV-QMS-" + frappe.generate_hash(length=12)
    ensure("Batch", batch, batch_id=batch, item=item)
    entry = frappe.get_doc({
        "doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": company,
        "items": [{"item_code": item, "qty": 100, "basic_rate": 1,
                   "t_warehouse": "QMS Quarantine - NQ", "batch_no": batch}],
    }).insert()
    frappe.db.commit()
    return {"batch": batch, "item": item, "procedure": procedure, "parameter": parameter,
            "reference": entry.name, "date": frappe.utils.today()}


if __name__ == "__main__":
    frappe.init(site="qms.localhost", sites_path=".")
    frappe.connect()
    frappe.set_user("Administrator")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            fixture = seed()
        print(json.dumps(fixture))
    finally:
        frappe.destroy()
