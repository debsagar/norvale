"""Run the shipment-feedback path on the dedicated QMS proof site."""
import json
from pathlib import Path
import subprocess
import sys

from integrations.quality_feedback.complaint import close_action, contain, corrective_action, investigate
from integrations.quality_feedback.scope import shipped_batch, verify_inspection

qms_dir = Path(__file__).resolve().parents[1] / "qms"
sys.path.insert(0, str(qms_dir))
from adapter import APIError
from integrations.quality_feedback.prove_recovery import exercise_returns
from prove import exercise


def run(runtime, output):
    def feedback(fixture, clients, action, released):
        script = Path(__file__).with_name("fixture.py")
        command = [str(qms_dir / "provision.sh"), "compose", str(runtime), "exec", "-T", "qms"]
        result = subprocess.run(command + ["bash","-ec",
            'f=$(mktemp -p /data); trap \'rm -f "$f"\' EXIT; cat > "$f"; cd sites; '
            'echo "$1" | ../env/bin/python "$f"', "bash", json.dumps({**fixture, "release":released["name"]})],
            input=script.read_bytes(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace") or result.stdout.decode(errors="replace"))
        shipment = json.loads(result.stdout.splitlines()[-1])
        analyst, approver, reader = (clients[k] for k in ("analyst","approver","reader"))
        note = reader.get("Delivery Note", shipment["delivery"])
        row, = (r for r in note["items"] if r["name"] == shipment["line"])
        batch = shipped_batch(reader, note, row)
        scope = dict(custom_qms_batch=batch, custom_delivery_batch=batch,
                     custom_delivery_qms_source="feedback-proof-qms", custom_delivery_inspection=released["name"])
        pinned_release = verify_inspection(reader, scope, row["item_code"])
        args = dict(source="feedback-proof-erp", note=note, row=row, scope=scope,
                    complaint="Customer reported leaking closure on delivery",
                    procedure=fixture["procedure"],operation=fixture["batch"]+":complaint")
        complaint = investigate(analyst, **args)
        assert complaint["custom_delivery_customer"] == shipment["customer"]
        assert investigate(analyst, **args)["name"] == complaint["name"]
        followup = corrective_action(analyst, complaint["name"], args["operation"] + ":capa",
                                     "Investigate closure integrity")
        try:
            close_action(approver, followup["name"], released["name"], "Pre-complaint sample")
        except APIError:
            pass
        else:
            raise AssertionError("Evidence older than corrective action closed it")
        for client, sample in [(analyst,released["name"]), (approver,action["name"])]:
            try:
                close_action(client, action["name"], sample, "Repeat sample passed")
            except APIError:
                pass
            else:
                raise AssertionError("Invalid corrective closure succeeded")
        closed = close_action(approver, action["name"], released["name"], "Repeat sample passed")
        assert closed["modified_by"] == released["modified_by"]
        before = reader.get("Delivery Note", shipment["delivery"])
        assert contain(approver, complaint, pinned_release, scope)["workflow_state"] == "Revoked"
        assert before == reader.get("Delivery Note", shipment["delivery"])
        recovery = exercise_returns(runtime, fixture, shipment, complaint, released, clients)
        return {**shipment,"recovery":recovery,"complaint":complaint["name"],"action":closed["name"],"complaint_action":followup["name"],
                "effectiveness_inspection":closed["custom_effectiveness_inspection"],"shipped_qty":2}

    output.mkdir(parents=True,exist_ok=True)
    exercise(runtime, output, before_revocation=feedback)


if __name__ == "__main__":
    run(*[Path(arg).resolve() for arg in sys.argv[1:]])
