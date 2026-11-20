"""Qualify the recurring physical rework hook on fresh isolated native sites.

MES_TEST_URL, MES_TEST_SOURCE and the two passwords come from the MES fixture
runner. QMS runtime contains the existing private provisioner files.
"""

import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace
from xmlrpc.client import Fault

from integrations.company_run.authority import Authority
from integrations.mes.client import Mes
from integrations.qms.adapter import CASE, INSPECTION, APIError, QMS
from native.company import Engine, save
from native.company_erp import CompanyERP
from norvale.loader.erp_client import ErpClient


def prove(mes_fixture, qms_runtime, checkpoint):
    args = (os.environ["MES_TEST_URL"], "norvale_mes")
    identity = (os.environ["MES_TEST_PASSWORD"], os.environ["MES_TEST_SOURCE"])
    planner, operator = (Mes(*args, role, *identity) for role in ("planner", "operator"))
    try:
        Mes(*args, "rework", *identity)
    except PermissionError:
        pass
    else:
        raise AssertionError("Ordinary fixture password also authenticated as the rework bridge")
    fixture = json.loads((qms_runtime / "fixture.json").read_text())
    users = json.loads((qms_runtime / "users.json").read_text())
    url = (qms_runtime / "url").read_text().strip()
    analyst, approver, reader = (QMS(url, users[role]["api_key"], users[role]["api_secret"])
                                  for role in ("analyst", "approver", "reader"))
    settings = dict(shlex.split(line)[0].split("=", 1)
                    for line in (qms_runtime / "docker.env").read_text().splitlines())
    erp = ErpClient(url, "qms.localhost")
    erp.login("Administrator", settings["QMS_ADMIN_PASSWORD"])
    rev, total, salvage = "SHAMPOO-R1", 10, 7
    batch, item = fixture["batch"], fixture["item"]
    reference = reader.get("Stock Entry", fixture["reference"])
    company, hold = reference["company"], reference["items"][0]["t_warehouse"]
    fg = f"Rework Available {batch[-12:]} - NQ"
    erp.create("Warehouse", {"warehouse_name": fg.rsplit(" - ", 1)[0], "company": company})
    if not erp.list("Custom Field", {"dt": "Stock Entry", "fieldname": "custom_norvale_operation"}):
        erp.create("Custom Field", {"dt": "Stock Entry", "fieldname": "custom_norvale_operation",
                                  "label": "Norvale Operation", "fieldtype": "Data", "unique": 1})
    production_user = f"rework-production-{batch[-12:].lower()}@norvale.invalid"
    erp.create("User", {"email": production_user, "first_name": "Rework Production",
                        "send_welcome_email": 0, "roles": [{"role": "Manufacturing User"},
                                                          {"role": "Stock User"}]})
    secret = erp.call("frappe.core.doctype.user.user.generate_keys", {"user": production_user})["api_secret"]
    api_key = erp.get("User", production_user)["api_key"]
    stock_user = ErpClient(url, "qms.localhost", token=f"{api_key}:{secret}")
    original_batch = "ERP-" + batch
    erp.create("Batch", {"batch_id": original_batch, "item": item})
    material = erp.create("Stock Entry", {
        "company": company, "stock_entry_type": "Material Receipt",
        "items": [{"item_code": item, "qty": total, "basic_rate": 2, "batch_no": original_batch,
                   "use_serial_batch_fields": 1, "t_warehouse": hold}],
    })
    material = erp.update("Stock Entry", material["name"], {"docstatus": 1})

    planner.call("product.product", "write", [mes_fixture["product"]], {"default_code": item})
    bom = mes_fixture["boms"][0]
    ack = planner.acknowledge(bom)
    source = planner.dispatch("REWORK-LIVE-ORIGINAL", mes_fixture["product"], bom,
                              ack["revision"], total, mes_fixture["hold"])
    inputs = [{"lot_id": mes_fixture["bulk"][0], "quantity": 5},
              {"lot_id": mes_fixture["bottle"], "quantity": total}]
    released = [row["lot_id"] for row in inputs]
    operator.start(source["id"], inputs, released)
    original = operator.complete(source["id"], mes_fixture["output"][0], total, inputs, released)
    checkpoint.mkdir(mode=0o700)
    requests = [{"batch": original_batch, "mes_order": source["id"], "qms_batch": batch,
                 "inspection": ""}]
    def qms_settings(role):
        return {"url": url, "api_key": users[role]["api_key"], "api_secret": users[role]["api_secret"]}

    settings_path = checkpoint / "env/company-sources.json"
    mes_target = {"url": args[0], "db": args[1], "source_id": identity[1]}
    save(settings_path, {
        "mes": {**mes_target, "login": "operator", "password": identity[0]},
        "rework_mes": {**mes_target, "login": "rework", "password": os.environ["MES_REWORK_PASSWORD"]},
        "qms_reader": qms_settings("reader"), "qms_approver": users["approver"]["user"],
        "lab": {"analyst": qms_settings("analyst"), "analyst_user": users["analyst"]["user"]},
        "uoms": {"Nos": original["output"]["uom"]["native_id"]},
    })
    settings_path.chmod(0o600)
    authority = Authority(checkpoint)
    bridge = authority.rework.mes
    qms = authority.rework.quality

    def open_flow(reuse_authority=None):
        path = checkpoint / "state.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        roles = dict.fromkeys(("QA", "IT_ADMIN", "WAREHOUSE"), erp)
        roles["PRODUCTION"] = stock_user
        engine = SimpleNamespace(run=checkpoint, state=state, docs=state.setdefault("docs", {}),
                                 wh={"hold": hold, "fg": fg}, api=roles, admin=erp,
                                 company=company, day=0, date=lambda: fixture["date"],
                                 log=lambda *args, **kwargs: None)
        engine.persist = lambda: save(path, state)
        for method in ("get", "write", "recover_company_doc"):
            setattr(engine, method, getattr(Engine, method).__get__(engine))
        return CompanyERP(engine, reuse_authority or Authority(checkpoint))

    flow = open_flow(authority)
    flow.register(original_batch, item, total, "Nos", "finished", material,
                  completion_id=original["completion_ref"], completion_version=original["recorded_at_utc"],
                  mes_lot_id=f"{bridge.source_id}:stock.lot:{mes_fixture['output'][0]}",
                  revision={"inspection_spec": rev})
    flow.e.persist()
    fields = {
        "inspection_type": "In Process", "reference_type": "Stock Entry",
        "reference_name": reference["name"], "item_code": item, "batch_no": batch,
        "report_date": fixture["date"], "sample_size": 1, "inspected_by": users["analyst"]["user"],
        "custom_spec_revision": rev, "custom_lot_quantity": total, "custom_lot_uom": "Nos",
        "custom_rework_input_batch": original_batch, "custom_rework_salvage_quantity": salvage,
        "custom_decision_rationale": "Failed pH. Rework this exact held lot and discard three units.",
        "child_row_reference": reference["items"][0]["name"],
        "readings": [{"specification": fixture["parameter"], "numeric": 1,
                      "min_value": 5.0, "max_value": 6.0, "reading_1": "7.2"}],
    }
    failure = analyst.create(INSPECTION, batch + ":physical-failure", **fields)
    failure = analyst.transition(failure["name"], "Record")
    assert failure["status"] == "Rejected"
    requests[0]["inspection"] = failure["name"]
    save(checkpoint / "rework-requests.json", requests)
    case = analyst.create(CASE, batch + ":physical-case", subject="Failed lot requires physical rework",
                          procedure=fixture["procedure"], custom_qms_batch=batch,
                          custom_failed_inspection=failure["name"])
    flow.inspections()
    assert bridge.rework_record(source["id"]) is None
    assert flow.balance(flow.state["lots"][original_batch], hold, "QA") == total
    try:
        planner.execute_rework(source["id"], "Untrusted", total, salvage)
    except Fault as exc:
        assert "QMS rework bridge authority required" in exc.faultString
    else:
        raise AssertionError("An ordinary MES planner executed rework")
    try:
        analyst.transition(failure["name"], "Authorize Rework")
    except APIError as exc:
        assert exc.kind == "WorkflowTransitionError"
    else:
        raise AssertionError("QMS analyst authorized rework")
    approved = approver.transition(failure["name"], "Authorize Rework")
    assert approved["status"] == "Rejected"
    assert qms.authorization(batch, failure["name"], item, rev, total, "Nos", "other-batch", fixture["date"]) is None
    assert qms.authorization(batch, failure["name"], item, rev, total, "kg", original_batch, fixture["date"]) is None
    assert qms.authorization(batch, failure["name"], item, "WRONG-R", total, "Nos", original_batch, fixture["date"]) is None
    assert qms.authorization(batch, failure["name"], item, rev, total - 1, "Nos", original_batch, fixture["date"]) is None
    assert qms.authorization(batch, failure["name"], item, rev, total, "Nos", original_batch, "1970-01-01") is None

    execute = bridge.execute_rework

    def lost_reply(*args):
        execute(*args)
        raise TimeoutError("Injected lost Odoo success response")

    bridge.execute_rework = lost_reply
    flow.inspections()
    bridge.execute_rework = execute
    native = bridge.rework_record(source["id"])
    assert native and flow.balance(flow.state["lots"][original_batch], hold, "QA") == total

    class InterruptedErp(ErpClient):
        def update(self, doctype, name, changes):
            result = stock_user.update(doctype, name, changes)
            if doctype == "Stock Entry" and changes.get("docstatus") == 1:
                raise TimeoutError("Injected lost ERP submit response")
            return result

    flow = open_flow()
    # Use the live ERP session with a one-shot lost response after native submit.
    interrupted = InterruptedErp(url, "qms.localhost", token=stock_user.token)
    flow.e.api["PRODUCTION"] = interrupted
    flow.inspections()
    pending = next(iter(flow.state["rework"].values()))
    assert "receipt" not in pending and "retest" not in pending

    flow = open_flow()
    flow.inspections()
    key, operation = next(iter(flow.state["rework"].items()))
    output = flow.state["lots"][operation["output_batch"]]
    retest = reader.get(INSPECTION, operation["retest"])
    assert retest["docstatus"] == 0 and retest["workflow_state"] == "Draft"
    assert retest["status"] == "Pending" and not retest["readings"]
    assert retest["custom_retest_of"] == failure["name"]
    assert retest["custom_rework_execution"] == operation["execution"]
    assert retest["custom_rework_output_batch"] == output["batch"]
    assert retest["custom_lot_quantity"] == salvage
    for client, action in ((analyst, "Record"), (approver, "Release Lot")):
        try:
            client.transition(retest["name"], action)
        except APIError as exc:
            assert exc.kind == "WorkflowTransitionError"
        else:
            raise AssertionError(f"QMS allowed {action} on an unmeasured retest")
    assert reader.get(INSPECTION, failure["name"]) == approved
    assert flow.balance(flow.state["lots"][original_batch], hold, "QA") == 0
    assert flow.balance(output, hold, "QA") == salvage
    assert flow.balance(output, fg, "QA") == 0
    assert flow.available(output, "QA")[0] == 0
    assert erp.list("Stock Entry", {"custom_norvale_operation": hashlib.sha256(key.encode()).hexdigest()}) == [
        {"name": operation["receipt"]}]
    scrap = bridge.read("stock.scrap", native["norvale_rework_scrap"][0])
    assert scrap["scrap_qty"] == total - salvage
    assert bridge.execute_rework(source["id"], native["norvale_rework_authority"], total, salvage) == native["id"]
    try:
        bridge.execute_rework(source["id"], native["norvale_rework_authority"], total, salvage - 1)
    except Fault as exc:
        assert "Conflicting rework replay" in exc.faultString
    else:
        raise AssertionError("MES let a replay change the salvage quantity")
    flow.inspections()
    assert open_flow().state["rework"][key] == operation
    assert bridge.rework_record(source["id"]) == native

    # Declare a separate out-of-spec fixture measurement for the new sample.
    # The recurring hook above created no measurements of its own.
    analyst.request("PUT", analyst.resource(INSPECTION, retest["name"]), {
        "custom_decision_rationale": "Qualification sample remains outside the pH limits.",
        "readings": [{**fields["readings"][0], "reading_1": "7.1"}],
    })
    recorded = analyst.transition(retest["name"], "Record")
    assert recorded["docstatus"] == 1 and recorded["status"] == "Rejected"
    try:
        approver.transition(retest["name"], "Release Lot")
    except APIError as exc:
        assert exc.kind == "WorkflowTransitionError"
    else:
        raise AssertionError("QMS released a measured failed retest")
    flow.inspections()
    assert flow.balance(output, hold, "QA") == salvage and flow.available(output, "QA")[0] == 0
    assert reader.get(INSPECTION, failure["name"]) == approved
    print(json.dumps({"case": case["name"], "failure": failure["name"], "original_mo": source["id"],
                      "rework_mo": native["id"], "native_scrap": scrap["id"], "erp": operation["receipt"],
                      "retest": retest["name"], "initial": total, "held_output": salvage,
                      "scrapped": total - salvage, "uom": "Nos", "draft_retest_status": retest["status"],
                      "measured_retest_status": recorded["status"], "failed_retest_release_denied": True,
                      "missing_authority_denied": True, "role_denials": 2,
                      "separate_mes_credential_verified": True, "erp_production_role": True, "scope_denials": 5,
                      "recovered_response_losses": 2, "unmeasured_retest_transition_denials": 2,
                      "conflicting_native_replay_denied": True, "source_inspection_unchanged_after_authorization": True}))


if __name__ == "__main__":
    fixture, runtime, checkpoint = map(Path, sys.argv[1:])
    prove(json.loads(fixture.read_text()), runtime, checkpoint)
