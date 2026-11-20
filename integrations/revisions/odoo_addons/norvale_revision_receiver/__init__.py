from hashlib import sha256

from odoo import api, fields, models
from odoo.exceptions import UserError


class Bom(models.Model):
    _inherit = "mrp.bom"

    norvale_import_key = fields.Char(copy=False, readonly=True)

    norvale_receiver_marker = fields.Char(
        compute="_compute_norvale_receiver_marker", compute_sudo=False, readonly=True,
        groups="mrp.group_mrp_user,norvale_mes.group_analytics_reader",
    )

    def _compute_norvale_receiver_marker(self):
        native = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        if not native:
            raise UserError("Database identity is unavailable.")
        marker = sha256(("norvale:odoo:" + native).encode()).hexdigest()
        for bom in self:
            bom.norvale_receiver_marker = marker

    @api.model_create_multi
    def create(self, vals_list):
        if ("default_norvale_receiver_marker" in self.env.context
                or any("norvale_receiver_marker" in vals for vals in vals_list)):
            raise UserError("Receiver marker is assigned by the database.")
        return super().create(vals_list)

    _sql_constraints = [
        ("norvale_import_key_unique", "unique(norvale_import_key)", "Revision import key already exists."),
    ]

    def write(self, vals):
        if "norvale_receiver_marker" in vals:
            raise UserError("Receiver marker is assigned by the database.")
        if "norvale_import_key" in vals:
            new_key = vals["norvale_import_key"] or False
            if any(bom.norvale_import_key != new_key for bom in self):
                raise UserError("Revision import key can only be assigned at BOM creation.")
        return super().write(vals)
