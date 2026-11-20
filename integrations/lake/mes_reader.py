"""Ingest verified Odoo MES completions for specific ERP work references."""
import argparse
from hashlib import sha256
import os
import math
if __package__:
    from .boundary import ingest
else:
    from boundary import ingest
from integrations.revisions import valid_receiver_marker
from integrations.revisions.receivers import BOM, ODOO_SITE


def completion_document(entity, completion):
    source = completion["work"]["source_id"]

    def native(ref, model):
        if ref["source_id"] != source or ref["entity"] != model or ref["system"] != "mes":
            raise ValueError("MES reference does not belong to the expected source and model")
        return str(ref["native_id"])

    def movement(row):
        return {"line_id": native(row["record"], "stock.move.line"),
                "lot_id": native(row["lot"], "stock.lot"), "lot_name": row["lot_name"],
                "product_id": native(row["product"], "product.product"), "item_code": row["item_code"],
                "quantity": row["quantity"], "unit_id": native(row["uom"], "uom.uom"),
                "unit": row["uom_name"]}

    return {**({"receiver_marker": completion["receiver_marker"]} if "receiver_marker" in completion else {}),
            **({"company_id": completion["company_id"]} if "company_id" in completion else {}),
            **({"rework_native": completion["rework_native"]} if "rework_native" in completion else {}),
            "work_order_ids": [native(ref, "mrp.workorder") for ref in completion["work_orders"]],
            "backorder_id": native(completion["backorder"], entity) if completion["backorder"] else None,
            "hold_location_id": native(completion["output_hold_location"], "stock.location"),
            "name": native(completion["work"], entity), "modified": completion["recorded_at_utc"],
            "erp_work_ref": completion["erp_work_ref"], "revision": completion["revision"],
            "bom_id": native(completion["bom"], "mrp.bom") if completion["bom"] else None,
            "completion_ref": completion["completion_ref"],
            "inputs": [movement(row) for row in completion["inputs"]],
            "output": movement(completion["output"])}


def scan(root, mes, work_refs=None, *, product=None, company=None):
    if isinstance(work_refs, str):
        raise TypeError("Pass a sequence of ERP work references")
    requested = sorted(set(work_refs)) if work_refs is not None else None
    domain = [["norvale_revision", "!=", False], ["state", "=", "done"]]
    if requested is not None:
        domain.append(["origin", "in", requested])
    if product is not None and company is not None:
        domain.extend([["product_id", "=", product], ["company_id", "=", company]])
    elif product is not None or company is not None:
        raise ValueError("A product scope requires its company")
    def fetch():
        optional = ["norvale_rework_of", "norvale_rework_authority", "norvale_rework_scrap"]
        fields = mes.call("mrp.production", "fields_get", optional, attributes=["type"])
        columns = ["company_id", *[field for field in optional if field in fields]]
        ids = mes.call("mrp.production", "search", domain, order="id")
        markers = {}
        supported = ids and ODOO_SITE in mes.call(BOM, "fields_get", [ODOO_SITE], attributes=["type"])
        for identifier in ids:
            completion = mes.completion(identifier)
            ref = completion["work"]
            if (ref["source_id"] != mes.source_id or ref["native_id"] != identifier
                    or (requested is not None and completion["erp_work_ref"] not in requested)):
                raise ValueError("Completion does not match the requested MES work")
            mo = mes.call("mrp.production", "read", [identifier], fields=columns)[0]
            if company is not None and mo["company_id"][0] != company:
                raise ValueError("Completion belongs to another company")
            completion = dict(completion, company_id=mo["company_id"][0])
            if mo.get("norvale_rework_of"):
                scrap = None
                if mo.get("norvale_rework_scrap"):
                    raw = mes.read("stock.scrap", mo["norvale_rework_scrap"][0])
                    lines = mes._lines(raw["move_ids"])
                    scrap = dict(id=raw["id"], state=raw["state"], production_id=raw["production_id"][0],
                                 location_id=raw["location_id"][0], destination_id=raw["scrap_location_id"][0],
                                 lines=[dict(id=x["id"], state=x["state"], lot_id=x["lot_id"][0],
                                             product_id=x["product_id"][0], unit_id=x["product_uom_id"][0],
                                             quantity=x["quantity"], location_id=x["location_id"][0],
                                             destination_id=x["location_dest_id"][0]) for x in lines])
                input_line = mes.read("stock.move.line", completion["inputs"][0]["record"]["native_id"])
                completion["rework_native"] = dict(source_order=mo["norvale_rework_of"][0],
                    authority=mo["norvale_rework_authority"], scrap=scrap,
                    input_location=input_line["location_id"][0])
            if supported and completion["bom"]:
                bom_id = completion["bom"]["native_id"]
                if bom_id not in markers:
                    row, = mes.call(BOM, "read", [bom_id], fields=[ODOO_SITE])
                    if row["id"] != bom_id or not valid_receiver_marker("mes", row.get(ODOO_SITE)):
                        raise ValueError("Invalid MES BOM receiver evidence")
                    markers[bom_id] = row[ODOO_SITE]
                completion["receiver_marker"] = markers[bom_id]
            yield completion
    scope = {key: value for key, value in [('product', product), ('company', company), ('work_refs', requested)]
             if value is not None}
    return ingest(root, "mrp.production", fetch, source=mes.source_id, projection=completion_document,
                  scope=scope or None)


def scan_staging(root, mes, *, product=None, company=None):
    """Archive native receipts linked to the original manufacturing order."""
    if (product is None) != (company is None):
        raise ValueError("A product scope requires its company")
    link = "norvale_staging_order_id"
    domain = [[link, "!=", False]]
    scope = None
    if product is not None:
        domain.extend([[link + ".product_id", "=", product], ["company_id", "=", company]])
        scope = dict(product=product, company=company)

    def fetch():
        if link not in mes.call("stock.move", "fields_get", [link], attributes=["type"]):
            return
        move_fields = [link, "is_inventory", "quantity", "product_uom_qty", "product_uom", "norvale_staging_receipt", "origin", "state", "company_id",
                  "move_line_ids", "product_id", "location_id", "location_dest_id", "write_date"]
        for move in mes.call("stock.move", "search_read", domain, fields=move_fields, order="id"):
            order = mes.read("mrp.production", move[link][0])
            line_id, = move["move_line_ids"]
            fields = ["move_id", "state", "product_id", "product_uom_id", "lot_id", "quantity",
                      "location_id", "location_dest_id", "company_id", "owner_id", "package_id", "result_package_id"]
            line, = mes.call("stock.move.line", "read", [line_id], fields=fields)
            locations = mes.call("stock.location", "read", [move["location_id"][0], move["location_dest_id"][0]],
                                 fields=["usage", "company_id"])
            places = {row["id"]: row for row in locations}
            origin, target = places[move["location_id"][0]], places[move["location_dest_id"][0]]
            inputs = mes.call("stock.move", "read", order["move_raw_ids"], fields=["product_id"])
            item, = mes.call("product.product", "read", [move["product_id"][0]],
                             fields=["uom_id", "property_stock_inventory"],
                             context={"allowed_company_ids": [move["company_id"][0]]})
            lot = mes.read("stock.lot", line["lot_id"][0])
            if (not str(move["norvale_staging_receipt"]).startswith("NV-STAGE-")
                    or move["origin"] != move["norvale_staging_receipt"]
                    or origin["usage"] != "inventory" or origin["company_id"] not in (False, move["company_id"])
                    or target["usage"] != "internal" or target["company_id"] != move["company_id"]
                    or move["product_id"] not in [raw["product_id"] for raw in inputs]
                    or item["property_stock_inventory"][0] != origin["id"]
                    or item["uom_id"] != line["product_uom_id"] or not math.isfinite(line["quantity"])
                    or not move["is_inventory"] or move["product_uom"] != line["product_uom_id"]
                    or move["quantity"] != line["quantity"] or move["product_uom_qty"] != line["quantity"]
                    or line["id"] != line_id or line["company_id"] != move["company_id"]
                    or line["owner_id"] or line["package_id"] or line["result_package_id"]
                    or move["state"] != "done" or line["state"] != "done" or line["quantity"] <= 0
                    or line["move_id"][0] != move["id"] or lot["product_id"] != move["product_id"]
                    or any(line[key] != move[key] for key in
                           ("product_id", "location_id", "location_dest_id"))
                    or lot["company_id"] != move["company_id"]
                    or order["company_id"] != move["company_id"] or not order["origin"]
                    or order["location_src_id"] != move["location_dest_id"]
                    or (scope and (order["product_id"][0], order["company_id"][0]) != (product, company))):
                raise ValueError("Staging line does not match its native move and manufacturing order")
            yield dict(name=str(move["id"]), modified=move["write_date"], production_id=order["id"],
                       erp_work_ref=order["origin"], receipt=move["norvale_staging_receipt"],
                       company_id=move["company_id"][0], line_id=line_id, lot_id=lot["id"],
                       lot_name=lot["name"], product_id=line["product_id"][0],
                       unit_id=line["product_uom_id"][0], unit=line["product_uom_id"][1],
                       quantity=line["quantity"], source_location_id=line["location_id"][0],
                       destination_location_id=line["location_dest_id"][0])
    return ingest(root, "stock.move", fetch, source=mes.source_id,
                  projection=lambda entity, row: row, scope=scope)


def scan_labor(root, mes, work_refs=None, *, product=None, company=None):
    """Read native productivity by its own durable ID through the reader account."""
    from integrations.mes.client import EVENT, MO, WO

    if isinstance(work_refs, str):
        raise TypeError("Pass a sequence of ERP work references")
    if (product is None) != (company is None):
        raise ValueError("A product scope requires its company")
    requested = sorted(set(work_refs)) if work_refs is not None else None
    production = "workorder_id.production_id."
    domain = [["norvale_scheduled_labor", "!=", False]]
    if requested is not None:
        domain.append([production + "origin", "in", requested])
    if product is not None:
        domain.extend([[production + "product_id", "=", product], ["company_id", "=", company]])

    def fetch():
        fields = ["norvale_scheduled_labor", "norvale_bridge_started", "workorder_id", "workcenter_id",
                  "user_id", "create_uid", "company_id", "date_start", "date_end", "duration",
                  "loss_type", "write_date"]
        supported = mes.call(EVENT, "fields_get", ["norvale_scheduled_labor"], attributes=["type"])
        if "norvale_scheduled_labor" not in supported:
            return
        for row in mes.call(EVENT, "search_read", domain, fields=fields, order="id"):
            labor = row["norvale_scheduled_labor"]
            wo = mes.read(WO, row["workorder_id"][0])
            mo = mes.read(MO, wo["production_id"][0])
            completion = mes.completion(mo["id"])
            intervals = labor["intervals"]
            if (not row["norvale_bridge_started"] or not row["date_start"] or not row["date_end"]
                    or row["user_id"] != row["create_uid"] or row["id"] not in wo["time_ids"]
                    or row["workcenter_id"] != wo["workcenter_id"] or wo["state"] != "done"
                    or wo["id"] not in mo["workorder_ids"] or row["company_id"] != mo["company_id"]
                    or str(labor["dispatch"]) != str(mo["id"]) or labor["source"] != mes.source_id
                    or labor["work_order"] != mo["origin"] or labor["role"] != "PRODUCTION"
                    or not intervals or sum(end - start for start, end in intervals) != labor["minutes"]
                    or (requested is not None and mo["origin"] not in requested)
                    or (company is not None and (mo["product_id"][0], mo["company_id"][0]) != (product, company))):
                raise ValueError("Scheduled labor does not match the native output and operator")
            # Export the event fingerprint without the controller's internal job keys.
            yield dict(name=str(row["id"]), modified=row["write_date"],
                       company_id=mo["company_id"][0], production_id=mo["id"], work_order_id=wo["id"],
                       work_center_id=row["workcenter_id"][0], user_id=row["user_id"][0],
                       erp_work_ref=mo["origin"], completion_ref=completion["completion_ref"],
                       output_line_id=completion["output"]["record"]["native_id"],
                       output_lot_id=completion["output"]["lot"]["native_id"],
                       event_sha256=sha256(labor["event"].encode()).hexdigest(), role=labor["role"],
                       business_minutes=labor["minutes"], business_minute_intervals=intervals,
                       odoo_timer_start_utc=row["date_start"], odoo_timer_end_utc=row["date_end"],
                       odoo_timer_duration=row["duration"], odoo_timer_loss_type=row["loss_type"])

    scope = {key: value for key, value in [('product', product), ('company', company), ('work_refs', requested)]
             if value is not None}
    return ingest(root, EVENT, fetch, source=mes.source_id, projection=lambda entity, row: row,
                  scope=scope or None)


if __name__ == "__main__":
    from integrations.mes.client import Mes
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("root", "url", "database", "login", "source"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--work-ref", action="append", required=True)
    parser.add_argument("--labor", action="store_true")
    args = parser.parse_args()
    client = Mes(args.url, args.database, args.login, os.environ["MES_PASSWORD"], args.source)
    collect = scan_labor if args.labor else scan
    print(collect(args.root, client, args.work_ref))
