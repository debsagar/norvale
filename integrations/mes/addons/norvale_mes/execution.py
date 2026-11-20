"""Atomic bridge calls over Odoo's existing Manufacturing and Inventory models."""

from collections import defaultdict
from hashlib import sha256
import json
import math

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare, float_round


def require(env, group="group_mrp_user"):
    if not env.user.has_group(f"mrp.{group}"):
        raise AccessError("Manufacturing authority required")


def record(env, model, identifier):
    if type(identifier) is not int or identifier <= 0:
        raise UserError(f"Invalid {model} ID")
    result = env[model].browse(identifier).exists()
    if len(result) != 1:
        raise UserError(f"Unknown {model} record")
    result.check_access("read")
    return result


def label(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or "\n" in value or "\r" in value or len(value) > 160):
        raise UserError("Expected a nonblank reference of up to 160 characters")
    return value


def quantity(value, uom):
    if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0
            or not math.isclose(float_round(value, precision_rounding=uom.rounding),
                                value, rel_tol=0, abs_tol=1e-9)):
        raise UserError(f"Enter a positive quantity in {uom.name} at native precision")
    return float(value)


def revision(bom):
    """Fingerprint the supported execution definition, including the external revision ref."""
    lines = bom.bom_line_ids
    if (not bom.code or bom.type != "normal" or not bom.active or bom.byproduct_ids
            or len(bom.operation_ids) != 1 or not lines
            or len(lines.product_id) != len(lines)
            or bom.product_qty <= 0
            or any(line.product_qty <= 0 or line.product_uom_id != line.product_id.uom_id
                   or line.product_id.tracking != "lot" or not line.product_id.is_storable
                   or line.bom_product_template_attribute_value_ids for line in lines)):
        raise UserError("Revision requires one routed, lot-tracked BoM without variants or byproducts")
    op = bom.operation_ids
    definition = [bom.code, bom.product_tmpl_id.id, bom.product_id.id, bom.product_qty,
                  bom.product_uom_id.id, bom.consumption, op.id, op.name, op.workcenter_id.id,
                  op.time_mode, op.time_cycle_manual,
                  sorted((line.product_id.id, line.product_qty, line.product_uom_id.id)
                         for line in lines)]
    digest = sha256(json.dumps(definition, separators=(",", ":")).encode()).hexdigest()
    return f"{bom.code}@{digest}"


class Production(models.Model):
    _inherit = "mrp.production"

    # Stored on the native MO and copied by Odoo to its backorders.
    norvale_revision = fields.Char(copy=True, readonly=True)

    @api.model
    def norvale_acknowledge(self, bom_id):
        require(self.env)
        bom = record(self.env, "mrp.bom", bom_id)
        return {"bom_id": bom.id, "revision": revision(bom)}

    @api.model
    def norvale_dispatch(self, work_ref, product_id, bom_id, acknowledged, qty, hold_location_id):
        require(self.env, "group_mrp_manager")
        work_ref = label(work_ref)
        # Odoo's name/company uniqueness prevents duplicate initial dispatch.
        # Odoo renames the MO on split. Its origin and root sequence survive.
        key = sha256(work_ref.encode()).hexdigest()[:32]
        name = f"NV/{key}"
        prod = record(self.env, "product.product", product_id)
        bom = record(self.env, "mrp.bom", bom_id)
        hold = record(self.env, "stock.location", hold_location_id)
        qty = quantity(qty, prod.uom_id)
        existing = self.search([
            ("origin", "=", work_ref), ("company_id", "=", self.env.company.id),
            ("backorder_sequence", "in", [0, 1]), ("norvale_revision", "!=", False),
        ], limit=2)
        if len(existing) > 1:
            raise UserError("Ambiguous work dispatch reference")
        if existing:
            family = existing._norvale_family()
            if ((existing.origin, existing.product_id, existing.bom_id,
                 existing.norvale_revision, existing.location_dest_id)
                    != (work_ref, prod, bom, acknowledged, hold)
                    or float_compare(sum(family.mapped("product_qty")), qty,
                                     precision_rounding=prod.uom_id.rounding)):
                raise UserError("Conflicting work dispatch replay")
            return existing.id
        if (acknowledged != revision(bom) or prod.product_tmpl_id != bom.product_tmpl_id
                or (bom.product_id and bom.product_id != prod)
                or not prod.is_storable or prod.tracking != "lot" or bom.product_uom_id != prod.uom_id):
            raise UserError("Product or acknowledged revision differs from the pinned definition")
        mo = self.create({
            "name": name, "origin": work_ref, "product_id": prod.id,
            "bom_id": bom.id, "norvale_revision": acknowledged, "product_qty": qty,
            "location_dest_id": hold.id,
        })
        if (hold.usage != "internal" or hold.company_id != mo.company_id
                or hold == mo.location_src_id or hold.parent_path.startswith(mo.location_src_id.parent_path)
                or mo.warehouse_id.manufacture_steps != "mrp_one_step"):
            raise UserError("Require one-step manufacture to a dedicated company hold location")
        mo.action_confirm()
        if len(mo.workorder_ids) != 1 or not mo.move_raw_ids:
            raise UserError("Expected one native work order and component moves")
        return mo.id

    def _norvale_family(self):
        return self.procurement_group_id.mrp_production_ids or self

    def _norvale_current(self):
        self.ensure_one()
        self.check_access("write")
        if not self.norvale_revision or self.norvale_revision != revision(self.bom_id):
            raise UserError("Pinned revision is missing or has changed")
        wo = self.workorder_ids
        if len(wo) != 1 or wo.workcenter_id != self.bom_id.operation_ids.workcenter_id:
            raise UserError("Pinned work center has changed")
        return wo

    def _norvale_inputs(self, inputs):
        actual = {}
        for row in inputs:
            lot = record(self.env, "stock.lot", row["lot_id"])
            if lot.company_id and lot.company_id != self.company_id:
                raise UserError("Input lot belongs to another company")
            if lot.id in actual:
                raise UserError("Each input lot must occur once")
            actual[lot.id] = quantity(row["quantity"], lot.product_id.uom_id)
        if not actual:
            raise UserError("Explicit lot quantities are required")
        return actual

    def _norvale_available(self, actual, released, full_plan=False):
        if not actual.keys() <= set(released):
            raise UserError("Input lot is held or lacks release evidence")
        moves = {m.product_id.id: m for m in self.move_raw_ids if m.state not in ("done", "cancel")}
        lots = self.env["stock.lot"].browse(list(actual))
        if set(moves) != set(lots.product_id.ids):
            raise UserError("Allocations must cover exactly the pinned component products")
        grouped = defaultdict(list)
        for lot in lots:
            move = moves[lot.product_id.id]
            amount = actual[lot.id]
            own = move.move_line_ids.filtered(
                lambda line: line.lot_id == lot and line.location_id == move.location_id
                and not line.package_id and not line.owner_id
            )
            free = self.env["stock.quant"]._get_available_quantity(
                lot.product_id, move.location_id, lot_id=lot, strict=True, allow_negative=True
            )
            if float_compare(amount, free + sum(own.mapped("quantity")),
                             precision_rounding=lot.product_id.uom_id.rounding) > 0:
                raise UserError(f"Insufficient available quantity from lot {lot.name}")
            grouped[move].append((lot, amount))
        if full_plan and any(float_compare(sum(amount for _, amount in rows), move.product_uom_qty,
                                           precision_rounding=move.product_uom.rounding)
                             for move, rows in grouped.items()):
            raise UserError("Start allocation must match the full component plan")
        return grouped

    def _norvale_set_inputs(self, grouped, picked):
        # Odoo leaves picked move lines alone on unreserve, so clear picked first.
        self.move_raw_ids.picked = False
        self.do_unreserve()
        for move, rows in grouped.items():
            for lot, amount in rows:
                self.env["stock.move.line"].create({
                    "move_id": move.id, "product_id": move.product_id.id, "lot_id": lot.id,
                    "product_uom_id": move.product_uom.id, "quantity": amount,
                    "location_id": move.location_id.id, "location_dest_id": move.location_dest_id.id,
                    "company_id": move.company_id.id,
                })
            move.picked = picked

    def norvale_start(self, inputs, released_lot_ids):
        require(self.env)
        wo = self._norvale_current()
        wo.workcenter_id._norvale_serialize()
        actual = self._norvale_inputs(inputs)
        grouped = self._norvale_available(actual, released_lot_ids, full_plan=True)
        if wo.workcenter_id.working_state == "blocked":
            raise UserError("Work order or allocated equipment is unavailable")
        if self.env["mrp.workcenter.productivity"].search_count([
            ("norvale_interrupted_id", "=", wo.id),
        ]):
            raise UserError("Interrupted work requires a separate batch disposition")
        if wo.state == "progress":
            reserved = {line.lot_id.id: line.quantity for line in self.move_raw_ids.move_line_ids}
            if reserved != actual:
                raise UserError("Conflicting start replay")
            return wo.id
        occupied = self.env["mrp.workorder"].search_count([
            ("workcenter_id", "=", wo.workcenter_id.id), ("state", "=", "progress"),
        ])
        if wo.state != "ready" or wo.workcenter_id.working_state != "normal" or occupied:
            raise UserError("Work order or allocated equipment is unavailable")
        # Set the MO quantity directly so button_start does not replace our
        # allocations through the WO quantity inverse's auto-consumption logic.
        self.qty_producing = self.product_qty
        self._norvale_set_inputs(grouped, picked=False)
        previous = wo.time_ids
        wo.button_start()
        timer = wo.time_ids - previous
        if len(timer) == 1 and timer.user_id == self.env.user and timer.loss_type == "productive":
            timer._norvale_mark_start()
        reserved = {line.lot_id.id: line.quantity for line in self.move_raw_ids.move_line_ids}
        if wo.state != "progress" or reserved != actual:
            raise UserError("Native work order did not start with the requested lots")
        return wo.id

    def norvale_complete(self, output_lot_id, qty, inputs, released_lot_ids):
        require(self.env)
        self.ensure_one()
        self.check_access("write")
        lot = record(self.env, "stock.lot", output_lot_id)
        qty = quantity(qty, self.product_uom_id)
        actual = self._norvale_inputs(inputs)
        if self.state == "done":
            self._norvale_verify_completion(lot, qty, actual)
            return self.id
        wo = self._norvale_current()
        wo.workcenter_id._norvale_serialize()
        if wo.state != "progress" or wo.workcenter_id.working_state == "blocked":
            raise UserError("Completion requires running work and operable equipment")
        if (lot.product_id != self.product_id or lot.company_id not in
                (self.env["res.company"], self.company_id)
                or float_compare(qty, self.product_qty, precision_rounding=self.product_uom_id.rounding) > 0):
            raise UserError("Invalid output lot or quantity")
        if self.env["stock.move.line"].search_count([
            ("lot_id", "=", lot.id), ("move_id.production_id", "!=", False), ("state", "=", "done")
        ]):
            raise UserError("Output lot already has a completed production")
        if self.env["mrp.workcenter.productivity"].search_count([
            ("norvale_interrupted_id", "=", wo.id),
        ]):
            raise UserError("Interrupted work requires a separate batch disposition")
        if wo.time_ids and self.env["mrp.workcenter.productivity"].search_count([
            ("workcenter_id", "=", wo.workcenter_id.id), ("loss_type", "=", "availability"),
            ("id", ">", min(wo.time_ids.ids)), ("description", "=like", "Fault %"),
        ]):
            raise UserError("Interrupted work requires a separate batch disposition")
        grouped = self._norvale_available(actual, released_lot_ids)
        self.write({"lot_producing_id": lot.id, "qty_producing": qty})
        self._norvale_set_inputs(grouped, picked=True)
        partial = float_compare(qty, self.product_qty, precision_rounding=self.product_uom_id.rounding) < 0
        self.with_context(skip_backorder=True, skip_redirection=True,
                          mo_ids_to_backorder=self.ids if partial else []).button_mark_done()
        self._norvale_verify_completion(lot, qty, actual)
        return self.id

    def norvale_complete_scheduled(self, output_lot_id, qty, inputs, released_lot_ids, labor):
        """Commit output and its business-minute evidence in one native transaction."""
        require(self.env)
        self.ensure_one()
        self.check_access("write")
        wo = self.workorder_ids
        wo.ensure_one()
        wo.workcenter_id._norvale_serialize()
        labor = self._norvale_labor_envelope(wo, labor)
        timer = wo.time_ids.filtered("norvale_bridge_started")
        if (len(timer) != 1 or timer.user_id != self.env.user or timer.create_uid != self.env.user
                or timer.workcenter_id != wo.workcenter_id):
            raise UserError("Scheduled completion requires the original production operator and timer")
        if self.state == "done":
            if timer.norvale_scheduled_labor != labor:
                raise UserError("Completed work has different or missing scheduled labor")
        elif (wo.time_ids != timer or timer.date_end or timer.loss_type != "productive"
                or timer.norvale_scheduled_labor):
            raise UserError("Scheduled completion requires the original open productive work")
        replay = self.state == "done"
        self.norvale_complete(output_lot_id, qty, inputs, released_lot_ids)
        if not replay:
            if not timer.date_end or wo.state != "done":
                raise UserError("Native productivity timer did not close with the output")
            timer._norvale_record_labor(labor)
        return timer.id

    def _norvale_labor_envelope(self, wo, labor):
        if not isinstance(labor, dict) or labor.keys() != {
                "event", "role", "center", "dispatch", "source", "work_order", "intervals", "minutes"}:
            raise UserError("Expected the original scheduled production envelope")
        event, source = labor["event"], label(labor["source"])
        center = wo.workcenter_id.id
        supplied_center = labor["center"]
        matching_center = ((type(supplied_center) is int and supplied_center == center)
                           or supplied_center == f"{source}:{center}")
        if (labor["role"] != "PRODUCTION" or labor["work_order"] != self.origin
                or type(labor["dispatch"]) not in (int, str) or str(labor["dispatch"]) != str(self.id)
                or not matching_center
                or ":" in source or not isinstance(event, str) or len(event) > 1024
                or not event.startswith("company:")):
            raise UserError("Scheduled labor does not match the native dispatch")
        try:
            job, edge, attempt = json.loads(event.removeprefix("company:"))
            valid_event = (isinstance(job, list) and len(job) == 5 and job[:2] == ["mes", source]
                           and job[2] == labor["dispatch"] and edge == "complete"
                           and type(attempt) is int and attempt == 0)
        except (ValueError, TypeError):
            valid_event = False
        if not valid_event:
            raise UserError("Scheduled labor requires the original completion event")
        intervals = labor["intervals"]
        if not isinstance(intervals, list) or not intervals:
            raise UserError("Scheduled labor needs productive intervals")
        previous = total = 0
        for interval in intervals:
            if (not isinstance(interval, list) or len(interval) != 2
                    or any(type(value) is not int for value in interval)
                    or not previous <= interval[0] < interval[1]):
                raise UserError("Expected ordered, nonoverlapping business-minute intervals")
            start, previous = interval
            total += previous - start
        if type(labor["minutes"]) is not int or labor["minutes"] != total:
            raise UserError("Scheduled business minutes differ from the intervals")
        return labor

    def norvale_reject_interrupted(self, fault_id, disposition_ref):
        require(self.env, "group_mrp_manager")
        self.ensure_one()
        self.check_access("write")
        fault = record(self.env, "mrp.workcenter.productivity", fault_id)
        if (not self.norvale_revision or fault.norvale_interrupted_id.production_id != self
                or fault.norvale_interrupted_id.workcenter_id != fault.workcenter_id
                or self.state == "done"
                or any(move.state == "done" for move in self.move_raw_ids | self.move_finished_ids)):
            raise UserError("Reject requires matching interrupted work without posted movements")
        fault.workcenter_id._norvale_serialize()
        evidence = label(disposition_ref)
        if fault.norvale_disposition and fault.norvale_disposition != evidence:
            raise UserError("Conflicting interrupted batch disposition")
        if self.state == "cancel" and not fault.norvale_disposition:
            raise UserError("Interrupted work was cancelled outside the disposition workflow")
        if self.state != "cancel":
            self.action_cancel()
            if self.state != "cancel":
                raise UserError("Interrupted work cancellation did not persist")
            fault.norvale_disposition = evidence
        return self.id

    def _norvale_verify_completion(self, lot, qty, actual):
        lines = self.move_raw_ids.move_line_ids.filtered(lambda l: l.state == "done")
        consumed = defaultdict(float)
        for line in lines:
            consumed[line.lot_id.id] += line.quantity
        finished = self.move_finished_ids.move_line_ids.filtered(lambda l: l.state == "done")
        if (not self.norvale_revision or self.state != "done" or len(finished) != 1
                or finished.lot_id != lot or finished.quantity != qty
                or finished.location_dest_id != self.location_dest_id
                or set(consumed) != set(actual)
                or any(not math.isclose(consumed[k], value, rel_tol=0, abs_tol=1e-9)
                       for k, value in actual.items())
                or set(finished.consume_line_ids.ids) != set(lines.ids)):
            raise UserError("Native completion or replay differs from the requested lot movements")


class Productivity(models.Model):
    _inherit = "mrp.workcenter.productivity"

    norvale_interrupted_id = fields.Many2one("mrp.workorder", ondelete="restrict", copy=False)
    norvale_disposition = fields.Char(copy=False)
    norvale_bridge_started = fields.Boolean(readonly=True, copy=False)
    norvale_scheduled_labor = fields.Json(string="Scheduled labor (business minutes)", readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        protected = ("norvale_bridge_started", "norvale_scheduled_labor")
        if (any(set(protected) & vals.keys() for vals in vals_list)
                or any(self.env.context.get(f"default_{key}") for key in protected)):
            raise AccessError("Scheduled labor is owned by native MES execution")
        return super().create(vals_list)

    def write(self, vals):
        attribution = {"workorder_id", "workcenter_id", "company_id", "user_id", "date_start"}
        if ({"norvale_bridge_started", "norvale_scheduled_labor"} & vals.keys()
                or (attribution & vals.keys() and any(self.mapped("norvale_bridge_started")))
                or ({"date_end", "loss_id"} & vals.keys() and any(self.mapped("norvale_scheduled_labor")))):
            raise AccessError("Scheduled labor attribution is owned by native MES execution")
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _norvale_keep_scheduled_labor(self):
        if any(self.mapped("norvale_scheduled_labor")):
            raise UserError("Productivity with posted scheduled labor cannot be deleted")

    def _norvale_mark_start(self):
        super().write({"norvale_bridge_started": True})

    def _norvale_record_labor(self, labor):
        super().write({"norvale_scheduled_labor": labor})


class Workcenter(models.Model):
    _inherit = "mrp.workcenter"

    norvale_execution_epoch = fields.Boolean(copy=False)

    def _norvale_serialize(self):
        # Force concurrent bridge operations through Odoo's transaction retry
        # on the same equipment row. Operators only touch this internal bit.
        self.ensure_one()
        self.check_access("read")
        self.sudo().write({"norvale_execution_epoch": not self.norvale_execution_epoch})

    def norvale_fault(self, maintenance_ref):
        require(self.env, "group_mrp_manager")
        self.ensure_one()
        self.check_access("write")
        self._norvale_serialize()
        events = self.env["mrp.workcenter.productivity"]
        marker = "Fault " + label(maintenance_ref)
        existing = events.search([("workcenter_id", "=", self.id)]).filtered(
            lambda row: (row.description or "").partition("\n")[0] == marker
        )
        if existing:
            return existing.id
        if self.working_state == "blocked":
            raise UserError("Equipment is already blocked")
        running = self.env["mrp.workorder"].search([
            ("workcenter_id", "=", self.id), ("state", "=", "progress"),
        ], limit=2)
        if len(running) > 1:
            raise UserError("Multiple concurrent work orders on one equipment")
        # Close timers while preserving the interrupted WO's native progress state.
        events.search([("workcenter_id", "=", self.id), ("date_end", "=", False)]).write(
            {"date_end": fields.Datetime.now()}
        )
        reason = self.env.ref("mrp.block_reason0", raise_if_not_found=False)
        if not reason or reason.loss_type != "availability":
            reason = self.env["mrp.workcenter.productivity.loss"].search(
                [("loss_type", "=", "availability")], limit=1
            )
        if not reason:
            raise UserError("No native availability loss reason is configured")
        return events.create({"workcenter_id": self.id, "loss_id": reason.id, "description": marker,
                              "norvale_interrupted_id": running.id}).id

    def norvale_restart(self, fault_id, verified_ref):
        require(self.env, "group_mrp_manager")
        self.ensure_one()
        self.check_access("write")
        self._norvale_serialize()
        fault = record(self.env, "mrp.workcenter.productivity", fault_id)
        suffix = "\nVerified restart " + label(verified_ref)
        if (fault.workcenter_id != self or fault.loss_type != "availability"
                or not fault.description or not fault.description.startswith("Fault ")):
            raise UserError("Restart requires a matching MES equipment fault")
        if fault.date_end:
            if fault.description.partition("\n")[2] != suffix[1:]:
                raise UserError("Conflicting restart replay")
        else:
            fault.write({"date_end": fields.Datetime.now(), "description": fault.description + suffix})
        return fault.id
