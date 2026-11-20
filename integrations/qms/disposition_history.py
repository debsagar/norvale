"""Commission disposition evidence only during full QMS site setup."""

from datetime import datetime


def confirmed_current(observations):
    """Check the last collected inspection against other versions from the same site and ID."""
    if not observations or observations[-1].get('source_absent'):
        return None
    current = observations[-1]
    disappeared = any(doc.get('source_absent') for doc in observations[:-1])
    if disappeared:
        last_absent = max(i for i, doc in enumerate(observations) if doc.get('source_absent'))
        earlier = [doc for doc in observations[:last_absent] if not doc.get('source_absent')]
        if not earlier or current in earlier:
            return None
    for previous in observations[:-1]:
        if previous == current or previous.get('source_absent'):
            continue
        try:
            if datetime.fromisoformat(previous['modified']) >= datetime.fromisoformat(current['modified']):
                return None
        except (KeyError, ValueError, TypeError):
            return None
    return current


ENTITY = "Norvale QMS Disposition"
REFERENCE = "Quality Inspection"
FIELDS = ("inspection", "inspection_version", "disposition", "item_code", "batch_no",
          "output_batch", "spec_revision", "lot_quantity", "lot_uom", "approver")
EVENT = "After Save (Submitted Document)"

# Frappe runs this after the inspection UPDATE and before the request commits.
# Read the saved row so the timestamp and the quantity match native REST readback.
RECORD = """
prior = doc.get_doc_before_save()
if doc.workflow_state == "Released" or (doc.workflow_state == "Revoked" and prior and prior.workflow_state == "Released"):
    saved = frappe.db.get_value("Quality Inspection", doc.name,
        ["modified", "modified_by", "workflow_state", "docstatus", "status", "item_code", "batch_no",
         "custom_rework_output_batch", "custom_spec_revision", "custom_lot_quantity", "custom_lot_uom"],
        as_dict=True)
    if not (saved and saved.docstatus == 1 and saved.status == "Accepted" and saved.item_code
            and saved.batch_no and saved.custom_spec_revision and saved.custom_lot_quantity > 0
            and saved.custom_lot_uom and saved.modified_by):
        frappe.throw("Disposition history requires a submitted accepted inspection with complete lot scope")
    frappe.get_doc({
        "doctype": "Norvale QMS Disposition", "inspection": doc.name,
        "inspection_version": str(saved.modified), "disposition": saved.workflow_state,
        "item_code": saved.item_code, "batch_no": saved.batch_no,
        "output_batch": saved.custom_rework_output_batch or saved.batch_no,
        "spec_revision": saved.custom_spec_revision, "lot_quantity": saved.custom_lot_quantity,
        "lot_uom": saved.custom_lot_uom, "approver": saved.modified_by,
    }).insert(ignore_permissions=True)
"""


def install(frappe, reader_role):
    if not frappe.db.exists("DocType", ENTITY):
        frappe.get_doc({
            "doctype": "DocType", "name": ENTITY, "module": "Quality Management",
            "custom": 1, "allow_rename": 0,
            "fields": [
                {"fieldname": field, "label": field.replace("_", " ").title(),
                 "fieldtype": "Float" if field == "lot_quantity" else "Data", "reqd": 1}
                for field in FIELDS
            ],
            "permissions": [{"role": reader_role, "read": 1}],
        }).insert()

    for title, entity, event, body in (
        ("Norvale Record Disposition History", REFERENCE, EVENT, RECORD),
        ("Norvale Preserve Disposition History", ENTITY, "Before Save",
         'if doc.get_doc_before_save(): frappe.throw("Disposition history is append-only")'),
        ("Norvale Retain Disposition History", ENTITY, "Before Delete",
         'frappe.throw("Disposition history is append-only")'),
    ):
        script = (frappe.get_doc("Server Script", title) if frappe.db.exists("Server Script", title)
                  else frappe.new_doc("Server Script"))
        script.update(dict(name=title, script_type="DocType Event", reference_doctype=entity,
                           doctype_event=event, disabled=0, script=body))
        script.save()
