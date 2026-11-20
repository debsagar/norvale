"""Post an observed receipt against one previously dispatched lot row."""
from datetime import date
from decimal import Decimal, InvalidOperation
import json

from integrations.qms.adapter import CASE
from .scope import DELIVERY, WAREHOUSE, legacy_case


def authorize(engine, approver, case, note, line, scope, release, reason, legacy):
    if not reason.strip():
        raise ValueError("Withdrawal needs a documented reason")
    if release["workflow_state"] != "Revoked":
        raise ValueError("Withdrawal requires the revoked shipment release")
    batch = scope["custom_delivery_batch"]
    engine.company_erp.verify_row(note, line, batch, line["stock_qty"], line["warehouse"], "Outward", WAREHOUSE)
    release_id = release["name"]
    expected = {"custom_withdrawal_release": release_id, "custom_withdrawal_reason": reason}
    if not case.get("custom_withdrawal_approver"):
        bind = scope if legacy else {}
        case = approver.update(CASE, case["name"], **expected, **bind)
        if legacy_case(case, scope):
            raise ValueError("Native complaint lost the recovered shipment release binding")
    if not case.get("custom_withdrawal_approver") or any(case.get(k) != v for k, v in expected.items()):
        raise ValueError("Native withdrawal decision differs from the request")
    return {"complaint": case["name"], "delivery": note["name"], "release": release_id,
            "approver": case["custom_withdrawal_approver"]}


def prior_returns(api, note, line, omit):
    filters = {"return_against": note["name"], "is_return": 1, "docstatus": 1}
    total = Decimal(0)
    while page := api.list(DELIVERY, filters, order_by="name asc"):
        for record in page:
            if record["name"] == omit:
                continue
            returned = api.get(DELIVERY, record["name"])
            for row in returned["items"]:
                if row.get("dn_detail") == line["name"]:
                    qty = Decimal(str(row["stock_qty"]))
                    if not qty.is_finite() or qty >= 0 or row["item_code"] != line["item_code"]:
                        raise ValueError("Unexpected stock quantity or item on an earlier return")
                    total -= qty
                elif not row.get("dn_detail") and row["item_code"] == line["item_code"]:
                    raise ValueError("Earlier return lacks the source row; reconcile it before receiving")
        filters["name"] = [">", page[-1]["name"]]
    return total


def receive(engine, args, case, note, line, release, scope, source):
    if not args.observation or args.observation.strip() != args.observation or len(args.observation) > 100:
        raise ValueError("Provide a stable warehouse receipt reference of at most 100 characters")
    try:
        qty = Decimal(args.quantity)
    except InvalidOperation as exc:
        raise ValueError("Expected a positive stock quantity") from exc
    if not qty.is_finite() or qty <= 0 or Decimal(str(float(qty))) != qty:
        raise ValueError("Expected a positive, representable stock quantity")
    observed = date.fromisoformat(args.observed_on)
    batch = scope["custom_delivery_batch"]
    qms_source = scope["custom_delivery_qms_source"]
    release_id = case.get("custom_withdrawal_release")
    if not release_id or not case.get("custom_withdrawal_approver"):
        raise ValueError("Customer receipt requires a native withdrawal authorization")
    if release_id != release["name"] or release["workflow_state"] != "Revoked":
        raise ValueError("Withdrawal evidence differs from the dispatched lot")
    if not date.fromisoformat(note["posting_date"]) <= observed <= date.fromisoformat(engine.date()):
        raise ValueError("Arrival date must fall between dispatch and the current operating date")
    if line["warehouse"] == engine.wh["hold"] or line["conversion_factor"] != 1:
        raise ValueError("Expected dispatch outside quarantine and in stock UOM")
    company_erp = engine.company_erp
    shipped = Decimal(str(line["stock_qty"]))
    if not shipped.is_finite() or shipped <= 0:
        raise ValueError("Expected a positive outbound stock quantity")
    company_erp.verify_row(note, line, batch, float(shipped), line["warehouse"], "Outward", WAREHOUSE)

    api = engine.api[WAREHOUSE]
    # The same native unique operation field used for dispatch also covers a lost POST reply.
    if not engine.admin.list("Custom Field", {"dt": DELIVERY, "fieldname": "custom_norvale_operation", "unique": 1}):
        raise ValueError("Delivery Note needs the unique recurring operation field")
    if engine.api[WAREHOUSE].get("UOM", line["stock_uom"]).get("must_be_whole_number") and qty != qty.to_integral_value():
        raise ValueError("The shipment stock UOM requires a whole number")
    key = "customer-return:" + json.dumps([source, args.observation], separators=(",", ":"))
    evidence = {"complaint": args.complaint, "qms_source": qms_source, "erp_source": source,
                "release": release_id, "delivery": note["name"], "line": line["name"],
                "batch": batch, "qms_batch": scope["custom_qms_batch"], "observed_on": observed.isoformat(), "quantity": float(qty)}
    returns = company_erp.state.setdefault("customer_returns", {})
    entry = returns.get(key)
    if entry is not None and entry["receipt"] != evidence:
        # Existing ordinary-lot receipts were posted before QMS batch was explicit.
        legacy = {key: value for key, value in evidence.items() if key != "qms_batch"}
        if scope["custom_qms_batch"] != batch or entry["receipt"] != legacy:
            raise ValueError("Warehouse receipt intent changed on retry")
        evidence = legacy
    if entry is None or not entry.get("return_note"):
        if any(k != key and v["receipt"]["delivery"] == note["name"]
               and v["receipt"]["line"] == line["name"] and not v.get("return_note")
               for k, v in returns.items()):
            raise ValueError("Reconcile the pending receipt for this shipment row first")

    existing = engine.recover_company_doc(key, DELIVERY, WAREHOUSE) if entry is not None else None
    fields = {k: note[k] for k in ("company", "customer", "currency", "conversion_rate")}
    warehouse = engine.wh["hold"]

    def verify(doc, *, submitted):
        row, = doc["items"]
        if (doc["docstatus"] != int(submitted) or not doc["is_return"]
                or doc["return_against"] != note["name"]
                or doc["posting_date"] != entry["posting_date"]
                or json.loads(doc.get("custom_norvale_quality_releases") or "null") != {"customer_return": evidence}
                or any(doc[k] != v for k, v in fields.items())
                or row["dn_detail"] != line["name"] or row["item_code"] != line["item_code"]
                or row["warehouse"] != warehouse or row["batch_no"] != batch
                or row["uom"] != line["stock_uom"] or row["conversion_factor"] != 1
                or Decimal(str(row["stock_qty"])) != -qty or Decimal(str(row["qty"])) != -qty
                or row.get("against_sales_order") or row.get("against_sales_invoice")
                or (not submitted and row.get("serial_and_batch_bundle"))):
            raise ValueError("Native return differs from observed arrival")

    def capacity():
        if prior_returns(api, note, line, existing["name"] if existing else None) + qty > shipped:
            raise ValueError("Receipt would exceed the shipped quantity on this row")

    capacity()
    if entry is None:
        entry = returns[key] = {"receipt": evidence, "posting_date": engine.date()}
        engine.persist()
    payload = {**fields, "is_return": 1, "return_against": note["name"], "set_posting_time": 1,
               "posting_date": entry["posting_date"], "posting_time": "23:59:59",
               "custom_norvale_quality_releases": json.dumps({"customer_return": evidence}, sort_keys=True),
               "items": [company_erp.batch_row({
                   "dn_detail": line["name"], "item_code": line["item_code"], "qty": -float(qty),
                   "uom": line["stock_uom"], "conversion_factor": 1, "warehouse": warehouse,
                   "rate": line["rate"],
               }, batch)]}

    def before_submit(doc):
        verify(doc, submitted=False)
        capacity()

    posted = engine.write(key, DELIVERY, payload, WAREHOUSE, True, before_submit=before_submit)
    verify(posted, submitted=True)
    ledger, bundle = company_erp.verify_row(
        posted, posted["items"][0], batch, float(qty), warehouse, "Inward", WAREHOUSE)
    amounts = [Decimal(str(row["qty"])) for row in bundle["entries"]]
    if (bundle["name"] != posted["items"][0]["serial_and_batch_bundle"]
            or bundle.get("is_cancelled") or bundle["voucher_type"] != DELIVERY
            or Decimal(str(ledger["actual_qty"])) != qty or sum(amounts) != qty
            or any(amount <= 0 for amount in amounts)):
        raise ValueError("Posted bundle and stock ledger differ from the observed receipt")
    if entry.get("return_note", posted["name"]) != posted["name"]:
        raise ValueError("Warehouse receipt resolved to a different native return")
    entry["return_note"] = posted["name"]
    engine.persist()
    return {**evidence, "return_note": posted["name"], "return_row": posted["items"][0]["name"],
            "warehouse": warehouse, "bundle": bundle["name"], "stock_ledger": ledger["name"]}
