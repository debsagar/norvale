"""Exercise the adapter against the pinned Odoo service, including a lost completion reply."""

import json
import os
from pathlib import Path
import sys
from xmlrpc.client import Fault

from . import Mes
from .client import MO, WO


def must_fail(action, expected):
    try:
        action()
    except Fault as error:
        if not any(fragment in error.faultString for fragment in
                   ((expected,) if isinstance(expected, str) else expected)):
            raise
    else:
        raise AssertionError(f"Expected {expected}")


def input_lots(*pairs):
    return [{"lot_id": lot, "quantity": amount} for lot, amount in pairs]


def run(phase, fixture_path, checkpoint_path):
    fixture = json.loads(Path(fixture_path).read_text())
    checkpoint = Path(checkpoint_path)
    url = os.environ["MES_TEST_URL"]
    password = os.environ["MES_TEST_PASSWORD"]
    deployment = os.environ["MES_TEST_SOURCE"]
    planner = Mes(url, "norvale_mes", "planner", password, deployment)
    operator = Mes(url, "norvale_mes", "operator", password, deployment)
    center_a, center_b = fixture["centers"]
    bom_a, bom_b = fixture["boms"]
    bulk_a, bulk_b = fixture["bulk"]
    bottle = fixture["bottle"]
    out_a, out_b, out_c = fixture["output"]
    product, hold = fixture["product"], fixture["hold"]
    released = [bulk_a, bulk_b, bottle]
    all_a = input_lots((bulk_a, 3), (bulk_b, 1), (bottle, 8))
    actual_a = input_lots((bulk_a, 1.5), (bulk_b, 0.25), (bottle, 4))
    actual_b = input_lots((bulk_b, 1), (bottle, 2))

    def quants(lot, location):
        return planner.call("stock.quant", "search_read",
                            [["lot_id", "=", lot], ["location_id", "=", location]],
                            fields=["quantity", "reserved_quantity"])

    def total(lot, location, field="quantity"):
        return sum(row[field] for row in quants(lot, location))

    staging_ref = "NV-STAGE-qualification"
    stock = fixture["stock"]

    def stage(client, staging_lot, production, quantity=2):
        return client.call("stock.lot", "norvale_stage_input", [staging_lot],
                           staging_ref, stock, quantity, production)

    def scan_staging_receipt(production, lot):
        from integrations.company_run.lake import scan
        from integrations.lake.adapter import Lake

        reader = dict(url=url, db="norvale_mes", login="analytics",
                      password=os.environ["MES_ANALYTICS_PASSWORD"], source_id=deployment)
        cfg = dict(reader=reader, instance=fixture["analytics_instance"],
                   reader_company=fixture["analytics_company"], product=product,
                   company=fixture["analytics_company"])
        archive = checkpoint.with_name("analytics")
        scan("mes", cfg, archive)
        lake = Lake(archive / "delta", {"audit": ["stock.move"]})
        stored, = lake.read("audit", "stock.move")
        receipt = json.loads(stored["payload"])
        assert stored["source"] == deployment and receipt["receipt"] == staging_ref
        assert (receipt["production_id"], receipt["lot_id"], receipt["quantity"]) == (production, lot, 2)
        assert receipt["erp_work_ref"] == "ERP-WO-A" and receipt["destination_location_id"] == stock
        version = lake.history()[0]["version"]
        scan("mes", cfg, archive)
        assert lake.history()[0]["version"] == version
        return receipt

    if phase == "before":
        observer = Mes(url, "norvale_mes", "observer", password, deployment)
        rev_a = planner.acknowledge(bom_a)["revision"]
        rev_b = planner.acknowledge(bom_b)["revision"]
        must_fail(lambda: planner.dispatch("ERP-WO-A", product, bom_a, rev_b, 8, hold),
                  ("acknowledged revision differs", "Conflicting work dispatch replay"))
        a = planner.dispatch("ERP-WO-A", product, bom_a, rev_a, 8, hold)["id"]
        assert planner.dispatch("ERP-WO-A", product, bom_a, rev_a, 8, hold)["id"] == a
        must_fail(lambda: planner.dispatch("ERP-WO-A", product, bom_a, rev_a, 7, hold),
                  "Conflicting work dispatch replay")
        b = planner.dispatch("ERP-WO-B", product, bom_b, rev_b, 2, hold)["id"]
        must_fail(lambda: observer.read(MO, a), "not allowed to access")
        must_fail(lambda: observer.start(a, all_a, released), "Manufacturing authority required")
        must_fail(lambda: operator.fault(center_a, "GITEA-27"), "Manufacturing authority required")
        must_fail(lambda: operator.complete(a, out_a, 3, actual_a, released), "requires running work")
        must_fail(lambda: operator.start(a, all_a, [bulk_b, bottle]), "held or lacks release")
        must_fail(lambda: operator.start(a, input_lots((bulk_a, 6), (bottle, 8)), released),
                  "Insufficient available quantity")

        line = planner.call("mrp.bom.line", "search", [["bom_id", "=", bom_a],
                                                       ["product_id.tracking", "=", "lot"]], limit=1)[0]
        old = planner.call("mrp.bom.line", "read", [line], fields=["product_qty"])[0]["product_qty"]
        planner.call("mrp.bom.line", "write", [line], {"product_qty": old * 2})
        must_fail(lambda: operator.start(a, all_a, released), "Pinned revision")
        planner.call("mrp.bom.line", "write", [line], {"product_qty": old})

        fault = planner.fault(center_a, "GITEA-27")
        assert not operator.availability(center_a)["available"]
        assert operator.availability(center_b)["available"]
        must_fail(lambda: operator.start(a, all_a, released), "allocated equipment is unavailable")
        active_b = operator.start(b, actual_b, released)
        assert operator.start(b, actual_b, released)["id"] == active_b["id"]
        assert not operator.availability(center_b)["available"]
        report_b = operator.complete(b, out_b, 2, actual_b, released)
        assert report_b["backorder"] is None and report_b["output"]["quantity"] == 2
        assert total(out_b, hold) == 2 and not total(out_b, fixture["stock"])

        must_fail(lambda: planner.restart(center_b, fault["native_id"], "RESTART-27"), "matching MES")
        planner.restart(center_a, fault["native_id"], "RESTART-27")
        assert planner.fault(center_a, "GITEA-27")["native_id"] == fault["native_id"]
        assert operator.availability(center_a)["available"]
        operator.start(a, all_a, released)
        assert total(bulk_a, fixture["stock"], "reserved_quantity") == 3
        assert total(bulk_b, fixture["stock"], "reserved_quantity") == 1
        must_fail(lambda: operator.complete(a, out_a, 3, actual_a, [bulk_b, bottle]),
                  "held or lacks release")
        assert total(out_a, hold) == 0

        # The native transaction commits. The controller saves intent only
        # and discards the response, then exits before fetching the report.
        bottle_lot = planner.read("stock.lot", bottle)
        staging_lot = planner.call("stock.lot", "create", {
            "name": "BOTTLE-STAGING-PROOF", "product_id": bottle_lot["product_id"][0],
            "company_id": bottle_lot["company_id"][0],
        })
        assert total(staging_lot, stock) == 0
        must_fail(lambda: stage(operator, staging_lot, a), "Inventory manager required")
        if "analytics_instance" in fixture:
            analytics = Mes(url, "norvale_mes", "analytics", os.environ["MES_ANALYTICS_PASSWORD"], deployment)
            must_fail(lambda: stage(analytics, staging_lot, a), "Inventory manager required")
            must_fail(lambda: analytics.call("stock.lot", "write", [staging_lot], {"name": "unauthorized"}),
                      "not allowed to modify")
            assert planner.read("stock.lot", staging_lot)["name"] == "BOTTLE-STAGING-PROOF"
        checkpoint.write_text(json.dumps({"a": a, "b": b, "revision": rev_a, "fault": fault["native_id"],
                                          "staging_lot": staging_lot}))
        stage(planner, staging_lot, a)  # Discard the committed staging reply as well.
        operator.call(MO, "norvale_complete", [a], out_a, 3, actual_a, released)
        if "analytics_instance" in fixture:
            receipt = scan_staging_receipt(a, staging_lot)
            checkpoint.with_name("staging-observed.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print("before restart: native denial, stale revision, held/exhausted lot, equipment isolation, "
              "actual reservations and committed partial completion with discarded response")
    elif phase == "after":
        saved = json.loads(checkpoint.read_text())
        a, b, rev_a = saved["a"], saved["b"], saved["revision"]
        staging_lot = saved["staging_lot"]
        def receipt():
            move, = planner.call("stock.move", "search_read",
                                 [["norvale_staging_receipt", "=", staging_ref]],
                                 fields=["state", "move_line_ids", "norvale_staging_order_id"])
            line_id, = move["move_line_ids"]
            line = planner.read("stock.move.line", line_id)
            assert move["state"] == "done" and move["norvale_staging_order_id"][0] == a
            assert line["lot_id"][0] == staging_lot and line["quantity"] == 2
            assert line["location_dest_id"][0] == stock and total(staging_lot, stock) == 2
            return dict(source=deployment, move=move["id"], line=line_id, lot=staging_lot,
                        production=a, location=stock, quantity=line["quantity"])
        recovered = receipt()
        assert stage(planner, staging_lot, a) == recovered["line"]
        must_fail(lambda: stage(planner, staging_lot, a, 3), "Conflicting native staging receipt")
        must_fail(lambda: stage(planner, staging_lot, b), "Conflicting native staging receipt")
        must_fail(lambda: planner.call("stock.lot", "norvale_stage_input", [staging_lot],
                                       staging_ref + "-late", stock, 2, a),
                  "New staging requires an active manufacturing order")
        assert receipt() == recovered
        if "analytics_instance" in fixture:
            archived = scan_staging_receipt(a, staging_lot)
            assert archived == json.loads(checkpoint.with_name("staging-observed.json").read_text())
            assert archived["name"] == str(recovered["move"]) and archived["line_id"] == recovered["line"]
        checkpoint.with_name("staging-recovery.json").write_text(json.dumps(recovered, indent=2) + "\n")
        report = operator.complete(a, out_a, 3, actual_a, [])
        assert operator.completion(a) == report
        must_fail(lambda: operator.complete(a, out_a, 3, input_lots((bulk_a, 2), (bottle, 4)), released),
                  "Native completion or replay differs")
        assert report["revision"] == rev_a and report["erp_work_ref"] == "ERP-WO-A"
        assert report["output"]["quantity"] == 3
        assert {row["lot_name"]: row["quantity"] for row in report["inputs"]} == {
            "BULK-104": 1.5, "BULK-105": 0.25, "BOTTLE-663": 4.0,
        }
        assert {row["uom_name"] for row in report["inputs"]} == {"kg", "Units"}
        assert total(out_a, hold) == 3 and not total(out_a, fixture["stock"])
        assert total(bulk_a, fixture["stock"]) == 3.5
        assert total(bulk_b, fixture["stock"]) == 18.75
        assert total(bottle, fixture["stock"]) == 94
        assert planner.dispatch("ERP-WO-A", product, bom_a, rev_a, 8, hold)["id"] == a
        child = report["backorder"]["native_id"]
        open_work = operator.read(MO, child)
        assert open_work["state"] == "confirmed" and open_work["product_qty"] == 5
        assert open_work["norvale_revision"] == rev_a
        assert open_work["origin"] == "ERP-WO-A"
        assert open_work["name"].endswith("-002")
        assert operator.read(MO, a)["name"].endswith("-001")
        assert operator.read(WO, open_work["workorder_ids"][0])["state"] == "ready"
        remaining = input_lots((bulk_b, 2.5), (bottle, 5))
        operator.start(child, remaining, released)
        next_report = operator.complete(child, out_c, 5, remaining, released)
        assert next_report["completion_ref"] != report["completion_ref"]
        assert next_report["backorder"] is None and total(out_c, hold) == 5
        assert operator.completion(a) == report
        assert planner.restart(center_a, saved["fault"], "RESTART-27")["native_id"] == saved["fault"]
        export = operator.export(["ERP-WO-A", "ERP-WO-B"], [center_a, center_b])
        assert {row["id"] for row in export["work"]} == {a, b, child}
        assert sum(row["completion"]["output"]["quantity"] for row in export["work"]) == 10
        assert all(row["operations"][0]["state"] == "done" and row["labor"] for row in export["work"])
        assert export["equipment"][0]["faults"][0]["native_id"] == saved["fault"]
        assert all(row["completion"]["output"]["record"]["source_url"] for row in export["work"])
        assert all(row["completion"]["output"]["item_code"] == "NV-SHAMPOO" for row in export["work"])
        assert operator.export(["other-run"])["work"] == []
        export_path = checkpoint.with_name("mes-export.json")
        export_path.write_text(json.dumps(export, indent=2) + "\n")
        print(f"after restart: 3 completed MOs, 10 units in hold, preserved genealogy, "
              f"stable ERP posting reference {report['completion_ref']}, native export {export_path}")
    elif phase == "interrupt":
        revision = planner.acknowledge(bom_a)["revision"]
        first = planner.dispatch("ERP-WO-Interrupted", product, bom_a, revision, 1, hold)["id"]
        second = planner.dispatch("ERP-WO-Waiting", product, bom_a, revision, 1, hold)["id"]
        allocated = input_lots((bulk_b, 0.5), (bottle, 1))
        operator.start(first, allocated, released)
        must_fail(lambda: operator.start(second, allocated, released), "allocated equipment is unavailable")
        marker = planner.fault(center_a, "GITEA-Interrupted")
        assert not operator.availability(center_a)["available"]
        assert operator.availability(center_b)["available"]
        must_fail(lambda: operator.complete(first, out_a, 1, allocated, released),
                  "requires running work and operable equipment")
        planner.restart(center_a, marker["native_id"], "RESTART-Interrupted")
        assert operator.availability(center_a)["working_state"] == "normal"
        assert not operator.availability(center_a)["available"]
        # A fresh native lot ensures the disposition check is exercised before posting.
        lot = planner.call("stock.lot", "create", {
            "name": "SHAMPOO-INTERRUPTED", "product_id": product,
        })
        must_fail(lambda: operator.complete(first, lot, 1, allocated, released),
                  "Interrupted work requires a separate batch disposition")
        assert total(lot, hold) == 0 and operator.read(MO, first)["state"] == "progress"
        print("interruption: occupied equipment denied new work, verified restart left batch pending")
    else:
        raise ValueError("Use before, after or interrupt")


if __name__ == "__main__":
    run(*sys.argv[1:])
