"""Isolated PLM -> native ERPNext + Odoo qualification with real receiver readback."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from integrations.company_run.check_definition_change import blocked, clients
from integrations.company_run.definition_change import propose
from integrations.mes.client import Mes
from integrations.plm import Blocked
from integrations.revisions import Bridge
from integrations.revisions.receivers import ERPNext, Odoo
from integrations.revisions.test_bridge import EARLY, LATER, PRODUCT, components, mapping
from native.company import Engine, save
from norvale.loader.erp_client import ErpClient, ErpError


def qualify(path, runtime, passwords):
    path.mkdir(parents=True, exist_ok=True)
    actors = clients(passwords)
    publisher, engineer, approver = (actors[k] for k in ('publisher', 'engineering', 'approver'))
    erp = ErpClient('http://127.0.0.1:43169', 'norvale')
    erp.login('Administrator', 'admin')
    ERPNext(erp).provision()
    company = 'Definition Change Qualification'
    if not erp.list('Warehouse Type', {'name': 'Transit'}):
        erp.create('Warehouse Type', {'name': 'Transit'})
    if not erp.list('Company', {'name': company}):
        erp.create('Company', dict(company_name=company, abbr='DCQ', country='United States',
                                   default_currency='USD'))
    if not erp.list('Item Group', {'name': 'All Item Groups'}):
        erp.create('Item Group', dict(item_group_name='All Item Groups', is_group=1))
    for unit, whole in (('Nos', 1), ('Kg', 0)):
        if not erp.list('UOM', {'name': unit}):
            erp.create('UOM', dict(uom_name=unit, must_be_whole_number=whole, enabled=1))
    item = PRODUCT + '-NATIVE'
    inputs = ('NV-BRIDGE-BULK', 'NV-BRIDGE-BOTTLE')
    for code, unit in ((item, 'Nos'), (inputs[0], 'Kg'), (inputs[1], 'Nos')):
        if not erp.list('Item', {'name': code}):
            erp.create('Item', dict(item_code=code, item_name=code, item_group='All Item Groups',
                                    stock_uom=unit, is_stock_item=1, valuation_rate=1))

    planner_id = 'definition-planner@example.test'
    if not erp.list('User', {'name': planner_id}):
        erp.create('User', dict(email=planner_id, first_name='Definition Planner', send_welcome_email=0,
                                roles=[dict(role=role) for role in ('Manufacturing User', 'Stock User')]))
    secret = erp.call('frappe.core.doctype.user.user.generate_keys', {'user': planner_id})['api_secret']
    planner = ErpClient(erp.base, erp.site, erp.get('User', planner_id)['api_key'] + ':' + secret)
    assert planner.call('frappe.auth.get_logged_user', {}) == planner_id

    mes = Mes(url='http://127.0.0.1:43168', db='norvale_mes', login='planner',
              password=(passwords / 'MES_TEST_PASSWORD').read_text().strip(),
              source_id='nvmes-product-change-43168')
    fixture = json.loads((runtime / 'mes-fixture.json').read_text())
    fg, raw, pack = [mes.call('product.product', 'search_read', [['default_code', '=', code]],
                               fields=['uom_id', 'company_id'], limit=2)[0]
                     for code in ('NV-SHAMPOO', 'NV-BULK', 'NV-BOTTLE')]
    mappings = mapping(company, fg['company_id'][0], fg['id'], raw['id'], pack['id'],
                       raw['uom_id'][0], fg['uom_id'][0], fixture['centers'][0])[PRODUCT]
    mappings['erp']['output']['item'] = item
    a = publisher.draft(item, 'A-1', components(1))
    if not publisher._meta(a).get('norvale_approval'):
        engineer.review(a, on=EARLY, evidence='Native A engineering review')
        approver.approve(a, on=EARLY, effective=EARLY, evidence='Native A commissioning')
    product = dict(family=a, publication=a, published=True, mapping={item: mappings})

    def bridge():
        return Bridge(publisher, ERPNext(planner), Odoo(mes), source='native-definition-change',
                      mapping=product['mapping'], checkpoint=path / 'native-pins.sqlite', scoped_receipts=True)

    first = bridge().publish(a, on=EARLY)
    print('Native baseline BOMs confirmed', a, flush=True)
    old = bridge().release(a, 'NV-WO-OLD', on=EARLY)['pin']
    plans, _ = bridge().installed(old, publisher.publication(a))
    planner_seen = ERPNext(planner).verify(first['erp']['bom_id'], first['erp']['import_key'],
                                         plans['erp'], scoped=True)
    assert planner_seen['receiver_marker'] == old['receiver_markers']['erp']
    field = 'custom_norvale_definition'
    if not erp.list('Custom Field', {'dt': 'Work Order', 'fieldname': field}):
        erp.create('Custom Field', dict(dt='Work Order', fieldname=field,
                                       label='Norvale Definition', fieldtype='Small Text',
                                       insert_after='bom_no', no_copy=1))
    warehouse_names = {'source': 'Stores - DCQ', 'wip': 'Work In Progress - DCQ',
                       'finished': 'Finished Goods - DCQ'}
    for warehouse in warehouse_names.values():
        assert erp.get('Warehouse', warehouse)['company'] == company

    def release_erp(pin):
        doc = planner.create('Work Order', dict(
            production_item=item, bom_no=pin['receiver_ids']['erp'], qty=4, company=company,
            use_multi_level_bom=0, skip_transfer=1, source_warehouse=warehouse_names['source'],
            wip_warehouse=warehouse_names['wip'], fg_warehouse=warehouse_names['finished'],
            planned_start_date=pin['released_on'] + ' 08:00:00', **{field: json.dumps(pin, sort_keys=True)}))
        planner.call('frappe.client.submit', {'doc': doc})
        return doc['name']

    erp_old = release_erp(old)
    print('Baseline ERP Work Order submitted', erp_old, flush=True)
    evidence = erp.create('BOM', dict(item=item, uom='Nos', quantity=1, company=company,
                                      is_active=1, is_default=0,
                                      items=[dict(item_code=inputs[0], uom='Kg', qty=.30, rate=1),
                                             dict(item_code=inputs[1], uom='Nos', qty=1, rate=1)]))
    erp.call('frappe.client.submit', {'doc': evidence})
    b = propose(publisher, planner, product, item, 'B', evidence['name'], EARLY)
    assert blocked(lambda: ErpClient(erp.base, erp.site).get('BOM', evidence['name']), ErpError).status in (401, 403)
    engineer.review(b, on=EARLY, evidence=evidence['name'])
    approver.approve(b, on=EARLY, effective=LATER, evidence='Native B authorized')
    assert publisher.pin(a, 'early', on=EARLY)['native_id'] == a
    blocked(lambda: bridge().release(a, 'NV-WO-NEW', on=LATER), Blocked)
    print('Effective successor blocks new work until receiver readback', b, flush=True)

    events = []
    authority = SimpleNamespace(clients=lambda _: (product, publisher, bridge(), mes))
    planning = []
    e = SimpleNamespace(run=path, state={'ops': {'orders': {}}}, date=lambda: LATER,
                        config={'company_operating': True}, additions=None,
                        company_erp=SimpleNamespace(authority=authority, state={'work': {}},
                                                    attempt=lambda _, __, act: act()),
                        plan_production=lambda: planning.append(
                            publisher.pin(a, 'planning', on=LATER)['native_id']),
                        log=lambda kind, **data: events.append((kind, data)))
    save(path / 'env/company-sources.json', {'products': {item: product}})
    Engine.production(e)
    Engine.production(e)
    assert len(events) == 1 and planning == [b, b]
    print('Ordinary production phase reconciled successor before planning', b, flush=True)
    new = bridge().release(a, 'NV-WO-NEW', on=LATER)['pin']
    assert new['native_id'] == b and (new['receiver_ids'] != old['receiver_ids'])
    erp_b = erp.get('BOM', new['receiver_ids']['erp'])
    assert erp_b['docstatus'] == 1 and next(r for r in erp_b['items'] if r['item_code'] == inputs[0])['qty'] == .30
    assert first['erp']['bom_id'] == old['receiver_ids']['erp']
    results = {}
    for pin, erp_work in ((old, erp_old), (new, release_erp(new))):
        order = planner.get('Work Order', erp_work)
        assert order['docstatus'] == 1 and json.loads(order[field]) == pin
        assert order['bom_no'] == pin['receiver_ids']['erp']
        bom = planner.get('BOM', order['bom_no'])
        assert bom['owner'] == planner_id and bom['docstatus'] == 1
        work = bridge().dispatch(pin['work_id'], on=LATER, quantity=4, hold_location_id=fixture['hold'])
        assert work['bom_id'][0] == int(pin['receiver_ids']['mes'])
        assert bridge().dispatch(pin['work_id'], on=LATER, quantity=4,
                                 hold_location_id=fixture['hold'])['id'] == work['id']
        results[pin['work_id']] = dict(pin=pin, erp_work=erp_work, mes_work=work['id'], bom_owner=bom['owner'])
    save(path / 'native-proof.json', dict(work=results, evidence=evidence['name'],
                                          publications=publisher.export(a, on=LATER)))
    print(json.dumps(results))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('proof', type=Path)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('passwords', type=Path)
    args = parser.parse_args()
    qualify(args.proof, args.runtime, args.passwords)
