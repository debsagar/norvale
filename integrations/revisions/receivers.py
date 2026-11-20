"""Native ERPNext 15 and Odoo 18 BOM import and readback."""

from decimal import Decimal
import re
from uuid import uuid4

from . import amount, line_map, require, text, valid_receiver_marker, wire_quantity


ERP_KEY = "custom_norvale_revision_key"
ERP_SITE = "custom_norvale_receiver_marker"
ODOO_SITE = "norvale_receiver_marker"
ODOO_KEY = "norvale_import_key"
BOM = "mrp.bom"


def erp_receiver_marker(metadata):
    field = next((row for row in metadata["fields"] if row.get("fieldname") == ERP_SITE), {})
    marker = field.get("default")
    require(field.get("read_only") == 1 and valid_receiver_marker("erp", marker),
            "ERPNext receiver marker is not commissioned")
    return marker


def positive_id(value):
    require(type(value) is int and value > 0, "Expected a positive Odoo native ID")
    return value


def check_quantity(qty, increment):
    wire_quantity(qty)
    require(Decimal(qty) % Decimal(amount(increment)) == 0,
            f"Quantity {qty} does not fit native increment {increment}")


class ERPNext:
    """Use the release's Frappe ErpClient and a unique BOM custom field."""

    def __init__(self, client):
        self.client = client

    def provision(self, *, scoped=True):
        """One-time setup with a Frappe customization account."""
        spec = {"dt": "BOM", "label": "Norvale Revision Key", "fieldname": ERP_KEY,
                "fieldtype": "Data", "insert_after": "item", "unique": 1,
                "read_only": 1, "no_copy": 1, "allow_on_submit": 0}
        self._provision_field(spec)
        if not scoped:
            return
        self._provision_field({**spec, "label": "Norvale Receiver Marker", "fieldname": ERP_SITE,
                               "insert_after": ERP_KEY, "unique": 0, "permlevel": 0},
                              site_marker=True)

    def _provision_field(self, spec, *, site_marker=False):
        fieldname = spec["fieldname"]
        existing = self.client.list("Custom Field", {"dt": "BOM", "fieldname": fieldname}, limit=2)
        require(len(existing) <= 1, "Ambiguous ERPNext revision field")
        if not existing:
            self.client.create("Custom Field", {**spec, "default": uuid4().hex} if site_marker else spec)
        row = self.client.get("Custom Field", "BOM-" + fieldname)
        require(all(row.get(key) == value for key, value in spec.items())
                and (not site_marker or valid_receiver_marker("erp", row.get("default"))),
                "ERPNext revision custom field differs from the required schema")

    def _receiver_marker(self, bom=None):
        marker = erp_receiver_marker(self.client.metadata("BOM"))
        require(bom is None or bom.get(ERP_SITE) == marker,
                "ERPNext BOM receiver marker differs from the native site")
        return marker

    def validate(self, plan):
        company = text(plan["settings"]["company"])
        require(self.client.get("Company", company)["name"] == company, "ERPNext company mismatch")
        for target in [plan["output"], *plan["lines"]]:
            code, unit = text(target["item"]), text(target["unit"])
            item = self.client.get("Item", code)
            uom = self.client.get("UOM", unit)
            require(item["name"] == code and item["stock_uom"] == unit and item["is_stock_item"]
                    and not item["disabled"] and uom["name"] == unit and uom["enabled"],
                    f"ERPNext item or stock UOM mismatch for {code}")
            wire_quantity(target["qty"])
            if uom["must_be_whole_number"]:
                check_quantity(target["qty"], 1)

    def ensure(self, marker, plan, *, scoped=False):
        self.validate(plan)
        if scoped:
            self._receiver_marker()
        matches = self.client.list("BOM", {ERP_KEY: marker}, limit=2)
        require(len(matches) <= 1, "Ambiguous ERPNext BOM import key")
        if matches:
            native_id = matches[0]["name"]
        else:
            out = plan["output"]
            doc = {ERP_KEY: marker, "item": out["item"], "uom": out["unit"],
                   "quantity": wire_quantity(out["qty"]), "company": plan["settings"]["company"],
                   "is_active": 1, "is_default": 0, "with_operations": 0,
                   "items": [{"item_code": row["item"], "uom": row["unit"],
                              "qty": wire_quantity(row["qty"]), "conversion_factor": 1,
                              "do_not_explode": 1, "include_item_in_manufacturing": 1}
                             for row in plan["lines"]]}
            native_id = self.client.create("BOM", doc)["name"]
        bom = self._read(native_id, marker, plan)
        if scoped:
            self._receiver_marker(bom)
        if bom["docstatus"] == 0:
            self.client.call("frappe.client.submit", {"doc": bom})
        return native_id

    def _read(self, native_id, marker, plan):
        bom = self.client.get("BOM", native_id)
        out = plan["output"]
        require(bom["name"] == native_id and bom[ERP_KEY] == marker and bom["is_active"] == 1
                and bom["docstatus"] in (0, 1) and bom["company"] == plan["settings"]["company"]
                and (bom["item"], bom["uom"], amount(bom["quantity"])) ==
                (out["item"], out["unit"], out["qty"])
                and not any(bom.get(key) for key in ("with_operations", "operations", "scrap_items",
                                                     "process_loss_percentage", "process_loss_qty",
                                                     "allow_alternative_item")),
                f"ERPNext BOM {native_id} header differs")
        rows = []
        for row in bom["items"]:
            require(row["stock_uom"] == row["uom"] and row["conversion_factor"] == 1
                    and amount(row["stock_qty"]) == amount(row["qty"]) and row["do_not_explode"]
                    and row["include_item_in_manufacturing"] and not any(
                        row.get(key) for key in ("allow_alternative_item", "sourced_by_supplier",
                                                 "bom_no")),
                    f"ERPNext BOM {native_id} material options or stock quantity differ")
            rows.append({"item": row["item_code"], "unit": row["uom"], "qty": amount(row["qty"])})
        require(line_map(rows) == line_map(plan["lines"]), f"ERPNext BOM {native_id} lines differ")
        return bom

    def verify(self, native_id, marker, plan, *, scoped=False):
        self.validate(plan)
        bom = self._read(native_id, marker, plan)
        require(bom["docstatus"] == 1, f"ERPNext BOM {native_id} must be submitted")
        observed = {"bom_id": native_id, "import_key": marker}
        if scoped:
            observed["receiver_marker"] = self._receiver_marker(bom)
        return observed


class Odoo:
    def __init__(self, mes):
        self.client = mes

    def _rows(self, model, ids, fields):
        rows = self.client.call(model, "read", ids, fields=fields.split())
        require(len(rows) == len(ids) and {row["id"] for row in rows} == set(ids),
                f"Incomplete Odoo readback for {model}")
        return rows

    def _read(self, model, identifier, fields):
        return self._rows(model, [positive_id(identifier)], fields)[0]

    def validate(self, plan):
        settings = plan["settings"]
        company = positive_id(settings["company_id"])
        operation = settings["operation"]
        center = self._read("mrp.workcenter", operation["workcenter_id"], "company_id active")
        require(center["active"] and (center["company_id"] or [None])[0] == company,
                "Odoo work center does not belong to the configured company")
        text(operation["name"])
        wire_quantity(amount(operation["minutes"]))
        template = None
        for target in [plan["output"], *plan["lines"]]:
            item = self._read("product.product", target["item"],
                              "product_tmpl_id uom_id active company_id tracking is_storable")
            unit = self._read("uom.uom", target["unit"], "active rounding")
            require(item["active"] and item["tracking"] == "lot" and item["is_storable"]
                    and (not item["company_id"] or item["company_id"][0] == company)
                    and unit["active"] and item["uom_id"][0] == target["unit"],
                    f"Odoo product or stock UOM mismatch for {target['item']}")
            check_quantity(target["qty"], unit["rounding"])
            if template is None:
                template = item["product_tmpl_id"][0]
        return template

    def ensure(self, marker, plan, *, scoped=False):
        if scoped:
            fields = self.client.call(BOM, "fields_get", [ODOO_SITE], attributes=["type"])
            require(ODOO_SITE in fields, "Odoo receiver marker is not installed or accessible")
        ids = self.client.call(BOM, "search", [[ODOO_KEY, "=", marker]], limit=2,
                               context={"active_test": False})
        require(len(ids) <= 1, "Ambiguous Odoo BOM import key")
        if ids:
            return str(ids[0])
        template = self.validate(plan)
        out, settings = plan["output"], plan["settings"]
        operation = settings["operation"]
        native_id = self.client.call(BOM, "create", {
            ODOO_KEY: marker, "code": "NV-PLM-" + marker, "type": "normal", "consumption": "strict",
            "product_tmpl_id": template, "product_id": out["item"], "product_uom_id": out["unit"],
            "product_qty": wire_quantity(out["qty"]), "company_id": settings["company_id"],
            "bom_line_ids": [[0, 0, {"product_id": row["item"], "product_uom_id": row["unit"],
                                     "product_qty": wire_quantity(row["qty"])}] for row in plan["lines"]],
            "operation_ids": [[0, 0, {"name": operation["name"], "time_mode": "manual",
                                      "time_cycle_manual": wire_quantity(amount(operation["minutes"])),
                                      "workcenter_id": operation["workcenter_id"]}]],
        })
        return str(native_id)

    def verify(self, native_id, marker, plan, *, scoped=False):
        template = self.validate(plan)
        identifier = int(native_id)
        require(str(identifier) == native_id, "Noncanonical Odoo BOM ID")
        ack = self.client.acknowledge(identifier)
        site_field = " " + ODOO_SITE if scoped else ""
        bom = self._read(BOM, identifier,
                         f"{ODOO_KEY} code type consumption active company_id product_tmpl_id product_id "
                         "product_uom_id product_qty bom_line_ids operation_ids byproduct_ids" + site_field)
        out, settings = plan["output"], plan["settings"]
        require(bom[ODOO_KEY] == marker and bom["code"] == "NV-PLM-" + marker
                and bom["active"] and bom["type"] == "normal" and bom["consumption"] == "strict"
                and not bom["byproduct_ids"] and (bom["company_id"] or [None])[0] == settings["company_id"]
                and bom["product_tmpl_id"][0] == template and (bom["product_id"] or [None])[0] == out["item"]
                and bom["product_uom_id"][0] == out["unit"] and amount(bom["product_qty"]) == out["qty"],
                f"Odoo BOM {native_id} header differs")
        rows = self._rows("mrp.bom.line", bom["bom_line_ids"],
                          "product_id product_uom_id product_qty operation_id")
        require(all(not row["operation_id"] for row in rows),
                f"Odoo BOM {native_id} has component-specific routing")
        actual = [{"item": row["product_id"][0], "unit": row["product_uom_id"][0],
                   "qty": amount(row["product_qty"])} for row in rows]
        require(line_map(actual) == line_map(plan["lines"]), f"Odoo BOM {native_id} lines differ")
        ops = self._rows("mrp.routing.workcenter", bom["operation_ids"],
                         "name workcenter_id time_mode time_cycle_manual")
        op = settings["operation"]
        require(len(ops) == 1 and (ops[0]["name"], ops[0]["workcenter_id"][0], ops[0]["time_mode"],
                                   amount(ops[0]["time_cycle_manual"])) ==
                (op["name"], op["workcenter_id"], "manual", amount(op["minutes"])),
                f"Odoo BOM {native_id} operation differs")
        require(ack["bom_id"] == identifier
                and re.fullmatch(re.escape(bom["code"]) + r"@[0-9a-f]{64}", ack["revision"])
                and ack == self.client.acknowledge(identifier),
                f"Odoo BOM {native_id} fingerprint differs")
        observed = {"bom_id": identifier, "import_key": marker, "revision": ack["revision"]}
        if scoped:
            require(valid_receiver_marker("mes", bom.get(ODOO_SITE)), "Odoo receiver marker is invalid")
            observed["receiver_marker"] = bom[ODOO_SITE]
        return observed
