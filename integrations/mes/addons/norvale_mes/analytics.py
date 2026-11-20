"""Attest the native reader before accepting an analytics scan."""

from hashlib import sha256

from odoo import api, models
from odoo.exceptions import AccessError


class Production(models.Model):
    _inherit = "mrp.production"

    @api.model
    def norvale_analytics_identity(self, company):
        user = self.env.user
        reader = self.env.ref("norvale_mes.group_analytics_reader").sudo()
        groups = self.env.ref("base.group_portal") | reader
        if (self.env.su or set(user.groups_id.ids) != set(groups.ids)
                or user.company_ids.ids != [company] or user.company_id.id != company):
            raise AccessError("Expected the commissioned Norvale MES analytics reader and company.")
        access = self.env["ir.model.access"]
        protected = reader.model_access.mapped("model_id.model")
        if (not protected or any(not access.check(model, "read", False) for model in protected)
                or any(access.check(model, mode, False)
                       for model in (*protected, "stock.quant", "stock.picking")
                       for mode in ("create", "write", "unlink"))):
            raise AccessError("MES analytics requires read-only manufacturing and inventory access.")
        native = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        if not native:
            raise AccessError("Database identity is unavailable.")
        return sha256(("norvale:odoo:" + native).encode()).hexdigest()
