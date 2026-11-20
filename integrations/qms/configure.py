"""Run inside the dedicated Bench as its installation Administrator."""

import contextlib
import json
from secrets import token_urlsafe
import sys

import frappe
from frappe.core.doctype.user.user import generate_keys
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.permissions import add_permission
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

from disposition_history import install as install_disposition_history
from dispatch_lease import ROLE as DISPATCHER, install as install_dispatch_lease

ANALYST = "Norvale QMS Analyst"
APPROVER = "Norvale QMS Approver"
READER = "Norvale QMS Reader"
INSPECTION = "Quality Inspection"
CASE = "Non Conformance"
ACTION = "Quality Action"
WORKFLOW = "Norvale Lot Disposition"


def ensure(doctype, name, **fields):
    if frappe.db.exists(doctype, name):
        return frappe.get_doc(doctype, name)
    return frappe.get_doc({"doctype": doctype, **fields}).insert()


def field(name, fieldtype, **options):
    return {"fieldname": "custom_" + name, "label": name.replace("_", " ").title(),
            "fieldtype": fieldtype, **options}


def guard(title, doctype, script):
    doc = (frappe.get_doc("Server Script", title) if frappe.db.exists("Server Script", title)
           else frappe.new_doc("Server Script"))
    doc.update(dict(name=title, script_type="DocType Event", reference_doctype=doctype,
                    doctype_event="Before Save", disabled=0, script=script))
    doc.save()


# Keep these bodies separate from installation so their native policies can be checked offline.
INVESTIGATION_GUARD = """
shipment = (doc.custom_delivery_source, doc.custom_delivery_note, doc.custom_delivery_row, doc.custom_delivery_customer)
binding = (doc.custom_delivery_batch, doc.custom_delivery_qms_source, doc.custom_delivery_inspection)
scoped = shipment[0] and shipment[1] and shipment[2] and shipment[3]
has_binding = binding[0] or binding[1] or binding[2]
prior = doc.get_doc_before_save()
reviewer = frappe.db.exists("Has Role", {"parenttype":"User", "parent":frappe.session.user, "role":"Norvale QMS Approver"})
if not (doc.custom_failed_inspection or scoped) or ((shipment[0] or shipment[1] or shipment[2] or shipment[3]) and not scoped):
    frappe.throw("Investigation needs a failed inspection or a complete customer shipment reference")
if prior:
    old_shipment = (prior.custom_delivery_source, prior.custom_delivery_note, prior.custom_delivery_row, prior.custom_delivery_customer)
    old_binding = (prior.custom_delivery_batch, prior.custom_delivery_qms_source, prior.custom_delivery_inspection)
    if scoped or prior.custom_delivery_note:
        if shipment != old_shipment or doc.custom_qms_batch != prior.custom_qms_batch:
            frappe.throw("Complaint shipment scope is final")
        if (old_binding[0] or old_binding[1] or old_binding[2]) and binding != old_binding:
            frappe.throw("Complaint shipment release binding is final")
if has_binding or (scoped and not prior):
    if not (scoped and binding[0] and binding[1] and binding[2]):
        frappe.throw("Complaint needs the shipped batch, QMS source and pinned inspection")
    if not prior or not (old_binding[0] or old_binding[1] or old_binding[2]):
        if prior and not reviewer:
            frappe.throw("Recovery of shipment release binding requires a QMS approver")
        pinned = frappe.db.get_value("Quality Inspection", doc.custom_delivery_inspection,
            ["batch_no", "custom_rework_output_batch", "docstatus", "status", "workflow_state"], as_dict=True)
        if not (pinned and pinned.docstatus == 1 and pinned.status == "Accepted"
                and pinned.workflow_state in ("Released", "Revoked")
                and pinned.batch_no == doc.custom_qms_batch
                and (pinned.custom_rework_output_batch or pinned.batch_no) == doc.custom_delivery_batch
                and (not doc.custom_withdrawal_release or doc.custom_withdrawal_release == doc.custom_delivery_inspection)):
            frappe.throw("Pinned inspection differs from the original QMS batch or shipped batch")
decision = (doc.custom_withdrawal_release, doc.custom_withdrawal_reason, doc.custom_withdrawal_approver)
if prior and prior.custom_withdrawal_approver:
    if decision != (prior.custom_withdrawal_release, prior.custom_withdrawal_reason, prior.custom_withdrawal_approver):
        frappe.throw("Authorized withdrawal is final")
elif decision[0] or decision[1] or decision[2]:
    sample = frappe.db.get_value("Quality Inspection", doc.custom_withdrawal_release,
        ["batch_no", "custom_rework_output_batch", "docstatus", "status", "workflow_state"], as_dict=True) if doc.custom_withdrawal_release else None
    if not (prior and reviewer and doc.status == "Open" and scoped and binding[0] and binding[1] and binding[2]
        and doc.custom_withdrawal_reason and doc.custom_withdrawal_release == doc.custom_delivery_inspection
        and sample and sample.docstatus == 1 and sample.status == "Accepted" and sample.workflow_state == "Revoked"
        and sample.batch_no == doc.custom_qms_batch
        and (sample.custom_rework_output_batch or sample.batch_no) == doc.custom_delivery_batch):
        frappe.throw("Withdrawal needs an approver, an open shipment complaint and its pinned revoked release")
    doc.custom_withdrawal_approver = frappe.session.user
"""


EFFECTIVENESS_GUARD = """
prior = doc.get_doc_before_save()
if prior and prior.status == "Completed":
    frappe.throw("Completed corrective action is final")
case_fields = ["custom_delivery_note", "custom_delivery_batch", "custom_delivery_qms_source",
               "custom_delivery_inspection", "custom_qms_batch", "procedure"]
case = frappe.db.get_value("Non Conformance", doc.custom_non_conformance, case_fields, as_dict=True)
previous_case = frappe.db.get_value("Non Conformance", prior.custom_non_conformance,
                                    "custom_delivery_note") if prior else None
if (case and case.custom_delivery_note) or previous_case:
    if not (case and case.custom_delivery_note and doc.custom_qms_batch == case.custom_qms_batch
            and doc.procedure == case.procedure and doc.corrective_preventive == "Corrective"):
        frappe.throw("Corrective action differs from its shipment complaint")
    if prior and (doc.custom_non_conformance != prior.custom_non_conformance
                  or [r.problem for r in doc.resolutions] != [r.problem for r in prior.resolutions]):
        frappe.throw("Corrective action intent is final")
if doc.status == "Completed":
    reviewer = frappe.db.exists("Has Role", {"parenttype":"User", "parent":frappe.session.user, "role":"Norvale QMS Approver"})
    sample = frappe.db.get_value("Quality Inspection", doc.custom_effectiveness_inspection,
        ["creation", "batch_no", "custom_rework_output_batch", "docstatus", "status", "workflow_state"], as_dict=True) if doc.custom_effectiveness_inspection else None
    if not (prior and reviewer and doc.custom_effectiveness_result and sample and sample.creation > prior.creation
            and sample.batch_no == doc.custom_qms_batch and sample.docstatus == 1
            and sample.status == "Accepted" and sample.workflow_state == "Released"):
        frappe.throw("Completion requires an approver and a subsequent released inspection for this lot")
    if case and case.custom_delivery_note and not (
            case.custom_delivery_qms_source and case.custom_delivery_inspection
            and doc.custom_effectiveness_inspection != case.custom_delivery_inspection
            and (sample.custom_rework_output_batch or sample.batch_no) == case.custom_delivery_batch):
        frappe.throw("Effectiveness inspection differs from the shipped lot")
"""


def configure_withdrawal():
    create_custom_fields({CASE: [
        field("delivery_batch", "Data", no_copy=1),
        field("delivery_qms_source", "Data", no_copy=1),
        field("delivery_inspection", "Link", options=INSPECTION, no_copy=1),
        field("withdrawal_release", "Link", options=INSPECTION, no_copy=1),
        field("withdrawal_reason", "Small Text", no_copy=1),
        field("withdrawal_approver", "Data", no_copy=1, read_only=1),
    ]})
    guard("Norvale Investigation Evidence", CASE, INVESTIGATION_GUARD)
    guard("Norvale Corrective Effectiveness", ACTION, EFFECTIVENESS_GUARD)
    perm_name = frappe.db.exists("Custom DocPerm", {"parent": CASE, "role": APPROVER})
    perm = frappe.get_doc("Custom DocPerm", perm_name or add_permission(CASE, APPROVER))
    perm.update({"read": 1, "write": 1})
    perm.save()
    frappe.clear_cache()


SAMPLE_WRITER = "Norvale QMS Sample Writer"
SAMPLE_USER = "qms-sample-writer@norvale.invalid"
SAMPLE_GUARDS = {
    "Item": """doc.is_stock_item and doc.has_batch_no and not doc.has_serial_no
                and not doc.disabled and not doc.has_variants and not doc.variant_of
                and doc.item_code == doc.item_name
                and doc.item_group == "Raw Material" and doc.stock_uom in ("Kg", "Nos")""",
    "Batch": """doc.batch_id and doc.item and not doc.disabled""",
    "Stock Entry": """doc.docstatus == 0 and doc.stock_entry_type == "Material Receipt"
                and doc.company == "Norvale QMS" and len(doc.items) == 1
                and doc.custom_norvale_sample_key and doc.custom_norvale_sample_scope
                and not doc.work_order and not doc.bom_no
                and doc.items[0].qty > 0 and doc.items[0].batch_no
                and not doc.items[0].s_warehouse
                and doc.items[0].t_warehouse == "QMS Quarantine - NQ" """,
}


def configure_sample_writer():
    settings = frappe.get_single("System Settings")
    if str(settings.float_precision) != "8":
        settings.float_precision = "8"
        settings.save()
    ensure("Role", SAMPLE_WRITER, role_name=SAMPLE_WRITER)
    create_custom_fields({"Stock Entry": [
        field("norvale_sample_key", "Data", unique=1, no_copy=1, set_only_once=1),
        field("norvale_sample_scope", "Small Text", no_copy=1, set_only_once=1),
    ]})
    for doctype in (*SAMPLE_GUARDS, "Quality Inspection Parameter"):
        condition = SAMPLE_GUARDS.get(doctype)
        perm_name = frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": SAMPLE_WRITER})
        perm = frappe.get_doc("Custom DocPerm", perm_name or add_permission(doctype, SAMPLE_WRITER))
        perm.update(dict(read=1, create=int(condition is not None), write=0, submit=0,
                         cancel=0, delete=0, amend=0, share=0))
        perm.save()
        if not condition:
            continue
        guard("Norvale Sample Writer " + doctype, doctype, f"""
writer_role = frappe.db.exists("Has Role", {{"parenttype": "User", "parent": frappe.session.user,
                                             "role": {SAMPLE_WRITER!r}}})
if writer_role or frappe.session.user == {SAMPLE_USER!r}:
    if frappe.session.user != {SAMPLE_USER!r} or doc.owner != frappe.session.user or doc.get_doc_before_save() or not ({condition}):
        frappe.throw("Outside the Norvale sample writer scope")
""")
    ensure("Quality Inspection Parameter", "Norvale Incoming Quality Index",
           parameter="Norvale Incoming Quality Index")
    markers = frappe.get_all("Quality Inspection Parameter",
                             filters={"name": ["like", "Norvale Sample Site %"]}, pluck="name", limit=2)
    if len(markers) > 1:
        raise RuntimeError("Ambiguous primary QMS sample site identity")
    marker = markers[0] if markers else "Norvale Sample Site " + token_urlsafe(24)
    ensure("Quality Inspection Parameter", marker, parameter=marker)
    user = ensure("User", SAMPLE_USER, email=SAMPLE_USER, first_name="QMS Sample Writer",
                  send_welcome_email=0, user_type="System User", roles=[{"role": SAMPLE_WRITER}])
    if {row.role for row in user.roles} != {SAMPLE_WRITER} or not user.enabled:
        raise RuntimeError("Primary QMS sample writer has unexpected roles or is disabled")
    keys = ({"api_key": user.api_key, "api_secret": user.get_password("api_secret")}
            if user.api_key else generate_keys(SAMPLE_USER))
    return {"user": SAMPLE_USER, "instance": marker, **keys}


def configure():
    result = setup_complete({
        "language": "English", "full_name": "Norvale QMS Site Administrator",
        "email": "qms-site-admin@norvale.invalid", "password": token_urlsafe(32),
        "chart_of_accounts": "Standard", "setup_demo": 0, "company_name": "Norvale QMS",
        "company_abbr": "NQ", "country": "Netherlands", "currency": "EUR", "timezone": "UTC",
        "fy_start_date": frappe.utils.getdate().replace(month=1, day=1).isoformat(),
        "fy_end_date": frappe.utils.getdate().replace(month=12, day=31).isoformat(),
    })
    if result != {"status": "ok"}:
        raise RuntimeError("ERPNext setup wizard did not complete")
    for role in (ANALYST, APPROVER, READER):
        ensure("Role", role, role_name=role)

    operation = field("qms_operation", "Data", reqd=1, unique=1, no_copy=1, set_only_once=1)
    batch = field("qms_batch", "Link", options="Batch", reqd=1)
    create_custom_fields({
        INSPECTION: [operation, field("spec_revision", "Data", reqd=1),
                     field("lot_quantity", "Float", reqd=1, non_negative=1),
                     field("lot_uom", "Link", options="UOM", reqd=1),
                     field("retest_of", "Link", options=INSPECTION),
                     field("rework_input_batch", "Data"),
                     field("rework_salvage_quantity", "Float", non_negative=1),
                     field("rework_execution", "Data"),
                     field("rework_output_batch", "Data"),
                     field("decision_rationale", "Small Text")],
        CASE: [operation, batch, field("failed_inspection", "Link", options=INSPECTION),
              field("delivery_source", "Data"), field("delivery_note", "Data"),
              field("delivery_row", "Data"), field("delivery_customer", "Data")],
        ACTION: [operation, batch, field("non_conformance", "Link", options=CASE, reqd=1),
                 field("effectiveness_inspection", "Link", options=INSPECTION),
                 field("effectiveness_result", "Small Text")],
    })

    make_property_setter(INSPECTION, "status", "options", "Accepted\nRejected\nPending", "Text")

    configure_withdrawal()
    for doctype in (INSPECTION, CASE, ACTION, "Batch", "Item", "Stock Entry",
                    "Quality Procedure", "Quality Inspection Parameter", "UOM"):
        for role in (ANALYST, APPROVER, READER):
            permissions = {"read": 1}
            if role == ANALYST and doctype in (INSPECTION, CASE, ACTION):
                permissions.update(write=1, create=1)
                if doctype == INSPECTION:
                    permissions["submit"] = 1
            if role == APPROVER and doctype in (ACTION, CASE):
                permissions["write"] = 1
            if role == APPROVER and doctype == INSPECTION:
                # Frappe requires submit permission for updates to submitted documents.
                permissions.update(write=1, submit=1)
            perm_name = frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": role})
            perm = frappe.get_doc("Custom DocPerm", perm_name or add_permission(doctype, role))
            if any(perm.get(key) != value for key, value in permissions.items()):
                perm.update(permissions)
                perm.save()
    frappe.clear_cache()

    state_roles = {"Draft": ANALYST, "Recorded": APPROVER, "Rework Authorized": APPROVER,
                   "Lot Rejected": APPROVER, "Released": APPROVER, "Revoked": APPROVER}
    for state in state_roles:
        ensure("Workflow State", state, workflow_state_name=state)

    scope = ("doc.batch_no and doc.custom_spec_revision and doc.custom_lot_quantity > 0 "
             "and doc.custom_lot_uom and doc.custom_decision_rationale")
    same_scope = ('"batch_no": doc.batch_no, "custom_spec_revision": doc.custom_spec_revision, '
                  '"custom_lot_quantity": doc.custom_lot_quantity, "custom_lot_uom": doc.custom_lot_uom')
    latest = ('frappe.db.get_value("Quality Inspection", {"batch_no": doc.batch_no}, '
              '"name", order_by="creation desc, name desc") == doc.name')
    latest_failure = (
        'frappe.db.get_value("Quality Inspection", {"batch_no": doc.batch_no, "docstatus": 1, '
        '"status": "Rejected"}, "name", order_by="creation desc, name desc")'
    )
    physical_previous = (
        f'doc.custom_retest_of == {latest_failure} and doc.custom_rework_execution '
        'and doc.custom_rework_output_batch and doc.custom_lot_quantity > 0 and '
        'frappe.db.get_value("Quality Inspection", {"name": doc.custom_retest_of, "docstatus": 1, '
        '"status": "Rejected", "workflow_state": "Rework Authorized", "batch_no": doc.batch_no, '
        '"item_code": doc.item_code, "custom_spec_revision": doc.custom_spec_revision, '
        '"custom_rework_salvage_quantity": doc.custom_lot_quantity, '
        '"custom_lot_uom": doc.custom_lot_uom}, "name")'
    )
    previous = (
        f'doc.custom_retest_of == {latest_failure} and '
        'frappe.db.get_value("Quality Inspection", {"name": doc.custom_retest_of, "docstatus": 1, '
        '"status": "Rejected", "workflow_state": "Rework Authorized", ' + same_scope + ', "custom_rework_salvage_quantity": 0}, "name")'
    )
    evidence = 'doc.status == "Accepted" and not doc.manual_inspection and doc.readings'
    for flag, invalid in (("manual_inspection", 1), ("formula_based_criteria", 1), ("numeric", 0)):
        evidence += (
            ' and not frappe.db.get_value("Quality Inspection Reading", '
            f'{{"parent": doc.name, "parenttype": "Quality Inspection", "{flag}": {invalid}}}, "name")'
        )
    failure = (
        'doc.status == "Rejected" and frappe.db.get_value("Non Conformance", '
        '{"custom_qms_batch": doc.batch_no, "custom_failed_inspection": doc.name}, "name")'
    )
    transitions = [
        ("Draft", "Record", "Recorded", ANALYST,
         'doc.status in ("Accepted", "Rejected") and doc.readings and ' + scope),
        ("Recorded", "Authorize Rework", "Rework Authorized", APPROVER, scope + " and " + failure
         + " and (not doc.custom_rework_salvage_quantity or "
         "(doc.custom_rework_input_batch and doc.custom_rework_salvage_quantity <= doc.custom_lot_quantity))"),
        ("Recorded", "Reject Lot", "Lot Rejected", APPROVER, scope + " and " + failure),
        ("Recorded", "Release Lot", "Released", APPROVER,
         f"{scope} and {latest} and {evidence} and ((doc.custom_retest_of and ({previous} or ({physical_previous}))) "
         f"or (not doc.custom_retest_of and not {latest_failure}))"),
        ("Released", "Revoke Release", "Revoked", APPROVER, ""),
    ]
    for _, action, _, _, _ in transitions:
        ensure("Workflow Action Master", action, workflow_action_name=action)
    workflow = (frappe.get_doc("Workflow", WORKFLOW) if frappe.db.exists("Workflow", WORKFLOW)
                else frappe.new_doc("Workflow"))
    workflow.update({
        "workflow_name": WORKFLOW, "document_type": INSPECTION, "workflow_state_field": "workflow_state",
        "is_active": 1, "send_email_alert": 0,
        "states": [{"state": state, "allow_edit": role, "doc_status": "0" if state == "Draft" else "1"}
                   for state, role in state_roles.items()],
        "transitions": [{"state": source, "action": action, "next_state": target, "allowed": role,
                         "condition": condition, "allow_self_approval": role == ANALYST}
                        for source, action, target, role, condition in transitions],
    })
    workflow.save()
    frappe.clear_cache(doctype=INSPECTION)

    install_disposition_history(frappe, READER)
    users = {"dispatcher": configure_dispatcher()}
    for alias, role in (("analyst", ANALYST), ("approver", APPROVER), ("reader", READER)):
        email = f"qms-{alias}@norvale.invalid"
        user = ensure("User", email, email=email, first_name=f"QMS {alias.title()}",
                      send_welcome_email=0, user_type="System User", roles=[{"role": role}])
        keys = ({"api_key": user.api_key, "api_secret": user.get_password("api_secret")}
                if user.api_key else generate_keys(email))
        users[alias] = {"user": email, **keys}
    frappe.db.commit()
    frappe.clear_cache()
    return users


def configure_dispatcher():
    ensure("Role", DISPATCHER, role_name=DISPATCHER)
    install_dispatch_lease()
    if not frappe.db.exists("Custom DocPerm", {"parent": "Norvale Dispatch Lease", "role": READER, "read": 1}):
        add_permission("Norvale Dispatch Lease", READER, ptype="read")
    email = "qms-dispatcher@norvale.invalid"
    user = ensure("User", email, email=email, first_name="QMS Dispatcher",
                  send_welcome_email=0, user_type="System User", roles=[{"role": DISPATCHER}])
    keys = ({"api_key": user.api_key, "api_secret": user.get_password("api_secret")}
            if user.api_key else generate_keys(email))
    return {"user": email, **keys}


if __name__ == "__main__":
    frappe.init(site="qms.localhost", sites_path=".")
    frappe.connect()
    frappe.set_user("Administrator")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if sys.argv[1:] == ["lease-only"]:
                users = {"dispatcher": configure_dispatcher()}
                frappe.db.commit()
                frappe.clear_cache()
            elif sys.argv[1:] == ["sample-writer-only"]:
                users = {"sample_writer": configure_sample_writer()}
                frappe.db.commit()
                frappe.clear_cache()
            elif sys.argv[1:] == ["withdrawal-only"]:
                configure_withdrawal()
                frappe.db.commit()
                users = {}
            else:
                users = configure()
        print(json.dumps(users))
    finally:
        frappe.destroy()
