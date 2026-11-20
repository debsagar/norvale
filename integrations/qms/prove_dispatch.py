"""Prove the dispatch boundary against a dedicated native QMS site."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

from adapter import INSPECTION
from prove import denied, exercise


def main(runtime, output):
    output.mkdir(parents=True, exist_ok=True)
    provision = Path(__file__).with_name("provision.sh")
    first = (runtime / "fixture.json").read_text()
    subprocess.run([provision, "fixture", runtime], check=True)
    other = json.loads((runtime / "fixture.json").read_text())
    (runtime / "fixture.json").write_text(first)

    def before_revocation(fixture, clients, action, released):
        dispatcher = clients["dispatcher"]
        analyst = clients["analyst"]
        approver = clients["approver"]
        scope = {"batch": fixture["batch"], "item": fixture["item"], "inspection": released["name"],
                 "revision": released["custom_spec_revision"], "version": released["modified"],
                 "approver": released["modified_by"], "uom": released["custom_lot_uom"],
                 "qty": released["custom_lot_quantity"], "dispatch_qty": 2}
        other_scope = {**scope, "batch": other["batch"]}
        scopes = sorted([scope, other_scope], key=lambda row: row["batch"])

        def lease(label, scopes):
            return {"id": sha256((fixture["batch"] + label).encode()).hexdigest(),
                    "delivery": "synthetic-delivery-" + label, "scopes": scopes}

        def change(request, outcome="active"):
            return dispatcher.dispatch_lease(request, outcome=outcome)["state"]

        withdrawn = lease("pre-arm-withdrawal", [scope])
        assert change(withdrawn) == "active"
        assert change(withdrawn, "withdrawn") == change(withdrawn) == "withdrawn"
        request = lease("guarded", [scope])
        unauthorized = denied(
            lambda: approver.request("POST", "method/norvale_dispatch_lease",
                                     {"lease": request, "outcome": "active"}), "PermissionError")
        blocked = lease("multi-lot", scopes)
        assert change(blocked) == "denied"
        assert change(request) == "active"
        first_arm = dispatcher.dispatch_lease(request, outcome="armed")
        assert first_arm["state"] == "armed" and first_arm["submit_permitted"] is True
        assert dispatcher.dispatch_lease(request, outcome="armed")["submit_permitted"] is False
        denied(lambda: dispatcher.request("POST", "method/norvale_dispatch_lease",
                                          {"lease": request, "outcome": "aborted"}), "PermissionError")
        denied(lambda: dispatcher.request("POST", "method/norvale_dispatch_lease",
                                          {"lease": request, "outcome": "withdrawn"}), "ValidationError")
        assert change(lease("competing", [scope])) == "denied"

        def revoke():
            return approver.transition(released["name"], "Revoke Release")

        def add_sample(lot, label):
            source = clients["reader"].get("Stock Entry", lot["reference"])
            return analyst.request("POST", analyst.resource(INSPECTION), {
                "custom_qms_operation": lot["batch"] + ":" + label, "batch_no": lot["batch"],
                "item_code": lot["item"], "report_date": lot["date"], "inspection_type": "In Process",
                "reference_type": "Stock Entry", "reference_name": source["name"],
                "child_row_reference": source["items"][0]["name"],
                "inspected_by": released["inspected_by"], "sample_size": 1,
                "custom_spec_revision": scope["revision"], "custom_lot_quantity": 100,
                "custom_lot_uom": scope["uom"], "custom_decision_rationale": "Concurrent sample",
                "readings": [{"specification": lot["parameter"], "numeric": 1, "min_value": 5,
                              "max_value": 6, "reading_1": "5.5"}],
            })["data"]

        revocation = denied(revoke, "ValidationError")
        insert = denied(lambda: add_sample(fixture, "blocked"), "ValidationError")
        unrelated = add_sample(other, "independent")
        assert unrelated["batch_no"] == other["batch"]
        other_release = approver.transition(analyst.transition(unrelated["name"], "Record")["name"],
                                            "Release Lot")
        valid_other = {**other_scope, "inspection": other_release["name"],
                       "version": other_release["modified"], "approver": other_release["modified_by"]}
        multi = lease("both-released", sorted([scope, valid_other], key=lambda row: row["batch"]))
        assert change(multi) == "denied"  # One held lot blocks the entire request.
        independent = lease("independent", [valid_other])
        assert change(independent) == "active"
        assert change(independent, "armed") == "armed"
        assert change(independent, "submitted") == "submitted"

        subprocess.run([provision, "restart", runtime], check=True)
        for client in clients.values():
            client.url = (runtime / "url").read_text().strip()
        assert change(request, "status") == "armed"
        denied(revoke, "ValidationError")
        assert change(request, "submitted") == change(request, "submitted") == "submitted"
        both = lease("both-success", multi["scopes"])
        assert change(both) == "active"
        denied(revoke, "ValidationError")
        denied(lambda: approver.transition(other_release["name"], "Revoke Release"), "ValidationError")
        assert change(both, "armed") == "armed"
        assert change(both, "submitted") == "submitted"
        revoked = revoke()
        assert revoked["workflow_state"] == "Revoked"
        stale = lease("revocation-first", [scope])
        assert change(stale) == "denied"
        return {"unauthorized": unauthorized, "overlapping_revocation": revocation,
                "overlapping_sample": insert, "unrelated_inspection": unrelated["name"],
                "atomic_rejection": blocked["id"], "native_lease": request["id"],
                "multi_lot_lease": both["id"], "state_after_restart": "armed",
                "final_state": "submitted",
                "revocation_first": stale["id"], "erp_boundary": "simulated"}

    exercise(runtime, output, before_revocation=before_revocation)


if __name__ == "__main__":
    main(*(Path(arg).resolve() for arg in sys.argv[1:]))
