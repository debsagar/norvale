"""Qualify scheduled labor against fresh native Odoo and Delta."""

import json
import os
from pathlib import Path
import sys

from .client import EVENT, MO, WO, Mes
from .test_live import input_lots, must_fail


def run(fixture_path, lake_root):
    from adapter import Lake
    from mes_reader import scan_labor

    fixture = json.loads(Path(fixture_path).read_text())
    source = os.environ["MES_TEST_SOURCE"]
    url = os.environ["MES_TEST_URL"]
    password = os.environ["MES_TEST_PASSWORD"]
    planner, operator, observer = [
        Mes(url, "norvale_mes", role, password, source)
        for role in ("planner", "operator", "observer")
    ]
    reader = Mes(url, "norvale_mes", os.environ.get("MES_TEST_READER", "planner"), password, source)
    bom = fixture["boms"][0]
    center = fixture["centers"][0]
    revision = planner.acknowledge(bom)["revision"]
    product, hold = fixture["product"], fixture["hold"]
    bulk, bottle = fixture["bulk"][1], fixture["bottle"]
    released = [bulk, bottle]
    output, remainder, rejected_output = fixture["output"]
    work_ref = "ERP-WO-SCHEDULED"

    def dispatch(origin, qty):
        return planner.dispatch(origin, product, bom, revision, qty, hold)["id"]

    def envelope(mo, origin, intervals):
        event = "company:" + json.dumps([["mes", source, str(mo), origin, "qualification"],
                                         "complete", 0], separators=(",", ":"))
        return dict(event=event, role="PRODUCTION", center=center, dispatch=str(mo), source=source,
                    work_order=origin, intervals=intervals, minutes=sum(end - start for start, end in intervals))

    def rows(qty):
        return input_lots((bulk, qty / 2), (bottle, qty))

    mo = dispatch(work_ref, 4)
    labor = envelope(mo, work_ref, [[600, 630], [660, 678]])
    args = (mo, output, 2, rows(2), released)
    must_fail(lambda: observer.complete(*args, labor=labor), "Manufacturing authority required")
    must_fail(lambda: operator.complete(*args, labor=labor), "original production operator and timer")
    wo = operator.start(mo, rows(4), released)
    timer, = wo["time_ids"]
    start = operator.read(EVENT, timer)["date_start"]
    must_fail(lambda: planner.complete(*args, labor=labor), "original production operator and timer")
    must_fail(lambda: operator.call(EVENT, "write", [timer], {"norvale_scheduled_labor": labor}),
              "attribution is owned")
    must_fail(lambda: operator.complete(*args, labor=dict(labor, minutes=78)), "differ from the intervals")
    must_fail(lambda: operator.complete(mo, output, 2, rows(2), [], labor=labor), "held or lacks release")
    assert operator.read(MO, mo)["state"] != "done"
    assert not operator.read(EVENT, timer)["date_end"]
    assert scan_labor(lake_root, reader, [work_ref]) == 0

    # Discard the committed reply. Retry from a newly authenticated session.
    operator.call(MO, "norvale_complete_scheduled", [mo], output, 2, rows(2), released, labor)
    operator = Mes(url, "norvale_mes", "operator", password, source)
    report = operator.complete(mo, output, 2, rows(2), [], labor=labor)
    assert report["labor"]["native_id"] == timer and report["output"]["quantity"] == 2
    native = operator.read(EVENT, timer)
    assert native["date_start"] == start and native["date_end"] and native["duration"] < labor["minutes"]
    must_fail(lambda: operator.complete(*args, labor=dict(labor, intervals=[[600, 648]])),
              "different or missing scheduled labor")
    must_fail(lambda: operator.complete(mo, output, 2, rows(1), [], labor=labor),
              "Native completion or replay differs")
    must_fail(lambda: planner.complete(*args, labor=labor), "original production operator and timer")

    child = report["backorder"]["native_id"]
    child_wo, = operator.read(MO, child)["workorder_ids"]
    assert not operator.read(WO, child_wo)["time_ids"]
    assert scan_labor(lake_root, reader, [work_ref]) == 1
    operator.start(child, rows(2), released)
    child_args = (child, remainder, 2, rows(2), released)
    must_fail(lambda: operator.complete(*child_args, labor=labor), "does not match the native dispatch")
    child_report = operator.complete(*child_args, labor=envelope(child, work_ref, [[1500, 1530]]))
    child_timer = child_report["labor"]["native_id"]
    assert child_timer != timer and child_wo != wo["id"]

    interrupted_ref = "ERP-WO-SCHEDULED-INTERRUPTED"
    interrupted = dispatch(interrupted_ref, 1)
    interrupted_wo = operator.start(interrupted, rows(1), released)
    interrupted_args = (interrupted, rejected_output, 1, rows(1), released)
    interrupted_labor = envelope(interrupted, interrupted_ref, [[1600, 1610]])
    fault = planner.fault(center, "SCHEDULED-INTERRUPTION")["native_id"]
    planner.restart(center, fault, "SCHEDULED-RESTART")
    must_fail(lambda: operator.complete(*interrupted_args, labor=interrupted_labor), "original open productive work")
    planner.call(MO, "norvale_reject_interrupted", [interrupted], fault, "SCHEDULED-REJECTION")
    must_fail(lambda: operator.complete(*interrupted_args, labor=interrupted_labor), "original open productive work")
    assert operator.read(MO, interrupted)["state"] == "cancel"

    scope = dict(product=product, company=operator.read(MO, mo)["company_id"][0])
    assert scan_labor(lake_root, reader, **scope) == 1
    assert scan_labor(lake_root, reader, **scope) == 0
    assert scan_labor(lake_root, reader, [interrupted_ref]) == 0
    lake = Lake(Path(lake_root) / "delta", {"reader": [EVENT]})
    docs = {int(row["native_id"]): json.loads(row["payload"]) for row in lake.read("reader", EVENT)}
    assert len(docs) == 2 and {timer, child_timer} == docs.keys()
    for native_id, production, operation, minutes in [(timer, mo, wo["id"], 48),
                                                       (child_timer, child, child_wo, 30)]:
        row = docs[native_id]
        assert (row["production_id"], row["work_order_id"], row["user_id"], row["business_minutes"]) == (
            production, operation, operator.uid, minutes)
        assert "event" not in row and "description" not in row
    assert docs[timer]["business_minute_intervals"] == [[600, 630], [660, 678]]
    assert docs[timer]["odoo_timer_start_utc"] == start
    assert docs[timer]["odoo_timer_end_utc"] == native["date_end"]
    assert docs[timer]["odoo_timer_duration"] == native["duration"]
    print(json.dumps(dict(labor=docs, interrupted_mo=interrupted, interrupted_wo=interrupted_wo["id"],
                          fault=fault, delta_snapshots=len(docs), delta_commits=len(lake.history())), indent=2))


if __name__ == "__main__":
    run(*sys.argv[1:])
