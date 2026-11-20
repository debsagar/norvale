"""Process shipment feedback against a running company's native records."""
import argparse
import fcntl
import json
from pathlib import Path

from integrations.qms.adapter import ACTION, CASE, QMS
from .complaint import close_action, contain, corrective_action, investigate
from .returns import authorize, receive
from .scope import authority, feedback_site, legacy_case, shipment, shipped_batch, verify_dispatch_version, verify_inspection


def process(engine, args, cfg):
    if engine.company_erp is None or engine.company_erp.authority is None:
        raise ValueError("The company needs a configured lot authority")
    requested = getattr(args, "qms_source", None)
    primary = cfg["qms_source"]
    erp_source = cfg["source"] + "-erp"
    command = args.command
    case = None
    if command in ("report", "receive-return"):
        note, row = shipment(engine, args.delivery, args.line)
    else:
        if not requested and cfg.get("feedback_sites"):
            raise ValueError("Select --qms-source for this native case or action ID")
        requested = requested or primary
        site = feedback_site(cfg, requested)
        client = QMS(**site["lab"]["approver"])
        case_id = client.get(ACTION, args.action)["custom_non_conformance"] if command == "close" else args.complaint
        case = client.get(CASE, case_id)
        if case["name"] != case_id:
            raise ValueError("Native complaint identity differs from the request")
        note, row = shipment(engine, case["custom_delivery_note"], case["custom_delivery_row"],
                             case=case, source=erp_source)

    batch = shipped_batch(engine.api["WAREHOUSE"], note, row)
    if command in ("report", "receive-return") and args.batch != batch:
        raise ValueError("Requested batch differs from posted evidence")
    site, scope, entry = authority(engine, cfg, note, row, batch, requested)
    source = scope["custom_delivery_qms_source"]
    reader = QMS(**site["qms_reader"])
    release = verify_inspection(reader, scope, row["item_code"])
    verify_dispatch_version(engine, reader, note, row, scope, entry, release)
    if command == "receive-return":
        case = reader.get(CASE, args.complaint)
        if (case["name"] != args.complaint or case.get("custom_delivery_source") != erp_source
                or case.get("custom_delivery_note") != note["name"]
                or case.get("custom_delivery_row") != row["name"]
                or case.get("custom_delivery_customer") != note["customer"]):
            raise ValueError("Observed receipt differs from the authorized shipment scope")
    if case is not None:
        legacy = legacy_case(case, scope)
        if case.get("custom_withdrawal_release") and case["custom_withdrawal_release"] != release["name"]:
            raise ValueError("Complaint withdrawal differs from the shipped release")
        if command in ("contain", "authorize-withdrawal") and args.release != release["name"]:
            raise ValueError("Requested inspection differs from the shipped release")

    if command == "report":
        analyst = QMS(**site["lab"]["analyst"])
        case = investigate(analyst, source=erp_source, note=note, row=row, scope=scope,
                           complaint=args.description, procedure=site["lab"]["procedure"],
                           operation=args.operation)
        action = corrective_action(analyst, case["name"], "capa:" + case["name"], args.description)
        outcome = {"complaint":case["name"], "action":action["name"], "delivery":args.delivery}
    elif command == "contain":
        revoked = contain(client, case, release, scope)
        outcome = {"complaint":args.complaint, "inspection":revoked["name"], "disposition":revoked["workflow_state"]}
        engine.company_erp.sync(engine.company_erp.state["lots"][batch])
        case_key = args.complaint if source == primary else json.dumps([source, args.complaint])
        for role, instruction in (("PLANNER", "Review outstanding allocations and due dates"),
                                  ("SALES", "Contact the customer to assess the shipped units")):
            # Keep the historical task payload stable for ordinary lots on retry.
            task = engine.write("containment:" + case_key + ":" + role, "ToDo", {
                "reference_type":"Delivery Note", "reference_name":note["name"],
                "allocated_to":role.lower() + "@norvale.test",
                "description":f"{instruction}. QMS case {args.complaint}, batch {batch}, "
                              f"revoked release {args.release}."
            }, role)
            if task["reference_name"] != note["name"]:
                raise ValueError("Containment task lost its native delivery reference")
            outcome[role.lower() + "_task"] = task["name"]
    elif command == "authorize-withdrawal":
        outcome = authorize(engine, client, case, note, row, scope, release, args.reason, legacy)
    elif command == "receive-return":
        outcome = receive(engine, args, case, note, row, release, scope, erp_source)
    else:
        action = close_action(client, args.action, args.inspection, args.assessment,
                              scope=scope, bind_case=case if legacy else None)
        outcome = {"action":action["name"], "status":action["status"], "inspection":args.inspection}
    outcome["qms_source"] = source
    engine.log("customer_feedback", operation=command, **outcome)
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report")
    for key in ("operation","delivery","line","batch","description"):
        report.add_argument("--" + key, required=True)
    hold = commands.add_parser("contain")
    hold.add_argument("--complaint", required=True)
    hold.add_argument("--release", required=True)
    withdrawal = commands.add_parser("authorize-withdrawal")
    for key in ("complaint", "release", "reason"):
        withdrawal.add_argument("--" + key, required=True)
    receipt = commands.add_parser("receive-return")
    for key in ("complaint", "delivery", "line", "batch", "quantity", "observation", "observed-on"):
        receipt.add_argument("--" + key, required=True)
    close = commands.add_parser("close")
    for key in ("action","inspection","assessment"):
        close.add_argument("--" + key, required=True)
    for command in (report, hold, withdrawal, receipt, close):
        command.add_argument("--qms-source", help="Native QMS source of the case or action")
    args = parser.parse_args(argv)
    config = args.run / "env/company-sources.json"
    if not config.is_file():
        parser.error("Expected an existing company run with configured software connections")
    from native.company import Engine
    with (args.run / "execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = process(Engine(args.run), args, json.loads(config.read_text()))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
