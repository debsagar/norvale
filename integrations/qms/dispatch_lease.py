"""Install durable lot ordering in the dedicated Frappe QMS site."""

LEASE = "Norvale Dispatch Lease"
ROLE = "Norvale QMS Dispatcher"
API = "norvale_dispatch_lease"

# All ordinary Quality Inspection mutations take the same Batch lock as acquire.
# Lock both scopes on a move. Before Validate covers saves and submissions.
GUARD = """
prior = doc.get_doc_before_save()
batches = set()
if doc.batch_no:
    batches.add(doc.batch_no)
if prior and prior.batch_no:
    batches.add(prior.batch_no)
for batch in sorted(batches):
    lot = frappe.db.get_value("Batch", batch, ["name", "custom_dispatch_lease"],
                              as_dict=True, for_update=True)
    if not lot or lot.custom_dispatch_lease:
        frappe.throw("Quality Inspection change blocked by dispatch ordering")
"""

# One request transaction covers the receipt and all Batch pointers. In particular,
# use current locking reads even if Frappe opened a read snapshot during login.
ENDPOINT = """
request = frappe.form_dict.get("lease")
outcome = frappe.form_dict.get("outcome")
commit = frappe.form_dict.get("commit")
actor = frappe.session.user
dispatcher = frappe.db.exists("Has Role", {"parenttype": "User", "parent": actor,
                                         "role": "Norvale QMS Dispatcher"})
if not ((dispatcher and outcome in ("active", "armed", "submitted", "status", "withdrawn")) or
        (actor == "Administrator" and outcome == "aborted")):
    frappe.throw("Dispatch lease permission denied", frappe.PermissionError)
scopes = request.get("scopes")
kind = request.get("kind", "dispatch")
transfer = request.get("transfer")
if not request.get("id") or not scopes:
    frappe.throw("QMS lease needs an ID and lot scopes")
if kind == "release_transfer":
    if (request.get("delivery") or not transfer or len(scopes) != 1 or
            not all(transfer.get(key) for key in
                    ("erp_source", "stock_entry", "row", "company", "source", "target", "posting_date")) or
            transfer["source"] == transfer["target"]):
        frappe.throw("Release lease needs one lot and exact ERP movement")
    if outcome == "submitted":
        if (not commit or commit.get("transfer") != transfer or
                commit.get("quantity") != scopes[0]["dispatch_qty"]):
            frappe.throw("Release lease requires matching ERP commit readback")
        legs = commit.get("legs", {})
        outward, inward = legs.get("Outward", {}), legs.get("Inward", {})
        if (set(legs) != {"Outward", "Inward"} or
                not all(leg.get("ledger") and leg.get("bundle") for leg in (outward, inward)) or
                outward["ledger"] == inward["ledger"] or outward["bundle"] == inward["bundle"]):
            frappe.throw("Release lease requires distinct ERP stock legs")
elif kind != "dispatch" or not request.get("delivery") or transfer or commit:
    frappe.throw("Dispatch lease needs a Delivery Note")
if commit and outcome != "submitted":
    frappe.throw("Commit evidence is only accepted on finalization")
batches = [scope["batch"] for scope in scopes]
if batches != sorted(set(batches)) or not all(batches):
    frappe.throw("Dispatch lease lot scopes must be unique and sorted")
pinned = json.dumps(request, sort_keys=True)
lease_id = request["id"]
lots = {}
for batch in batches:
    lot = frappe.db.get_value("Batch", batch, ["name", "item", "custom_dispatch_lease"],
                              as_dict=True, for_update=True)
    if not lot or lot.name != batch:
        frappe.throw("Unknown QMS lot")
    lots[batch] = lot
lease = frappe.db.get_value("Norvale Dispatch Lease", lease_id,
                           ["name", "request_json", "lease_state", "owner", "commit_json"],
                           for_update=True, as_dict=True)
submit_permitted = False
if lease:
    if lease.name != lease_id or lease.request_json != pinned or (
            dispatcher and lease.owner != actor and lease.lease_state != "aborted"):
        frappe.throw("Dispatch lease identity or scope mismatch")
    prior = lease.lease_state
    if kind == "release_transfer" and prior == "armed" and outcome == "aborted":
        frappe.throw("Armed release lease requires ERP commit readback")
    if commit and prior == "submitted" and lease.commit_json != json.dumps(commit, sort_keys=True):
        frappe.throw("ERP commit readback differs from the finalized receipt")
    if (outcome == "status" or
            (outcome == "active" and prior in ("armed", "denied", "aborted", "withdrawn")) or
            (outcome == "withdrawn" and prior in ("denied", "aborted"))):
        outcome = prior
    if prior in ("active", "armed"):
        for batch in batches:
            if lots[batch].custom_dispatch_lease != lease_id:
                frappe.throw("Dispatch lease lot pointer mismatch")
    if prior != outcome:
        if not ((prior == "active" and outcome in ("armed", "withdrawn", "aborted")) or
                (prior == "armed" and outcome in ("submitted", "aborted"))):
            frappe.throw("Invalid dispatch lease transition")
        if outcome != "armed":
            for batch in batches:
                frappe.db.set_value("Batch", batch, "custom_dispatch_lease", None, update_modified=False)
        receipt = frappe.get_doc("Norvale Dispatch Lease", lease_id, for_update=True)
        receipt.lease_state = outcome
        if commit:
            receipt.commit_json = json.dumps(commit, sort_keys=True)
        receipt.save(ignore_permissions=True)
        submit_permitted = outcome == "armed"
elif outcome in ("active", "aborted", "withdrawn"):
    result = outcome
    for scope in scopes:
        if result != "active":
            break
        batch = scope["batch"]
        lot = lots[batch]
        if lot.custom_dispatch_lease or lot.item != scope["item"]:
            result = "denied"
            break
        filters = {"batch_no": batch}
        sample = frappe.db.get_value("Quality Inspection", filters,
            ["name", "creation", "modified", "modified_by", "item_code", "docstatus", "status",
             "workflow_state", "report_date", "custom_spec_revision", "custom_lot_quantity", "custom_lot_uom",
             "custom_retest_of", "custom_rework_execution", "custom_rework_output_batch"],
            order_by="creation desc, name desc", as_dict=True, for_update=True)
        if not (sample and sample.name == scope["inspection"] and
                (kind != "release_transfer" or (sample.report_date and str(sample.report_date) <= transfer["posting_date"])) and
                str(sample.modified) == scope["version"] and sample.modified_by == scope["approver"] and
                sample.docstatus == 1 and sample.status == "Accepted" and sample.workflow_state == "Released" and
                sample.item_code == scope["item"] and sample.custom_spec_revision == scope["revision"] and
                sample.custom_lot_quantity == scope["qty"] and sample.custom_lot_uom == scope["uom"] and
                0 < scope["dispatch_qty"] <= scope["qty"]):
            result = "denied"
            break
        rework = any(scope.get(k) for k in ("output_batch", "retest_of", "rework_execution", "report_date"))
        if kind == "release_transfer" and (sample.custom_rework_output_batch or sample.custom_rework_execution):
            rework = True
        if rework and (kind != "release_transfer" or not all(scope.get(k) for k in
                ("output_batch", "retest_of", "rework_execution", "report_date")) or
                scope["output_batch"] == batch or str(sample.report_date) != scope["report_date"] or
                sample.custom_retest_of != scope["retest_of"] or
                sample.custom_rework_execution != scope["rework_execution"] or
                sample.custom_rework_output_batch != scope["output_batch"]):
            result = "denied"
            break
        filters["creation"] = sample.creation
        filters["name"] = ["!=", sample.name]
        if frappe.db.get_value("Quality Inspection", filters, "name", for_update=True):
            result = "denied"
            break
    outcome = result
    frappe.get_doc({"doctype": "Norvale Dispatch Lease", "name": lease_id,
                    "request_json": pinned, "lease_state": outcome}).insert(ignore_permissions=True, set_name=lease_id)
    if outcome == "active":
        for batch in batches:
            frappe.db.set_value("Batch", batch, "custom_dispatch_lease", lease_id, update_modified=False)
elif outcome == "status":
    outcome = "absent"
else:
    frappe.throw("Dispatch lease does not exist")
frappe.response["message"] = {"id": lease_id, "delivery": request.get("delivery"),
                              "transfer": transfer, "state": outcome,
                              "submit_permitted": submit_permitted}
"""


def install():
    import frappe
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    states = "active\narmed\nsubmitted\naborted\ndenied\nwithdrawn"
    if not frappe.db.exists("DocType", LEASE):
        frappe.get_doc({
            "doctype": "DocType", "name": LEASE, "module": "Quality Management", "custom": 1,
            "track_changes": 1, "allow_rename": 0,
            "fields": [
                {"fieldname": "request_json", "label": "Pinned Request", "fieldtype": "Long Text", "reqd": 1},
                {"fieldname": "commit_json", "label": "ERP Commit Readback", "fieldtype": "Long Text", "read_only": 1},
                {"fieldname": "lease_state", "label": "Lease State", "fieldtype": "Select",
                 "options": states, "reqd": 1},
            ],
            "permissions": [{"role": "System Manager", "read": 1}],
        }).insert()
    else:
        lease = frappe.get_doc("DocType", LEASE)
        state = next(field for field in lease.fields if field.fieldname == "lease_state")
        changed = state.options != states
        state.options = states
        if not any(field.fieldname == "commit_json" for field in lease.fields):
            lease.append("fields", {"fieldname": "commit_json", "label": "ERP Commit Readback",
                                    "fieldtype": "Long Text", "read_only": 1})
            changed = True
        if changed:
            lease.save()
    create_custom_fields({"Batch": [{"fieldname": "custom_dispatch_lease", "label": "Dispatch Lease",
                                     "fieldtype": "Data", "read_only": 1, "no_copy": 1, "hidden": 1}]})
    frappe.db.add_index("Quality Inspection", ["batch_no", "creation"], "norvale_dispatch_scope")

    def script(name, body, **fields):
        doc = (frappe.get_doc("Server Script", name) if frappe.db.exists("Server Script", name)
               else frappe.new_doc("Server Script"))
        doc.update(dict(name=name, disabled=0, script=body, **fields))
        doc.save()

    script("Norvale Dispatch Lease API", ENDPOINT, script_type="API", api_method=API, allow_guest=0)
    for event in ("Before Validate", "Before Save (Submitted Document)", "Before Cancel",
                  "Before Delete", "Before Rename"):
        script("Norvale Dispatch Inspection " + event, GUARD,
               script_type="DocType Event", reference_doctype="Quality Inspection", doctype_event=event)
    for event in ("Before Delete", "Before Rename"):
        script("Norvale Dispatch Batch " + event, """
if frappe.db.get_value("Batch", doc.name, "custom_dispatch_lease", for_update=True):
    frappe.throw("Batch has an active dispatch lease")
""", script_type="DocType Event", reference_doctype="Batch", doctype_event=event)
    script("Norvale Dispatch Batch Before Save", """
saved = frappe.db.get_value("Batch", doc.name, "custom_dispatch_lease", for_update=True)
if (doc.custom_dispatch_lease or "") != (saved or ""):
    frappe.throw("Dispatch lease pointer can only be changed by the dispatch API")
""", script_type="DocType Event", reference_doctype="Batch", doctype_event="Before Save")
