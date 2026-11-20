"""Call Odoo Community Manufacturing and independently read back its records."""

from collections import defaultdict
import math
from xmlrpc.client import SafeTransport, ServerProxy, Transport


MO = "mrp.production"
WO = "mrp.workorder"
LINE = "stock.move.line"
CENTER = "mrp.workcenter"
EVENT = "mrp.workcenter.productivity"
LOT = "stock.lot"

FIELDS = {
    MO: ["name", "origin", "state", "product_id", "product_uom_id", "product_qty",
         "norvale_revision", "bom_id", "company_id", "backorder_sequence",
         "procurement_group_id", "workorder_ids", "move_raw_ids", "move_finished_ids",
         "lot_producing_id", "location_src_id", "location_dest_id", "date_start", "date_finished", "write_date"],
    WO: ["name", "state", "production_id", "workcenter_id", "date_start", "date_finished",
         "duration", "time_ids", "write_date"],
    LINE: ["move_id", "state", "product_id", "product_uom_id", "lot_id", "quantity",
           "location_id", "location_dest_id", "consume_line_ids", "produce_line_ids",
           "date", "write_date"],
    LOT: ["name", "product_id", "company_id", "write_date"],
    "product.product": ["default_code", "display_name"],
    CENTER: ["name", "code", "working_state", "write_date"],
    EVENT: ["workcenter_id", "workorder_id", "user_id", "loss_id", "loss_type", "description",
            "date_start", "date_end", "duration", "write_date", "norvale_interrupted_id",
            "norvale_disposition"],
    "stock.scrap": ["state", "production_id", "lot_id", "product_id", "product_uom_id",
                    "scrap_qty", "location_id", "scrap_location_id", "move_ids"],
    "stock.move": ["state", "move_line_ids", "write_date"],
}


class _Timeout:
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = 30
        return connection


class _HTTP(_Timeout, Transport):
    pass


class _HTTPS(_Timeout, SafeTransport):
    pass


class Mes:
    def __init__(self, url, db, login, password, source_id):
        """source_id is the stable, unique identity of this MES deployment."""
        if not source_id or ":" in source_id:
            raise ValueError("Use a stable MES source_id without colons")
        self.url, self.db, self.password = url.rstrip("/"), db, password
        self.source_id = source_id
        self.transport = _HTTPS() if self.url.startswith("https:") else _HTTP()
        common = ServerProxy(f"{self.url}/xmlrpc/2/common", transport=self.transport)
        self.uid = common.authenticate(db, login, password, {})
        if not self.uid:
            raise PermissionError("Odoo authentication failed")
        self.rpc = ServerProxy(f"{self.url}/xmlrpc/2/object", transport=self.transport)

    def call(self, model, method, *args, **kwargs):
        return self.rpc.execute_kw(self.db, self.uid, self.password, model, method, args, kwargs)

    def _rows(self, model, ids):
        if not ids:
            return []
        rows = self.call(model, "read", list(ids), fields=FIELDS[model])
        if {row["id"] for row in rows} != set(ids):
            raise RuntimeError(f"Incomplete native readback for {model}")
        return sorted(rows, key=lambda row: row["id"])

    def read(self, model, identifier):
        return self._rows(model, [identifier])[0]

    def _ref(self, model, identifier):
        return {
            "system": "mes", "source_id": self.source_id, "entity": model,
            "native_id": identifier, "source_url": f"{self.url}/web#id={identifier}&model={model}",
        }

    def acknowledge(self, bom_id):
        return self.call(MO, "norvale_acknowledge", bom_id)

    def dispatch(self, work_ref, product_id, bom_id, revision, quantity, hold_location_id):
        native_id = self.call(MO, "norvale_dispatch", work_ref, product_id, bom_id,
                              revision, quantity, hold_location_id)
        row = self.read(MO, native_id)
        if (row["origin"] != work_ref or row["bom_id"][0] != bom_id
                or row["product_id"][0] != product_id or row["norvale_revision"] != revision
                or row["location_dest_id"][0] != hold_location_id or row["state"] == "draft"):
            raise RuntimeError("MES dispatch readback differs")
        group = row["procurement_group_id"]
        family = self.call(MO, "search_read", [["procurement_group_id", "=", group[0]]],
                           fields=["product_qty"]) if group else [row]
        if not math.isclose(sum(order["product_qty"] for order in family), quantity,
                            rel_tol=0, abs_tol=1e-9):
            raise RuntimeError("MES dispatch readback quantity differs")
        return row

    def release_intermediate(self, lot_id, item, qty, stock, *, origin):
        """Transfer the original manufactured lot, keeping its native move genealogy."""
        lot = self.read(LOT, lot_id)
        product = self.read("product.product", lot["product_id"][0])
        produced = self.call(LINE, "search_read", [
            ["lot_id", "=", lot_id], ["move_id.production_id", "!=", False], ["state", "=", "done"],
        ], fields=["location_dest_id", "quantity", "product_uom_id"], limit=2)
        if (product["default_code"] != item or len(produced) != 1
                or not math.isclose(produced[0]["quantity"], qty, rel_tol=0, abs_tol=1e-8)):
            raise RuntimeError("Intermediate lot does not match the native production")
        hold = produced[0]["location_dest_id"][0]
        uom = produced[0]["product_uom_id"][0]
        location, = self.call("stock.location", "read", [stock], fields=["warehouse_id", "company_id"])
        company = location["company_id"][0]
        if hold == stock:
            raise RuntimeError("Intermediate output must originate in hold")
        picking = "stock.picking"
        ids = self.call(picking, "search", [["origin", "=", origin], ["company_id", "=", company]], limit=2)
        if len(ids) > 1:
            raise RuntimeError("Ambiguous intermediate transfer")
        if not ids:
            warehouse, = self.call("stock.warehouse", "read", [location["warehouse_id"][0]], fields=["int_type_id"])
            move = dict(name=origin, product_id=product["id"], product_uom=uom,
                        product_uom_qty=qty, company_id=company, location_id=hold,
                        location_dest_id=stock)
            line = dict(lot_id=lot_id, product_id=product["id"], product_uom_id=uom, quantity=qty,
                        company_id=company, location_id=hold, location_dest_id=stock)
            ids = [self.call(picking, "create", dict(
                origin=origin, company_id=company, picking_type_id=warehouse["int_type_id"][0],
                location_id=hold, location_dest_id=stock,
                move_ids=[(0, 0, dict(move, move_line_ids=[(0, 0, line)]))]))]
        fields = ["state", "location_id", "location_dest_id", "move_ids"]
        row, = self.call(picking, "read", ids, fields=fields)
        lines = self._lines(row["move_ids"])
        if (row["location_id"][0] != hold or row["location_dest_id"][0] != stock
                or len(row["move_ids"]) != 1 or len(lines) != 1
                or lines[0]["location_id"][0] != hold or lines[0]["location_dest_id"][0] != stock
                or not self._matches_inputs([dict(lot_id=lot_id, quantity=qty)], lines)):
            raise RuntimeError("Intermediate transfer differs from released lot")
        if row["state"] != "done":
            quants = self.call("stock.quant", "search_read", [
                ["lot_id", "=", lot_id], ["location_id", "=", hold],
                ["company_id", "=", company], ["owner_id", "=", False], ["package_id", "=", False],
            ], fields=["quantity"])
            if sum(quant["quantity"] for quant in quants) + 1e-8 < qty:
                raise RuntimeError("Intermediate transfer exceeds native hold stock")
            self.call("stock.move", "write", row["move_ids"], {"picked": True})
            self.call(picking, "button_validate", ids, context={"skip_backorder": True})
            row, = self.call(picking, "read", ids, fields=fields)
            if row["state"] != "done" or not self._matches_inputs(
                    [dict(lot_id=lot_id, quantity=qty)], self._lines(row["move_ids"])):
                raise RuntimeError("Intermediate transfer did not post the released lot")
        return ids[0]

    def start(self, mo_id, inputs, released_lot_ids):
        native_id = self.call(MO, "norvale_start", [mo_id], inputs, list(released_lot_ids))
        row = self.read(WO, native_id)
        if row["state"] != "progress" or row["production_id"][0] != mo_id:
            raise RuntimeError("MES start readback differs")
        mo = self.read(MO, mo_id)
        reservations = self._lines(mo["move_raw_ids"])
        if not self._matches_inputs(inputs, reservations):
            raise RuntimeError("MES reservation readback differs")
        return row

    @staticmethod
    def _matches_inputs(request, move_lines):
        actual = defaultdict(float)
        for line in move_lines:
            if not line["lot_id"]:
                return False
            actual[line["lot_id"][0]] += line["quantity"]
        return len(request) == len(actual) and all(
            math.isclose(row["quantity"], actual[row["lot_id"]], rel_tol=0, abs_tol=1e-9)
            for row in request
        )

    def complete(self, mo_id, output_lot_id, quantity, inputs, released_lot_ids, *, labor=None):
        args = ([mo_id], output_lot_id, quantity, inputs, list(released_lot_ids))
        if labor is None:
            native_id = self.call(MO, "norvale_complete", *args)
        else:
            if labor["source"] != self.source_id:
                raise ValueError("Scheduled labor belongs to a different MES source")
            timer_id = self.call(MO, "norvale_complete_scheduled", *args, labor)
            native_id = mo_id
        report = self.completion(native_id)
        actual = defaultdict(float)
        for row in report["inputs"]:
            actual[row["lot"]["native_id"]] += row["quantity"]
        if (native_id != mo_id or report["output"]["lot"]["native_id"] != output_lot_id
                or report["output"]["quantity"] != quantity or len(actual) != len(inputs)
                or any(not math.isclose(row["quantity"], actual[row["lot_id"]],
                                        rel_tol=0, abs_tol=1e-9) for row in inputs)):
            raise RuntimeError("MES completion readback differs from the request")
        if labor is not None:
            timer, = self.call(EVENT, "read", [timer_id],
                               fields=["norvale_scheduled_labor", "workorder_id", "user_id", "date_end"])
            if (timer["id"] != timer_id or not timer["date_end"] or timer["user_id"][0] != self.uid
                    or timer["norvale_scheduled_labor"] != labor
                    or timer["workorder_id"][0] not in {ref["native_id"] for ref in report["work_orders"]}):
                raise RuntimeError("MES scheduled labor readback differs from the request")
            report["labor"] = self._ref(EVENT, timer_id)
        return report

    def _lines(self, move_ids):
        moves = self._rows("stock.move", move_ids)
        return self._rows(LINE, [line_id for move in moves for line_id in move["move_line_ids"]])

    def _movement(self, row):
        lot = self.read(LOT, row["lot_id"][0])
        product = self.read("product.product", row["product_id"][0])
        return {
            "record": self._ref(LINE, row["id"]), "lot": self._ref(LOT, lot["id"]),
            "lot_name": lot["name"], "product": self._ref("product.product", product["id"]),
            "item_code": product["default_code"],
            "quantity": row["quantity"], "uom": self._ref("uom.uom", row["product_uom_id"][0]),
            "uom_name": row["product_uom_id"][1], "recorded_at_utc": row["date"],
            "write_date": row["write_date"],
        }

    def _next_order(self, mo):
        group = mo["procurement_group_id"]
        if not group:
            return None
        ids = self.call(MO, "search", [
            ["procurement_group_id", "=", group[0]],
            ["backorder_sequence", "=", mo["backorder_sequence"] + 1],
        ], limit=2)
        if len(ids) > 1:
            raise RuntimeError("Ambiguous native backorder")
        return self._ref(MO, ids[0]) if ids else None

    def completion(self, mo_id):
        mo = self.read(MO, mo_id)
        if mo["state"] != "done" or not mo["norvale_revision"]:
            raise ValueError("No completed MES execution")
        outputs = [row for row in self._lines(mo["move_finished_ids"]) if row["state"] == "done"]
        inputs = [row for row in self._lines(mo["move_raw_ids"]) if row["state"] == "done"]
        if (len(outputs) != 1 or not inputs
                or outputs[0]["lot_id"] != mo["lot_producing_id"]
                or outputs[0]["location_dest_id"] != mo["location_dest_id"]
                or set(outputs[0]["consume_line_ids"]) != {row["id"] for row in inputs}
                or any(outputs[0]["id"] not in row["produce_line_ids"] for row in inputs)):
            raise RuntimeError("Incomplete native lot genealogy or hold placement")
        return {
            "completion_ref": f"{self.source_id}:{MO}:{mo_id}",
            "work": self._ref(MO, mo_id), "erp_work_ref": mo["origin"], "revision": mo["norvale_revision"],
            "bom": self._ref("mrp.bom", mo["bom_id"][0]) if mo["bom_id"] else None,
            "work_orders": [self._ref(WO, identifier) for identifier in mo["workorder_ids"]],
            "recorded_at_utc": mo["date_finished"],
            "output_hold_location": self._ref("stock.location", mo["location_dest_id"][0]),
            "output": self._movement(outputs[0]), "inputs": [self._movement(row) for row in inputs],
            "backorder": self._next_order(mo),
        }

    def rework_record(self, source_id):
        rows = self.call(MO, "search_read", [["norvale_rework_of", "=", source_id]],
                         fields=["state", "norvale_rework_of", "norvale_rework_authority",
                                 "norvale_rework_scrap"], limit=2)
        if not rows:
            return None
        row, = rows
        if row["state"] != "done" or row["norvale_rework_of"][0] != source_id:
            raise RuntimeError("Invalid native rework order")
        return row

    def execute_rework(self, source_id, authority, total, salvage):
        return self.call(MO, "norvale_rework", source_id, authority, total, salvage)

    def availability(self, center_id):
        row = self.read(CENTER, center_id)
        busy = self.call(WO, "search_count", [
            ["workcenter_id", "=", center_id], ["state", "=", "progress"],
        ])
        return {**self._ref(CENTER, center_id), **row,
                "available": row["working_state"] == "normal" and busy == 0}

    def fault(self, center_id, maintenance_ref):
        identifier = self.call(CENTER, "norvale_fault", [center_id], maintenance_ref)
        row = self.read(EVENT, identifier)
        if (row["workcenter_id"][0] != center_id or row["loss_type"] != "availability"
                or row["description"].partition("\n")[0] != f"Fault {maintenance_ref}"):
            raise RuntimeError("MES equipment fault readback differs")
        return {**self._ref(EVENT, identifier), **row}

    def restart(self, center_id, fault_id, verified_ref):
        identifier = self.call(CENTER, "norvale_restart", [center_id], fault_id, verified_ref)
        row = self.read(EVENT, identifier)
        if (row["workcenter_id"][0] != center_id or row["loss_type"] != "availability"
                or not row["date_end"]
                or row["description"].partition("\n")[2] != f"Verified restart {verified_ref}"):
            raise RuntimeError("MES verified restart readback differs")
        return {**self._ref(EVENT, identifier), **row}

    def reject_interrupted(self, mo_id, fault_id, disposition_ref):
        native_id = self.call(MO, "norvale_reject_interrupted", [mo_id], fault_id, disposition_ref)
        if (native_id != mo_id or self.read(MO, mo_id)["state"] != "cancel"
                or self.read(EVENT, fault_id)["norvale_disposition"] != disposition_ref):
            raise RuntimeError("MES interrupted disposition readback differs")
        return self._ref(MO, mo_id)

    def export(self, work_refs, center_ids=()):
        """Project only the requested business records and their native relations."""
        if isinstance(work_refs, str):
            raise TypeError("Pass a sequence of ERP work references")
        roots = self.call(MO, "search", [
            ["origin", "in", list(work_refs)], ["norvale_revision", "!=", False],
        ], order="id")
        work = []
        for mo in self._rows(MO, roots):
            operations = self._rows(WO, mo["workorder_ids"])
            work.append({
                "record": self._ref(MO, mo["id"]), **mo,
                "operations": [{**self._ref(WO, row["id"]), **row} for row in operations],
                "labor": [{**self._ref(EVENT, row["id"]), **row}
                          for row in self._rows(EVENT, [t for op in operations for t in op["time_ids"]])],
                "completion": self.completion(mo["id"]) if mo["state"] == "done" else None,
            })
        centers = []
        for identifier in center_ids:
            events = self.call(EVENT, "search", [
                ["workcenter_id", "=", identifier], ["loss_type", "=", "availability"],
            ], order="id")
            centers.append({"equipment": self.availability(identifier),
                            "faults": [{**self._ref(EVENT, row["id"]), **row}
                                       for row in self._rows(EVENT, events)]})
        return {"source_system": "mes", "source_id": self.source_id, "work": work, "equipment": centers}
