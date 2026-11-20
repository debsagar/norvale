"""Resolve shipment feedback from posted ERP evidence and registered QMS authority."""
from decimal import Decimal
import json

from integrations.qms.adapter import INSPECTION

DELIVERY = "Delivery Note"
WAREHOUSE = "WAREHOUSE"


def shipment(engine, delivery, line, *, case=None, source=None):
    if not delivery or not any(
        s.get("target_id") == delivery and s.get("departed_day") is not None
        for s in engine.company_erp.state["shipments"].values()
    ):
        raise ValueError("Expected a confirmed dispatch from this company's ERP")
    note = engine.api[WAREHOUSE].get(DELIVERY, delivery)
    if (note["name"] != delivery or note["docstatus"] != 1 or note.get("is_return")
            or note["company"] != engine.company
            or (case is not None and (case.get("custom_delivery_source") != source
                                     or case.get("custom_delivery_customer") != note["customer"]))):
        raise ValueError("Shipment differs from the submitted outbound delivery")
    row, = (r for r in note["items"] if r["name"] == line)
    return note, row


def shipped_batch(erp, note, row):
    bundle_id = row.get("serial_and_batch_bundle")
    if not bundle_id:
        raise ValueError("Shipment line needs a posted outbound batch bundle")
    bundle = erp.get("Serial and Batch Bundle", bundle_id)
    if (bundle["name"] != bundle_id or bundle["voucher_type"] != DELIVERY
            or bundle["voucher_no"] != note["name"] or bundle["voucher_detail_no"] != row["name"]
            or bundle["docstatus"] != 1 or bundle.get("is_cancelled")
            or bundle["item_code"] != row["item_code"] or bundle["warehouse"] != row["warehouse"]
            or bundle["type_of_transaction"] != "Outward"):
        raise ValueError("Shipment line needs a matching posted outbound batch bundle")
    entries = bundle["entries"]
    batches = {entry.get("batch_no") for entry in entries}
    amounts = [Decimal(str(entry["qty"])) for entry in entries]
    if (len(batches) != 1 or not all(batches) or any(e.get("serial_no") for e in entries)
            or any(not amount.is_finite() or amount >= 0 for amount in amounts)
            or -sum(amounts) != Decimal(str(row["stock_qty"]))):
        raise ValueError("Shipment line needs a single posted batch in the shipped quantity")
    return batches.pop()


def allocation(note, batch, row):
    try:
        pins = json.loads(note.get("custom_norvale_quality_releases") or note.get("remarks") or "[]")
        entry, = (p for p in pins if p.get("batch") == batch)
        pin = entry["disposition"]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Shipment needs one released allocation for the batch") from exc
    if (not isinstance(pin, dict) or not pin.get("id") or pin.get("batch", batch) != batch
            or pin.get("status", "released") != "released" or not pin.get("version")
            or entry.get("qty") != row["stock_qty"]
            or (not isinstance(pin.get("qty"), (int, float)) or not 0 < row["stock_qty"] <= pin["qty"])):
        raise ValueError("Shipment needs one released allocation for the batch")
    return entry


def feedback_site(cfg, source):
    from native.production_profiles import quality_target

    site = cfg if source == cfg["qms_source"] else cfg.get("feedback_sites", {}).get(source)
    if not site:
        raise ValueError("Missing feedback credentials for the selected QMS source")
    target = quality_target(site)
    for actor in ("analyst", "approver"):
        account = site["lab"].get(actor)
        if account and quality_target({"qms_reader": account}) != target:
            raise ValueError("Feedback actor belongs to another QMS site")
    return site


def authority(engine, cfg, note, row, batch, requested):
    from native.production_profiles import quality_target

    entry = allocation(note, batch, row)
    pin = entry["disposition"]
    flow = engine.company_erp
    lot = flow.state["lots"].get(batch)
    if not lot or lot["item"] != row["item_code"] or pin.get("item", lot["item"]) != lot["item"]:
        raise ValueError("Shipment batch differs from the registered lot")
    registered, _ = flow.authority.route(
        lot["item"], lot.get("revision", {}).get("mes_source"), pinned=True)
    source = registered.get("qms_source")
    if not source:
        raise ValueError("Registered lot authority needs an unambiguous QMS source")
    if ((requested and requested != source)
            or (pin.get("qms_source") and pin["qms_source"] != source)):
        raise ValueError("Shipment release belongs to another QMS source than the registered lot authority")
    site = feedback_site(cfg, source)
    if quality_target(registered) != quality_target(site):
        raise ValueError("Feedback site differs from the registered lot authority")

    original = batch
    if lot.get("rework_key"):
        rework = flow.state.get("rework", {}).get(lot["rework_key"], {})
        original = rework.get("authority", {}).get("batch")
        if not original or rework.get("output_batch") != batch or rework.get("retest") != pin["id"]:
            raise ValueError("Shipment release differs from registered rework provenance")
    if pin.get("qms_batch", original) != original:
        raise ValueError("Shipment release names a different original QMS batch")
    return site, {"custom_qms_batch": original, "custom_delivery_batch": batch,
                  "custom_delivery_qms_source": source, "custom_delivery_inspection": pin["id"]}, entry


def inspection_matches(sample, scope):
    return (sample["batch_no"] == scope["custom_qms_batch"]
            and (sample.get("custom_rework_output_batch") or sample["batch_no"]) == scope["custom_delivery_batch"])


def verify_inspection(client, scope, item):
    release_id = scope["custom_delivery_inspection"]
    sample = client.get(INSPECTION, release_id)
    if (sample["name"] != release_id or sample["item_code"] != item or sample["docstatus"] != 1
            or sample["status"] != "Accepted" or sample["workflow_state"] not in ("Released", "Revoked")
            or not inspection_matches(sample, scope)
            or client.get("Batch", scope["custom_qms_batch"])["item"] != item):
        raise ValueError("Pinned inspection differs from the original QMS batch or shipped ERP batch")
    return sample


def verify_dispatch_version(engine, client, note, row, scope, entry, sample):
    """Resolve the exact shipped release, even after its current inspection changes."""
    pin = entry["disposition"]
    qty = row["stock_qty"]
    if entry.get("qms_lease"):
        lease = client.get("Norvale Dispatch Lease", entry["qms_lease"])
        request = json.loads(lease["request_json"])
        matches = [s for s in request["scopes"] if s["batch"] == scope["custom_qms_batch"]]
        if (lease["name"] != entry["qms_lease"] or request["id"] != lease["name"]
                or lease["lease_state"] != "submitted" or request["delivery"] != note["name"]
                or len(matches) != 1):
            raise ValueError("QMS dispatch lease differs from the submitted delivery")
        receipt = matches[0]
        if (receipt["inspection"] != sample["name"] or receipt["version"] != pin["version"]
                or receipt["item"] != row["item_code"] or receipt["uom"] != row["stock_uom"]
                or receipt["dispatch_qty"] != qty or receipt["qty"] != pin.get("qty")
                or not 0 < qty <= receipt["qty"]):
            raise ValueError("Shipment quantity or release version differs from the QMS dispatch lease")
        return
    if sample.get("custom_lot_quantity") != pin.get("qty") or sample.get("custom_lot_uom") != row["stock_uom"]:
        raise ValueError("Shipment quantity or unit differs from the QMS inspection")
    if sample["workflow_state"] == "Released" and sample.get("modified") == pin["version"]:
        return
    # Before native leases, fulfillment checkpointed the exact allocation verified at dispatch.
    confirmed = [s for s in engine.company_erp.state["shipments"].values()
                 if s.get("target_id") == note["name"] and s.get("departed_day") is not None]
    if (sample["workflow_state"] != "Revoked" or len(confirmed) != 1
            or [r for r in confirmed[0].get("rows", []) if r["batch"] == entry["batch"]] != [entry]):
        raise ValueError("Prior QMS release version lacks matching confirmed dispatch evidence")


def legacy_case(case, scope):
    """Verify bound records or recognize a legacy record whose binding can be recovered."""
    if case.get("custom_qms_batch") != scope["custom_qms_batch"]:
        raise ValueError("Complaint differs from the original QMS batch")
    binding = {key: scope[key] for key in scope if key != "custom_qms_batch"}
    if not any(case.get(key) for key in binding):
        return True
    if any(case.get(key) != value for key, value in binding.items()):
        raise ValueError("Complaint differs from its immutable shipment release binding")
    return False
