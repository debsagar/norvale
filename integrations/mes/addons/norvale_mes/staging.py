"""Post the authorized input quantity as a durable native stock movement."""

from odoo import fields, models
from odoo.exceptions import AccessError, UserError

from .execution import label, quantity, record


class StockMove(models.Model):
    _inherit = "stock.move"

    norvale_staging_receipt = fields.Char(copy=False)
    norvale_staging_order_id = fields.Many2one("mrp.production", copy=False, ondelete="restrict")
    _sql_constraints = [
        ("norvale_staging_receipt_unique", "unique(norvale_staging_receipt)",
         "A Norvale staging receipt can be posted only once."),
    ]


class StockLot(models.Model):
    _inherit = "stock.lot"

    def norvale_stage_input(self, receipt, location_id, amount, production_id=None):
        self.ensure_one()
        if not self.env.user.has_group("stock.group_stock_manager"):
            raise AccessError("Inventory manager required to stage production inputs")
        if not label(receipt).startswith("NV-STAGE-"):
            raise UserError("Expected a Norvale staging reference")
        self.check_access("read")
        location = record(self.env, "stock.location", location_id)
        company = self.company_id
        product = self.product_id
        amount = quantity(amount, product.uom_id)
        if (not company or company not in self.env.companies or location.company_id != company
                or not location.active or location.usage != "internal"
                or not product.is_storable or product.tracking != "lot"):
            raise UserError("Staging requires company-owned internal stock and a tracked lot")
        production = record(self.env, "mrp.production", production_id) if production_id is not None else None
        if production and (production.company_id != company or production.location_src_id != location
                           or product not in production.move_raw_ids.product_id):
            raise UserError("Staging input differs from the manufacturing order")
        source = product.with_company(company).property_stock_inventory
        if source.usage != "inventory" or (source.company_id and source.company_id != company):
            raise UserError("Invalid company inventory adjustment location")

        # The unique receipt field makes racing creates roll back before stock posts.
        moves = self.env["stock.move"]
        existing = moves.search([("norvale_staging_receipt", "=", receipt)])
        if existing:
            line = existing.move_line_ids
            if (len(existing) != 1 or existing.state != "done" or existing.origin != receipt
                    or (production and existing.norvale_staging_order_id != production)
                    or not existing.is_inventory or existing.company_id != company or len(line) != 1
                    or existing.product_uom != product.uom_id or existing.quantity != amount
                    or existing.product_uom_qty != amount
                    or line.lot_id != self or line.product_id != product
                    or line.product_uom_id != product.uom_id or line.quantity != amount
                    or line.location_id != source or line.location_dest_id != location
                    or line.package_id or line.result_package_id or line.owner_id):
                raise UserError("Conflicting native staging receipt")
            return line.id

        if production and production.state not in ("confirmed", "progress", "to_close"):
            raise UserError("New staging requires an active manufacturing order")

        move = moves.with_company(company).create({
            "name": receipt, "origin": receipt, "norvale_staging_receipt": receipt,
            "company_id": company.id, "norvale_staging_order_id": production.id if production else False,
            "product_id": product.id, "product_uom": product.uom_id.id,
            "product_uom_qty": amount, "location_id": source.id,
            "location_dest_id": location.id, "is_inventory": True,
            "move_line_ids": [(0, 0, {
                "company_id": company.id, "product_id": product.id,
                "product_uom_id": product.uom_id.id, "lot_id": self.id,
                "quantity": amount, "location_id": source.id, "location_dest_id": location.id,
            })],
        })
        move.picked = True
        move._action_done()
        return move.move_line_ids.id
