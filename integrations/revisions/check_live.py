"""Qualify all three APIs against freshly provisioned, isolated local services."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from integrations.mes.client import Mes
from integrations.plm import Blocked, PLM
from norvale.loader.erp_client import ErpClient

from . import Bridge, RevisionMismatch
from .receivers import ERP_KEY, ERPNext, Odoo
from .test_bridge import EARLY, LATER, PRODUCT, components, mapping


def blocked(exception, call, message):
    try:
        call()
    except exception as error:
        assert message in str(error), error
        return str(error)
    raise AssertionError("Expected operation to be blocked")


def qualify(config, checkpoint):
    for endpoint in ("plm", "erp", "mes"):
        url = config[endpoint]["base" if endpoint == "erp" else "url"]
        assert urlsplit(url).hostname in ("localhost", "127.0.0.1"), "Use isolated loopback endpoints"
    assert not checkpoint.exists(), "Use a fresh checkpoint for qualification"
    plm = PLM(**config["plm"], token=os.environ["REVISIONS_PLM_TOKEN"])
    erp = ErpClient(**config["erp"], token=os.environ["REVISIONS_ERP_TOKEN"])
    mes = Mes(**config["mes"], password=os.environ["REVISIONS_MES_PASSWORD"])
    erp_receiver = ERPNext(erp)
    erp_receiver.provision(scoped=config["bridge"].get("scoped_receipts", False))

    company = "Revision Bridge Example"
    if not erp.list("Company", {"name": company}):
        erp.create("Company", {"company_name": company, "abbr": "RBE",
                               "country": "United States", "default_currency": "USD"})
    for code, unit in ((PRODUCT, "Nos"), ("NV-BRIDGE-BULK", "Kg"), ("NV-BRIDGE-BOTTLE", "Nos")):
        assert not erp.list("Item", {"name": code}), "Use a fresh ERP site"
        erp.create("Item", {"item_code": code, "item_name": code, "item_group": "All Item Groups",
                            "stock_uom": unit, "is_stock_item": 1})

    def product(code):
        rows = mes.call("product.product", "search_read", [["default_code", "=", code]],
                        fields=["uom_id", "company_id"], limit=2)
        assert len(rows) == 1, rows
        return rows[0]

    output, bulk, bottle = (product(code) for code in ("NV-SHAMPOO", "NV-BULK", "NV-BOTTLE"))
    center = config["example"]["workcenter_id"]
    hold = config["example"]["hold_location_id"]
    mapped = mapping(company, output["company_id"][0], output["id"], bulk["id"], bottle["id"],
                     bulk["uom_id"][0], output["uom_id"][0], center)

    def controller():
        return Bridge(plm, erp_receiver, Odoo(mes), source=config["bridge"]["source"],
                      scoped_receipts=config["bridge"].get("scoped_receipts", False),
                      mapping=mapped, checkpoint=checkpoint)

    a = plm.draft(PRODUCT, "A", components(1))
    plm.review(a, on=EARLY, evidence="NV-EXAMPLE-REVIEW-A")
    plm.approve(a, on=EARLY, effective=EARLY, evidence="NV-EXAMPLE-APPROVAL-A")
    denials = {"before_publish": blocked(Blocked, lambda: controller().release(a, "NV-WORK-A", on=EARLY),
                                        "awaiting erp acknowledgment")}

    # Simulate a lost response after an actual ERPNext commit.
    create = erp.create

    def lost_response(doctype, doc):
        create(doctype, doc)
        raise TimeoutError("qualification dropped ERPNext create reply after native commit")

    erp.create = lost_response
    try:
        denials["after_erp_commit"] = blocked(TimeoutError, lambda: controller().publish(a, on=EARLY),
                                             "after native commit")
    finally:
        erp.create = create
    draft, = erp.list("BOM", {"item": PRODUCT}, fields=["name", "docstatus"])
    assert draft["docstatus"] == 0
    first = controller().publish(a, on=EARLY)
    assert first["erp"]["bom_id"] == draft["name"]

    old = controller().release(a, "NV-WORK-A", on=EARLY)
    b = plm.draft(PRODUCT, "B", components(2), base_id=a)
    plm.review(b, on=EARLY, evidence="NV-EXAMPLE-REVIEW-B")
    plm.approve(b, on=EARLY, effective=LATER, evidence="NV-EXAMPLE-APPROVAL-B")
    denials["new_effective_awaits_ack"] = blocked(
        Blocked, lambda: controller().release(a, "NV-WORK-B", on=LATER), "awaiting erp acknowledgment")
    second = controller().publish(b, on=LATER)
    new = controller().release(a, "NV-WORK-B", on=LATER)
    assert (old["pin"]["native_id"], new["pin"]["native_id"]) == (a, b)
    assert first["mes"]["bom_id"] != second["mes"]["bom_id"]
    assert old == controller().release(a, "NV-WORK-A", on=EARLY)

    bom_id = first["mes"]["bom_id"]
    native = mes.call("mrp.bom", "read", [bom_id], fields=["bom_line_ids"])[0]
    lines = mes.call("mrp.bom.line", "read", native["bom_line_ids"],
                     fields=["product_id", "product_uom_id", "product_qty"])
    raw = next(line for line in lines if line["product_id"][0] == bulk["id"])
    assert raw["product_qty"] == 0.25
    erp_a = erp.get("BOM", first["erp"]["bom_id"])
    assert erp_a["docstatus"] == 1 and erp_a[ERP_KEY] == first["erp"]["import_key"]
    assert {(row["item_code"], row["uom"], row["qty"]) for row in erp_a["items"]} == {
        ("NV-BRIDGE-BULK", "Kg", 0.25), ("NV-BRIDGE-BOTTLE", "Nos", 1),
    }

    def dispatch():
        return controller().dispatch("NV-WORK-A", on=LATER, quantity="4", hold_location_id=hold)

    try:
        mes.call("mrp.bom.line", "write", [raw["id"]], {"product_qty": 0.5})
        assert mes.acknowledge(bom_id)["revision"] != first["mes"]["revision"]
        assert plm.publication(a)["source_version"] == old["pin"]["source_version"]
        denials["odoo_line_drift"] = blocked(RevisionMismatch, dispatch, "lines differ")
    finally:
        mes.call("mrp.bom.line", "write", [raw["id"]], {"product_qty": 0.25})
    work = dispatch()
    assert work["id"] == dispatch()["id"]
    assert (work["bom_id"][0], work["norvale_revision"]) == (bom_id, first["mes"]["revision"])
    moves = mes.call("stock.move", "read", work["move_raw_ids"],
                     fields=["product_id", "product_uom", "product_uom_qty"])
    assert {move["product_id"][0]: move["product_uom_qty"] for move in moves} == {
        bulk["id"]: 1, bottle["id"]: 4,
    }

    return {"publications": {"A": first, "B": second}, "pins": {"A": old["pin"], "B": new["pin"]},
            "denials": denials, "erp_a": {"bom_id": erp_a["name"], "docstatus": erp_a["docstatus"],
                                         "items": [{k: row[k] for k in ("item_code", "uom", "qty")}
                                                   for row in erp_a["items"]]},
            "odoo_a_lines": lines, "odoo_mo": {k: work[k] for k in (
                "id", "origin", "name", "bom_id", "norvale_revision", "product_qty", "state")},
            "odoo_mo_components": moves,
            "plm_receipts": [row["acknowledgments"] for row in plm.export(a, on=LATER)]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("checkpoint", type=Path)
    args = parser.parse_args()
    print(json.dumps(qualify(json.loads(args.config.read_text()), args.checkpoint), indent=2))
