"""Create a submitted two-unit test delivery on the isolated feedback site via Bench."""
import json
import sys
import frappe


def delivery(fixture):
    from frappe.permissions import add_permission
    for doctype in ("Delivery Note", "Serial and Batch Bundle"):
        role = "Norvale QMS Reader"
        if not frappe.db.exists("Custom DocPerm", {"parent":doctype, "role":role}):
            perm = frappe.get_doc("Custom DocPerm", add_permission(doctype, role))
            perm.read = 1
            perm.save()
    if not frappe.db.exists("Custom DocPerm", {"parent":"Account","role":"Stock User","select":1}):
        add_permission("Account", "Stock User", ptype="select")
    frappe.clear_cache()
    reference = frappe.get_doc("Stock Entry", fixture["reference"])
    warehouse = reference.items[0].t_warehouse
    reference.items[0].allow_zero_valuation_rate = 1
    reference.submit()
    shipping = "Finished Goods - NQ"
    frappe.get_doc(dict(doctype="Stock Entry", stock_entry_type="Material Transfer", company=reference.company,
        items=[dict(item_code=fixture["item"],qty=2,s_warehouse=warehouse,t_warehouse=shipping,
                    batch_no=fixture["batch"],use_serial_batch_fields=1)])).insert().submit()
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({"Delivery Note": [
        dict(fieldname="custom_norvale_operation", label="Norvale operation", fieldtype="Data",
             unique=1, no_copy=1, read_only=1),
        dict(fieldname="custom_norvale_quality_releases", label="Batch quality releases", fieldtype="Long Text",
             no_copy=1, read_only=1),
    ]})
    from frappe.core.doctype.user.user import generate_keys
    user_id = "feedback-warehouse@norvale.invalid"
    if not frappe.db.exists("User", user_id):
        frappe.get_doc(dict(doctype="User",email=user_id,first_name="Feedback Warehouse",send_welcome_email=0,
                            roles=[dict(role="Stock User")])).insert()
    pair = generate_keys(user_id)
    customer = frappe.get_doc(dict(doctype="Customer", customer_name="Feedback " + fixture["batch"],
                                   customer_group=frappe.db.get_value("Customer Group", {"is_group":0}),
                                   territory=frappe.db.get_value("Territory", {"is_group":0}))).insert()
    inspection = frappe.get_doc("Quality Inspection", fixture["release"])
    releases = [{"batch": fixture["batch"], "qty": 2, "disposition": {
        "id": inspection.name, "version": str(inspection.modified), "qty": inspection.custom_lot_quantity,
        "qms_source":"feedback-proof-qms", "status":"released"}}]
    note = frappe.get_doc(dict(doctype="Delivery Note",company=reference.company,customer=customer.name,
        custom_norvale_quality_releases=json.dumps(releases),
        items=[dict(item_code=fixture["item"],qty=2,rate=1,warehouse=shipping,batch_no=fixture["batch"],
                    use_serial_batch_fields=1)])).insert()
    note.submit()
    note.reload()
    from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice
    invoice = make_sales_invoice(note.name)
    invoice.insert().submit()
    frappe.db.commit()
    return {"delivery":note.name,"line":note.items[0].name,"customer":customer.name,
            "release_rows":json.loads(note.custom_norvale_quality_releases), "invoice":invoice.name,"company":note.company,"hold":warehouse,
            "warehouse_token":frappe.db.get_value("User", user_id, "api_key") + ":" + pair["api_secret"]}


if __name__ == "__main__":
    frappe.init(site="qms.localhost", sites_path=".")
    frappe.connect()
    frappe.set_user("Administrator")
    print(json.dumps(delivery(json.load(sys.stdin))))
