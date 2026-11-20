"""Run only in the dedicated demo Odoo shell."""
precision = env['decimal.precision'].search([('name', '=', 'Product Unit of Measure')])
precision.write({'digits': 8})
env.ref('uom.product_uom_kgm').write({'rounding': 0.000001})
env.cr.commit()
