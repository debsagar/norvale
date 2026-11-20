"""Exercise the receipt command and its durable writes on an isolated proof site."""
from collections import defaultdict
import json
from types import SimpleNamespace

from integrations.lake.boundary import Lake, crosslinks, ingest
from integrations.quality_feedback.__main__ import process
from integrations.qms.adapter import APIError, QMS
from native.company import Engine
from native.company_erp import CompanyERP
from norvale.loader.erp_client import ErpClient


def exercise_returns(runtime, fixture, shipment, complaint, released, clients):
    def config(client):
        key, secret = client.headers["Authorization"].removeprefix("token ").split(":")
        return dict(url=client.url, api_key=key, api_secret=secret)

    cfg = dict(source="feedback-proof", qms_source="feedback-proof-qms",
               lab={"approver":config(clients["approver"])}, qms_reader=config(clients["reader"]))
    warehouse = ErpClient(clients["reader"].url, "qms.localhost", shipment.pop("warehouse_token"))
    admin = ErpClient(warehouse.base, warehouse.site)
    password = (runtime / "docker.env").read_text().split("QMS_ADMIN_PASSWORD=")[1].splitlines()[0]
    admin.login("Administrator", password)
    checkpoint = runtime / ("receipt-" + fixture["batch"])

    def invoke(command, **fields):
        e = Engine.__new__(Engine)
        e.run, e.day, e.event = checkpoint, 1, "feedback-proof"
        e.date = lambda: fixture["date"]
        e.company, e.wh = shipment["company"], {"hold":shipment["hold"]}
        e.admin, e.api = admin, {"WAREHOUSE":warehouse}
        saved = checkpoint / "state.json"
        e.state = json.loads(saved.read_text()) if saved.exists() else {"docs":{}}
        e.docs = e.state["docs"]
        authority = SimpleNamespace(route=lambda *args, **kwargs: (cfg, None))
        e.company_erp = CompanyERP(e, authority)
        e.company_erp.state["lots"][fixture["batch"]] = {"item":fixture["item"]}
        e.company_erp.state["shipments"]["test-dispatch"] = {"target_id":shipment["delivery"],"departed_day":0,
                                                          "rows":shipment["release_rows"]}
        return process(e, SimpleNamespace(command=command, complaint=complaint["name"], **fields), cfg)

    def refused(call, expected):
        try:
            call()
        except ValueError as exc:
            assert expected in str(exc), str(exc)
        else:
            raise AssertionError("Expected receipt denial: " + expected)

    try:
        QMS(**config(clients["analyst"])).update("Non Conformance", complaint["name"],
            custom_withdrawal_release=released["name"], custom_withdrawal_reason="Unauthorized")
    except APIError as exc:
        assert exc.kind in ("ValidationError", "PermissionError"), str(exc)
    else:
        raise AssertionError("Analyst authorized a withdrawal")

    invoice = admin.get("Sales Invoice", shipment["invoice"])
    assert invoice["docstatus"] == 1 and not invoice["is_return"]
    delivery = shipment["delivery"]
    batch = fixture["batch"]
    receipt = dict(delivery=delivery,line=shipment["line"],batch=batch,quantity="1",
                   observed_on=fixture["date"],observation=batch + ":receipt-a")

    def receive(**changes):
        return invoke("receive-return", **{**receipt, **changes})

    refused(receive, "native withdrawal")
    assert not warehouse.list("Delivery Note", {"return_against":delivery})
    decision = dict(release=released["name"],reason="Collect affected shipped units")
    assert invoke("authorize-withdrawal", **decision) == invoke("authorize-withdrawal", **decision)
    assert not warehouse.list("Delivery Note", {"return_against":delivery})

    request = warehouse.request
    def lose_reply(method, path, body=None, timeout=300):
        response = request(method, path, body, timeout)
        if method == lost:
            raise TimeoutError("synthetic lost " + method + " response after native commit")
        return response

    for lost in ("POST", "PUT"):
        warehouse.request = lose_reply
        try:
            receive()
        except TimeoutError as exc:
            assert lost in str(exc)
        else:
            raise AssertionError("Receipt did not exercise ambiguous " + lost)
        finally:
            warehouse.request = request

    first = receive()
    assert first == receive()
    refused(lambda: receive(observation=batch + ":receipt-b", quantity="2"), "exceed")
    second = receive(observation=batch + ":receipt-b")
    refused(lambda: receive(observation=batch + ":receipt-c"), "exceed")
    returns = [warehouse.get("Delivery Note", row["return_note"]) for row in (first, second)]
    source = warehouse.get("Delivery Note", delivery)
    assert source["docstatus"] == 1 and not source["is_return"]
    assert admin.get("Sales Invoice", shipment["invoice"]) == invoice
    assert len(warehouse.list("Delivery Note", {"return_against":delivery})) == 2
    bundles = [warehouse.get("Serial and Batch Bundle", d["items"][0]["serial_and_batch_bundle"])
               for d in [source, *returns]]
    ledger = [warehouse.get("Stock Ledger Entry", r["name"]) for d in returns
              for r in warehouse.list("Stock Ledger Entry", {"voucher_no":d["name"],"is_cancelled":0})]
    assert len(ledger) == 2 and all(r["actual_qty"] == 1 and r["warehouse"] == shipment["hold"] for r in ledger)
    lake = checkpoint / "lake"
    for entity, documents in {"Delivery Note":[source,*returns], "Serial and Batch Bundle":bundles,
                              "Stock Ledger Entry":ledger}.items():
        assert ingest(lake, entity, lambda: documents, source=complaint["custom_delivery_source"]) == len(documents)
        assert ingest(lake, entity, lambda: documents, source=complaint["custom_delivery_source"]) == 0
    latest_case = clients["reader"].get("Non Conformance", complaint["name"])
    assert ingest(lake, "Non Conformance", lambda: [latest_case], source=cfg["qms_source"]) == 1
    revoked = clients["reader"].get("Quality Inspection", released["name"])
    assert ingest(lake, "Quality Inspection", lambda: [revoked], source=cfg["qms_source"]) == 1
    snapshots = defaultdict(lambda: defaultdict(list))
    for row in Lake(lake / "delta", {})._rows():
        snapshots[row["source"]][row["entity"]].append(json.loads(row["payload"]))
    links = crosslinks(snapshots)
    assert not links["unresolved"], links["unresolved"]
    assert len([edge for edge in links["edges"] if edge[1] == "return_case"]) == 2
    return {"receipts":[first, second],"ledger":snapshots[complaint["custom_delivery_source"]]["Stock Ledger Entry"],
            "invoice_unchanged":True,"source_submitted":True,"crosslinks":links["edges"],
            "lost_replies_recovered":["POST", "PUT"]}
