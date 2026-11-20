"""Exercise a fractional bulk MO, native release transfer and packing MO on a private fixture."""
import json
import os
from pathlib import Path
import sys

from .client import Mes


def run(fixture):
    cfg = json.loads(Path(fixture).read_text())
    def client(role):
        return Mes(os.environ['MES_TEST_URL'], 'norvale_mes', role, os.environ['MES_TEST_PASSWORD'],
                   os.environ['MES_TEST_SOURCE'])
    planner, operator = client('planner'), client('operator')
    raw = planner.read('stock.lot', cfg['bulk'][0])['product_id'][0]
    existing, = planner.call('product.product', 'read', [raw], fields=['uom_id', 'company_id'])
    kg, company = existing['uom_id'][0], existing['company_id'][0]
    bulk = planner.call('product.product', 'create', dict(name='Bulk test', default_code='NV-BULK-TEST',
                        type='consu', is_storable=True, tracking='lot', company_id=company,
                        uom_id=kg, uom_po_id=kg))

    def recipe(product, qty, inputs, center):
        native, = planner.call('product.product', 'read', [product], fields=['product_tmpl_id', 'uom_id'])
        return planner.call('mrp.bom', 'create', dict(
            code='BULK-TEST-' + str(product), type='normal', product_tmpl_id=native['product_tmpl_id'][0],
            product_id=product, product_qty=qty, product_uom_id=native['uom_id'][0], company_id=company,
            bom_line_ids=[(0, 0, dict(product_id=i, product_uom_id=u, product_qty=q)) for i, u, q in inputs],
            operation_ids=[(0, 0, dict(name='Operation', workcenter_id=center, time_mode='manual',
                                      time_cycle_manual=1))]))

    def make(ref, product, bom, qty, inputs, name):
        revision = planner.acknowledge(bom)['revision']
        mo = planner.dispatch(ref, product, bom, revision, qty, cfg['hold'])['id']
        rows = [dict(lot_id=lot, quantity=q) for lot, q in inputs]
        ids = [lot for lot, q in inputs]
        operator.start(mo, rows, ids)
        output = planner.call('stock.lot', 'create', dict(name=name, product_id=product, company_id=company))
        result = operator.complete(mo, output, qty, rows, ids)
        return output, result

    bom = recipe(bulk, .75, [(raw, kg, 1)], cfg['centers'][0])
    lot, first = make('ERP-WO-BULK', bulk, bom, .75, [(cfg['bulk'][0], 1)], 'Bulk native lot')
    transfer = planner.release_intermediate(lot, 'NV-BULK-TEST', .75, cfg['stock'], origin='QMS bulk release')
    assert transfer == planner.release_intermediate(lot, 'NV-BULK-TEST', .75, cfg['stock'], origin='QMS bulk release')
    bottle = planner.read('stock.lot', cfg['bottle'])['product_id'][0]
    prod, = planner.call('product.product', 'read', [bottle], fields=['uom_id'])
    pack_bom = recipe(cfg['product'], 1, [(bulk, kg, .25), (bottle, prod['uom_id'][0], 1)], cfg['centers'][1])
    _, second = make('ERP-WO-PACK', cfg['product'], pack_bom, 3, [(lot, .75), (cfg['bottle'], 3)], 'Packed lot')
    assert {r['lot']['native_id'] for r in second['inputs']} == {first['output']['lot']['native_id'], cfg['bottle']}
    assert transfer == planner.release_intermediate(lot, 'NV-BULK-TEST', .75, cfg['stock'], origin='QMS bulk release')
    print(f"MO {first['work']['native_id']} produced lot {lot}, picking {transfer} released it, "
          f"MO {second['work']['native_id']} consumed it and packaging on moves "
          f"{[row['record']['native_id'] for row in second['inputs']]}")


if __name__ == '__main__':
    run(sys.argv[1])
