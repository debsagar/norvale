"""Native execution of a QMS-authorized one-lot rework in one transaction."""

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .execution import label, quantity, record


class Rework(models.Model):
    _inherit = "mrp.production"

    norvale_rework_of = fields.Many2one("mrp.production", copy=False, readonly=True, check_company=True)
    norvale_rework_authority = fields.Char(copy=False, readonly=True)
    norvale_rework_scrap = fields.Many2one("stock.scrap", copy=False, readonly=True, check_company=True)

    _sql_constraints = [
        ("norvale_rework_once", "unique(norvale_rework_of)", "This failed execution already has a rework."),
    ]

    @api.model
    def norvale_rework(self, source_id, authority, total, salvage):
        # The bridge account authenticates and reads QMS. Shop-floor identities
        # cannot use this entry point to declare their own disposition.
        if not self.env.user.has_group("norvale_mes.group_rework_bridge"):
            raise AccessError("QMS rework bridge authority required")
        source = record(self.env, "mrp.production", source_id)
        authority = label(authority)
        product, uom, hold = source.product_id, source.product_uom_id, source.location_dest_id
        total, salvage = quantity(total, uom), quantity(salvage, uom)
        if salvage > total:
            raise UserError("Salvage exceeds the failed lot quantity")
        loss = round(total - salvage, 9)
        existing = self.search([("norvale_rework_of", "=", source.id)])
        if existing:
            if ((existing.norvale_rework_authority, existing.product_qty,
                 existing.norvale_rework_scrap.scrap_qty or 0) != (authority, salvage, loss)
                    or existing.state != "done"):
                raise UserError("Conflicting rework replay")
            return existing.id

        finished = source.move_finished_ids.move_line_ids.filtered(lambda line: line.state == "done")
        lot = source.lot_producing_id
        center = source.workorder_ids.workcenter_id
        if (source.state != "done" or not source.norvale_revision or source.company_id != self.env.company
                or source.norvale_rework_of or len(center) != 1 or not lot or len(finished) != 1
                or finished.lot_id != lot or finished.quantity != total
                or finished.location_dest_id != hold or finished.product_uom_id != uom
                or uom != product.uom_id or product.tracking != "lot" or hold.usage != "internal"
                or hold.company_id != source.company_id):
            raise UserError("Rework needs the exact original MES output and hold location")
        live = self.env["stock.quant"]._get_available_quantity(
            product, hold, lot_id=lot, strict=True, allow_negative=True
        )
        if abs(live - total) > 1e-9:
            raise UserError("Full unreserved failed lot must be in MES hold")
        if center.working_state != "normal" or self.env["mrp.workorder"].search_count([
            ("workcenter_id", "=", center.id), ("state", "=", "progress"),
        ]):
            raise UserError("Rework equipment unavailable")

        name = f"NV/RW/{source.id}"
        mo = self.create({
            "name": name, "origin": source.name, "norvale_rework_of": source.id,
            "norvale_rework_authority": authority, "norvale_revision": source.norvale_revision,
            "product_id": product.id, "product_uom_id": uom.id, "product_qty": salvage,
            "bom_id": False, "location_src_id": hold.id, "location_dest_id": hold.id,
            "picking_type_id": source.picking_type_id.id,
        })
        output = self.env["stock.lot"].create({
            "name": name, "product_id": product.id, "company_id": source.company_id.id,
        })
        raw = self.env["stock.move"].create(mo._get_move_raw_values(product, salvage, uom))
        wo = self.env["mrp.workorder"].create({
            "name": "Rework failed lot", "production_id": mo.id, "workcenter_id": center.id,
            "product_uom_id": uom.id,
        })
        mo.action_confirm()
        mo.write({"qty_producing": salvage, "lot_producing_id": output.id})
        grouped = mo._norvale_available({lot.id: salvage}, [lot.id], full_plan=True)
        mo._norvale_set_inputs(grouped, picked=False)
        wo.button_start()
        mo._norvale_set_inputs(grouped, picked=True)
        mo.with_context(skip_backorder=True, skip_redirection=True).button_mark_done()
        mo._norvale_verify_completion(output, salvage, {lot.id: salvage})
        if raw.location_id != hold or wo.state != "done":
            raise UserError("Native rework execution did not close")
        if loss:
            scrap = self.env["stock.scrap"].create({
                "origin": mo.name, "production_id": mo.id, "workorder_id": wo.id,
                "product_id": product.id, "product_uom_id": uom.id, "lot_id": lot.id,
                "location_id": hold.id, "company_id": source.company_id.id, "scrap_qty": loss,
            })
            # Link the scrap using its native MO/WO references. Odoo excludes
            # scrapped moves from the MO's finished-output relation.
            scrap.action_validate()
            if scrap.state != "done":
                raise UserError("Native scrap did not close")
            mo.norvale_rework_scrap = scrap
        remaining = self.env["stock.quant"]._get_available_quantity(
            product, hold, lot_id=lot, strict=True, allow_negative=True
        )
        if abs(remaining) > 1e-9:
            raise UserError("Rework and scrap did not exhaust the held lot")
        return mo.id
