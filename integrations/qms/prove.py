"""Run synthetic QMS acceptance checks through role-scoped native REST."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from adapter import ACTION, CASE, INSPECTION, OP, APIError, QMS


def denied(call, *kinds):
    try:
        call()
    except APIError as exc:
        assert exc.kind in kinds, str(exc)
        return {"status": exc.status, "exception": exc.kind}
    raise AssertionError("Frappe unexpectedly allowed the action")


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def exercise(runtime, output, *, before_revocation=None):
    fixture = json.loads((runtime / "fixture.json").read_text())
    users = json.loads((runtime / "users.json").read_text())
    url = (runtime / "url").read_text().strip()
    clients = {role: QMS(url, pair["api_key"], pair["api_secret"]) for role, pair in users.items()}
    analyst, approver, reader = (clients[role] for role in ("analyst", "approver", "reader"))
    for role, client in clients.items():
        assert client.request("GET", "method/frappe.auth.get_logged_user")["message"] == users[role]["user"]

    batch = fixture["batch"]
    scope = (batch, "SHAMPOO-R1", 100, "Nos")
    assert not reader.release(*scope)
    source = reader.get("Stock Entry", fixture["reference"])
    assert source["docstatus"] == 0 and source["items"][0]["qty"] == scope[2]
    evidence = {"batch": batch, "before_inspection": "held"}

    def measure(label, value, failed=None):
        fields = {
            "inspection_type": "In Process", "reference_type": "Stock Entry",
            "reference_name": source["name"], "item_code": fixture["item"], "batch_no": batch,
            "report_date": fixture["date"], "sample_size": 1, "inspected_by": users["analyst"]["user"],
            "custom_spec_revision": scope[1], "custom_lot_quantity": scope[2],
            "custom_lot_uom": scope[3], "custom_decision_rationale":
                "Retest after authorized pH correction." if failed else "Initial pH outside specification.",
            "child_row_reference": source["items"][0]["name"],
            "readings": [{"specification": fixture["parameter"], "numeric": 1,
                          "min_value": 5.0, "max_value": 6.0, "reading_1": value}],
        }
        if failed:
            fields["custom_retest_of"] = failed
        operation = batch + ":" + label
        first = analyst.create(INSPECTION, operation, **fields)
        assert analyst.create(INSPECTION, operation, **fields)["name"] == first["name"]
        return analyst.transition(first["name"], "Record")

    failure = measure("initial", "7.2")
    assert failure["docstatus"] == 1 and failure["status"] == "Rejected" and not reader.release(*scope)
    case = analyst.create(CASE, batch + ":case", subject="Shampoo lot out of pH specification",
                          procedure=fixture["procedure"], details="Initial pH 7.2 against 5.0–6.0.",
                          custom_qms_batch=batch, custom_failed_inspection=failure["name"])
    action = analyst.create(ACTION, batch + ":action", corrective_preventive="Corrective",
                            procedure=fixture["procedure"], custom_qms_batch=batch,
                            custom_non_conformance=case["name"],
                            resolutions=[{"problem": "pH 7.2", "status": "Open"}])
    assert case["status"] == "Open" and action["status"] == "Open"
    evidence["failed_release_denial"] = denied(
        lambda: approver.transition(failure["name"], "Release Lot"), "WorkflowTransitionError")
    rework = approver.transition(failure["name"], "Authorize Rework")
    assert rework["status"] == "Rejected" and rework["modified_by"] == users["approver"]["user"]
    assert not reader.release(*scope)

    corrected = analyst.update(
        ACTION, action["name"],
        resolutions=[{"problem": "pH 7.2", "status": "Open",
                      "resolution": "Fixture rework completed. Monitor corrective-action effectiveness.",
                      "responsible": users["analyst"]["user"]}],
    )
    assert corrected["status"] == "Open" and "rework completed" in corrected["resolutions"][0]["resolution"]
    retest = measure("retest", "5.5", failure["name"])
    assert retest["status"] == "Accepted" and not reader.release(*scope)
    evidence["analyst_release_denial"] = denied(
        lambda: analyst.transition(retest["name"], "Release Lot"), "WorkflowTransitionError")
    evidence["analyst_direct_update_denial"] = denied(
        lambda: analyst.request("PUT", analyst.resource(INSPECTION, retest["name"]),
                                {"workflow_state": "Released"}),
        "WorkflowPermissionError", "PermissionError")
    assert not reader.release(*scope)
    released = approver.transition(retest["name"], "Release Lot")
    assert released["modified_by"] == users["approver"]["user"]
    assert reader.release(*scope)["name"] == retest["name"]
    assert not reader.release(batch, scope[1], 99, scope[3])
    assert not reader.release(batch, "SHAMPOO-R2", scope[2], scope[3])
    assert approver.transition(retest["name"], "Release Lot")["modified"] == released["modified"]
    evidence["released"] = {key: value for key, value in released.items() if key != OP}
    evidence["analyst_revocation_denial"] = denied(
        lambda: analyst.transition(retest["name"], "Revoke Release"), "WorkflowTransitionError")
    if before_revocation is not None:
        evidence["feedback"] = before_revocation(fixture, clients, action, released)
    revoked = approver.transition(retest["name"], "Revoke Release")
    assert revoked["modified_by"] == users["approver"]["user"] and not reader.release(*scope)
    assert approver.transition(retest["name"], "Revoke Release")["modified"] == revoked["modified"]

    exported = reader.export()
    assert exported[INSPECTION][failure["name"]]["status"] == "Rejected"
    assert exported[INSPECTION][retest["name"]]["custom_retest_of"] == failure["name"]
    assert exported[CASE][case["name"]]["custom_failed_inspection"] == failure["name"]
    assert exported[ACTION][action["name"]]["custom_non_conformance"] == case["name"]
    evidence.update({"scope": scope, "failed_inspection": failure["name"],
                     "rework": {key: value for key, value in rework.items() if key != OP},
                     "revoked": {key: value for key, value in revoked.items() if key != OP}, "non_conformance": case["name"],
                     "quality_action": action["name"]})
    write(output / "native-export.json", exported)
    before = hashlib.sha256((output / "native-export.json").read_bytes()).hexdigest()
    script = Path(__file__).with_name("provision.sh")
    subprocess.run([script, "restart", runtime], check=True)
    url = (runtime / "url").read_text().strip()
    reader = QMS(url, users["reader"]["api_key"], users["reader"]["api_secret"])
    assert reader.export() == exported and not reader.release(*scope)
    evidence["restart"] = {"native_export_sha256": before, "identical": True, "eligibility": "held"}
    evidence["installed_apps"] = subprocess.check_output(
        [script, "compose", runtime, "exec", "-T", "qms", "bench", "--site", "qms.localhost", "list-apps"],
        text=True).strip()
    write(output / "proof.json", evidence)
    print(f"QMS acceptance passed. Evidence at {output}")


if __name__ == "__main__":
    runtime, output = map(Path, sys.argv[1:])
    output.mkdir(exist_ok=True, parents=True)
    exercise(runtime.resolve(), output)
