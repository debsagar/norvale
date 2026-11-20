"""Load only into a fresh isolated database via Odoo's native ORM shell."""

import json
import os

password = os.environ["MES_TEST_PASSWORD"]
if len(password) < 16:
    raise ValueError("Use a generated test password")
env.ref("base.user_admin").password = os.urandom(32).hex()
company = env.company
groups = env["res.groups"]
roles = {
    "planner": ["mrp.group_mrp_manager", "stock.group_stock_manager"],
    "maintenance": ["mrp.group_mrp_manager", "stock.group_stock_user"],
    "operator": ["mrp.group_mrp_user", "stock.group_stock_user"],
    "observer": [],
}
rework_password = os.environ.get("MES_REWORK_PASSWORD")
if rework_password:
    if len(rework_password) < 16 or rework_password == password:
        raise ValueError("Use a separate generated rework password")
    roles["rework"] = ["norvale_mes.group_rework_bridge"]

analytics_password = os.environ.get("MES_ANALYTICS_PASSWORD")
if analytics_password is not None and (len(analytics_password) < 16
                                       or analytics_password in (password, rework_password)):
    raise ValueError("Use a separate generated MES analytics password")

for role, xmlids in roles.items():
    assigned = groups.browse([env.ref(x).id for x in ["base.group_user", *xmlids]])
    env["res.users"].with_context(no_reset_password=True).create({
        "name": f"Norvale {role.title()}", "login": role,
        "password": rework_password if role == "rework" else password,
        "groups_id": [(6, 0, assigned.ids)], "company_id": company.id,
        "company_ids": [(6, 0, [company.id])],
    })
analytics = {}
if analytics_password is not None:
    reader = env["res.users"].with_context(no_reset_password=True).create({
        "name": "Norvale MES Analytics", "login": "analytics",
        "password": analytics_password,
        "groups_id": [(6, 0, [env.ref("base.group_portal").id,
                              env.ref("norvale_mes.group_analytics_reader").id])],
        "company_id": company.id, "company_ids": [(6, 0, [company.id])],
    })
    identity = env["mrp.production"].with_user(reader).norvale_analytics_identity(company.id)
    analytics = {"analytics_instance": identity, "analytics_company": company.id}
stock = env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1).lot_stock_id
hold = env["stock.location"].create({
    "name": "Norvale Output Hold", "location_id": stock.location_id.id,
    "usage": "internal", "company_id": company.id,
})
unit = env.ref("uom.product_uom_unit")
kg = env.ref("uom.product_uom_kgm")


def product(code, uom):
    return env["product.product"].create({
        "name": code, "default_code": code, "type": "consu", "is_storable": True,
        "tracking": "lot", "uom_id": uom.id, "uom_po_id": uom.id, "company_id": company.id,
    })


def lot(name, item):
    return env["stock.lot"].create({
        "name": name, "product_id": item.id, "company_id": company.id,
    })


mix = product("NV-BULK", kg)
shampoo = product("NV-SHAMPOO", unit)
bottles = product("NV-BOTTLE", unit)
mix_lots = [lot("BULK-104", mix), lot("BULK-105", mix)]
bottle_lot = lot("BOTTLE-663", bottles)
for source_lot, amount in zip([*mix_lots, bottle_lot], [5, 20, 100]):
    env["stock.quant"]._update_available_quantity(
        source_lot.product_id, stock, amount, lot_id=source_lot
    )
outputs = [lot(f"SHAMPOO-00{n}", shampoo) for n in range(1, 4)]
centers = env["mrp.workcenter"].create([
    {"name": "Norvale filler A", "code": "NV-A", "company_id": company.id},
    {"name": "Norvale filler B", "code": "NV-B", "company_id": company.id},
])
boms = env["mrp.bom"].create([
    {
        "code": f"PLM-SHAMPOO-R{n}", "product_tmpl_id": shampoo.product_tmpl_id.id,
        "product_id": shampoo.id, "product_qty": 1, "product_uom_id": unit.id,
        "consumption": "flexible", "company_id": company.id,
        "bom_line_ids": [(0, 0, {"product_id": mix.id, "product_qty": 0.5, "product_uom_id": kg.id}),
                         (0, 0, {"product_id": bottles.id, "product_qty": 1, "product_uom_id": unit.id})],
        "operation_ids": [(0, 0, {"name": "Fill", "workcenter_id": center.id,
                                 "time_mode": "manual", "time_cycle_manual": 1})],
    }
    for n, center in enumerate(centers, 1)
])
env.cr.commit()
print(json.dumps({
    "product": shampoo.id, "hold": hold.id, "stock": stock.id, "boms": boms.ids,
    "centers": centers.ids, "bulk": [row.id for row in mix_lots], "bottle": bottle_lot.id,
    "output": [row.id for row in outputs], **analytics,
}))
