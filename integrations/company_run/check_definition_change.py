"""Qualify native PLM decisions using an isolated site and receiver API fixtures."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

from integrations.company_run.definition_change import propose
from integrations.company_run.operating import Operating
from integrations.plm import Blocked, PLM, digest
from integrations.revisions import Bridge
from integrations.revisions.receivers import ERPNext, Odoo
from integrations.revisions.test_bridge import (
    EARLY, LATER, PRODUCT, FrappeFixture, MesFixture, components, mapping,
)
from native.company import save


def blocked(call, expected):
    try:
        call()
    except expected as error:
        return error
    raise AssertionError('Expected denial')


def clients(passwords):
    actors = {}
    for role in ('publisher', 'engineering', 'approver', 'reader'):
        user = 'norvale-' + role
        password = (passwords / ('plm-secret-' + role.upper())).read_text().strip()
        actors[role] = PLM('http://127.0.0.1:43167', user + ':' + password,
                           publisher='norvale-publisher', basic=True, decision_actor=user)
        assert actors[role].request('/api/user/me/')['username'] == user
    return actors


def create(path, actors):
    publisher, engineer, approver, reader = (actors[k] for k in
                                            ('publisher', 'engineering', 'approver', 'reader'))
    a = publisher.draft(PRODUCT, 'A-1', components(1))
    engineer.review(a, on=EARLY, evidence='Native baseline review')
    approver.approve(a, on=EARLY, effective=EARLY, evidence='Native commissioning decision')
    sent = FrappeFixture()
    mes = MesFixture()
    product = dict(family=a, publication=a, published=True,
                   mapping=mapping('Fixture Company', 1, 1, 2, 3, 4, 5, 6))

    def bridge():
        return Bridge(publisher, ERPNext(sent), Odoo(mes), source='definition-change-fixture',
                      mapping=product['mapping'], checkpoint=path / 'pins.sqlite')

    bridge().publish(a, on=EARLY)
    old = bridge().release(a, 'old-order', on=EARLY)['pin']
    evidence = dict(name='BOM-FIXTURE-EVIDENCE', item=PRODUCT, docstatus=1, modified='fixture-version',
                    company='Fixture Company', uom='Nos', quantity=1,
                    items=[dict(item_code='NV-BRIDGE-BULK', stock_uom='Kg', stock_qty=.3),
                           dict(item_code='NV-BRIDGE-BOTTLE', stock_uom='Nos', stock_qty=1)])
    planner = SimpleNamespace(get=lambda entity, name: evidence)
    b = propose(publisher, planner, product, PRODUCT, 'B', evidence['name'], EARLY)
    assert b == propose(publisher, planner, product, PRODUCT, 'B', evidence['name'], EARLY)
    rejected = propose(publisher, planner, product, PRODUCT, 'B-rejected', evidence['name'], EARLY)

    guard_path = f'/api/metadata/part/{b}/'
    part_path = f'/api/part/{b}/'
    checksum = digest(publisher.definition(b))
    review = dict(actor='norvale-engineering', evidence='Native review', on=EARLY, sha256=checksum)
    approval = dict(actor='norvale-approver', evidence='Native decision', on=EARLY,
                    effective=LATER, sha256=checksum)
    denied = {}

    def deny(label, client, method, uri, doc):
        err = blocked(lambda: client.request(uri, method, doc), HTTPError)
        assert err.code == 403, (label, err.code, err.read().decode())
        denied[label] = err.code

    deny('publisher review', publisher, 'PATCH', guard_path, {'metadata': {'norvale_review': review}})
    deny('reader review', reader, 'PATCH', guard_path, {'metadata': {'norvale_review': review}})
    deny('approver review', approver, 'PATCH', guard_path, {'metadata': {'norvale_review': review}})
    deny('engineering spoofed actor', engineer, 'PATCH', guard_path,
         {'metadata': {'norvale_review': {**review, 'actor': 'other-engineer'}}})
    engineer.review(b, on=EARLY, evidence=review['evidence'])
    deny('engineering approval', engineer, 'PATCH', guard_path, {'metadata': {'norvale_approval': approval}})
    deny('publisher approval', publisher, 'PATCH', guard_path, {'metadata': {'norvale_approval': approval}})
    deny('publisher lock', publisher, 'PATCH', part_path, {'locked': True})
    deny('publisher metadata replacement', publisher, 'PUT', guard_path, {'metadata': {}})
    approver.approve(b, on=EARLY, effective=LATER, evidence=approval['evidence'])
    deny('publisher unlock', publisher, 'PATCH', part_path, {'locked': False})
    deny('engineering BOM edit', engineer, 'PATCH',
         f"/api/bom/{publisher.definition(b)['lines'][0]['native_id']}/", {'quantity': 12})
    engineer.review(rejected, on=EARLY, evidence='Reviewed alternative proposal')
    approver.reject(rejected, on=EARLY, evidence='Rejected alternative proposal')
    blocked(lambda: publisher.publication(rejected), Blocked)
    blocked(lambda: approver.approve(rejected, on=EARLY, effective=LATER,
                                      evidence='Conflicting decision'), Blocked)
    assert publisher.pin(a, 'early-order', on=EARLY)['native_id'] == a

    flow_state = {}
    events = []

    def attempt(key, role, action):
        try:
            action()
        except TimeoutError:
            flow_state[key] = 'paused'
        else:
            flow_state.pop(key, None)

    engine = SimpleNamespace(run=path, state={'ops': {'orders': {}}},
                             company_erp=SimpleNamespace(attempt=attempt, authority=SimpleNamespace(
                                 clients=lambda item: (product, publisher, bridge(), mes))),
                             date=lambda: LATER, log=lambda kind, **data: events.append((kind, data)))
    (path / 'env').mkdir()
    save(path / 'env/company-sources.json', {'products': {PRODUCT: product}})
    create_mes = mes.call

    def interrupt(model, method, *args, **kwargs):
        if model == 'mrp.bom' and method == 'create':
            raise TimeoutError('fixture MES receiver interrupted')
        return create_mes(model, method, *args, **kwargs)

    mes.call = interrupt
    Operating(engine).definitions()
    assert flow_state and not events
    assert publisher._meta(b)['norvale_ack_erp']['native_id'] == 'BOM-2'
    assert 'norvale_ack_mes' not in publisher._meta(b)
    blocked(lambda: bridge().release(a, 'new-order', on=LATER), Blocked)
    assert publisher.resolve_pin(old, work_id='old-order', on=LATER)['native_id'] == a
    mes.call = create_mes
    Operating(engine).definitions()
    assert not flow_state and len(events) == 1
    Operating(engine).definitions()
    assert len(events) == 1
    new = bridge().release(a, 'new-order', on=LATER)['pin']
    assert new['native_id'] == b
    assert len(sent.boms) == len(mes.boms) == 2
    assert bridge().dispatch('old-order', on=LATER, quantity=4,
                             hold_location_id=6)['bom_id'] == int(old['receiver_ids']['mes'])
    assert bridge().dispatch('new-order', on=LATER, quantity=4,
                             hold_location_id=6)['bom_id'] == int(new['receiver_ids']['mes'])
    export = publisher.export(a, on=LATER)
    assert [record['native_id'] for record in export] == [a, b]
    assert export[1]['definition']['proposal']['id'] == evidence['name']
    save(path / 'proof.json', dict(old=old, new=new, rejected=rejected, denials=denied, export=export,
                                   receivers='Frappe and Odoo API fixtures'))
    print(json.dumps(dict(old=old, new=new, rejected=rejected, denials=denied)))


def restart(path, publisher):
    proof = json.loads((path / 'proof.json').read_text())
    for key in ('old', 'new'):
        pin = proof[key]
        assert publisher.resolve_pin(pin, work_id=pin['work_id'], on=LATER)['native_id'] == pin['native_id']
    assert publisher.export(proof['old']['native_id'], on=LATER) == proof['export']
    assert 'norvale_rejection' in publisher._meta(proof['rejected'])
    print('Restart preserved independent decisions, receipts, and both release pins')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create', 'restart'))
    parser.add_argument('proof', type=Path)
    parser.add_argument('passwords', type=Path)
    args = parser.parse_args()
    actors = clients(args.passwords)
    if args.action == 'create':
        create(args.proof, actors)
    else:
        restart(args.proof, actors['publisher'])
