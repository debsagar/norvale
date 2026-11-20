"""Submit a product definition change between ordinary generator invocations."""
import argparse
from datetime import date, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path

from integrations.plm import Blocked, PLM, digest, quantity, required
from norvale.loader.erp_client import ErpClient
from .authority import Authority


def change_scope(run, item):
    try:
        bindings = json.loads((run / 'env/definition-changes.json').read_text())
    except FileNotFoundError:
        return None
    if not isinstance(bindings, dict):
        raise Blocked('Expected definition change bindings keyed by registered item')
    return bindings.get(item)


def change_actors(scope, cfg, product):
    if (not isinstance(scope, dict) or set(scope) != {'source', 'family', 'engineering', 'approver'}
            or scope['source'] != cfg['source'] or type(scope['family']) is not int
            or scope['family'] != product['family']):
        raise Blocked('Definition change binding must match the registered PLM source and family')
    actors = {role: scope[role] for role in ('engineering', 'approver')}
    for actor in actors.values():
        if not isinstance(actor, dict) or set(actor) != {'decision_actor', 'token_env'}:
            raise Blocked('Configure a native identity and token environment variable for each decision role')
        required(actor['decision_actor'])
        required(actor['token_env'])
    if (len({actor['decision_actor'] for actor in actors.values()} | {cfg['plm']['publisher']}) != 3
            or len({actor['token_env'] for actor in actors.values()}) != 2):
        raise Blocked('Definition changes require independent publisher, engineering and approver identities')
    return actors


def propose(plm, planner, product, item, revision, bom_id, on):
    """Derive the change from a submitted, planner-visible native ERP BOM."""
    revision = required(revision, 100)
    if revision == 'A-1':
        raise Blocked('A-1 is reserved for commissioning')
    plm.require_change_guard()
    existing = plm._find_part('NV-PLM-' + digest([item, revision]))
    if existing:
        prior = plm._meta(existing['pk']).get('norvale_proposal')
        if prior:
            if prior['on'] > on:
                raise Blocked('Proposal replay predates its recorded evidence')
            on = prior['on']
    pin = plm.pin(product['family'], 'proposal:' + item, on=on)
    base = plm.resolve_pin(pin, work_id=pin['work_id'], on=on)
    definition = base['definition']
    material_kind = product.get('material_kind', 'finished')
    if definition['name'] != item or definition.get('material_kind', 'finished') != material_kind:
        raise Blocked('Effective definition differs from the registered product or material kind')
    bom = planner.get('BOM', bom_id)
    target = product['mapping'][item]['erp']
    output = target['output']
    basis = Decimal(str(bom['quantity']))
    if (bom['name'] != bom_id or bom['item'] != item or bom['docstatus'] != 1
            or output['item'] != item or bom['company'] != target['settings']['company']
            or bom['uom'] != output['unit'] or not basis.is_finite() or basis <= 0):
        raise Blocked('Evidence must be a submitted BOM for this company, item and output unit')
    rows = {row['item_code']: row for row in bom['items']}
    current = definition['lines']
    mapped = {target['components'][line['source_id']]['item'] for line in current}
    if set(rows) != mapped or len(rows) != len(bom['items']) or len(mapped) != len(current):
        raise Blocked('Change must retain the existing mapped components')
    lines = []
    for line in current:
        mapping = target['components'][line['source_id']]
        row = rows[mapping['item']]
        if row['stock_uom'] != mapping['unit']:
            raise Blocked('Evidence BOM input has a different stock unit')
        amount = (Decimal(str(row['stock_qty'])) * Decimal(output['qty'])
                  / basis / Decimal(mapping['factor']))
        lines.append({**line, 'quantity': quantity(amount)})
    if all(new['quantity'] == old['quantity'] for new, old in zip(lines, current)):
        raise Blocked('Evidence BOM has no quantity change')
    evidence = dict(doctype='BOM', id=bom_id, version=required(bom['modified']), on=on,
                    base_id=pin['native_id'], base_version=pin['source_version'])
    return plm.draft(item, revision, lines, base_id=product['family'],
                     material_kind=material_kind, proposal=evidence)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    actions = parser.add_subparsers(dest='action', required=True)
    draft = actions.add_parser('propose')
    draft.add_argument('item')
    draft.add_argument('revision')
    draft.add_argument('bom')
    for action in ('review', 'approve', 'reject'):
        decision = actions.add_parser(action)
        decision.add_argument('part', type=int)
        decision.add_argument('evidence')
        decision.add_argument('--family', metavar='ITEM')
        decision.add_argument('--source', help='Require this configured PLM source for the selected family')
        if action == 'approve':
            decision.add_argument('--effective', required=True)
    args = parser.parse_args(argv)
    authority = Authority(args.run)
    settings = json.loads((args.run / 'config.json').read_text())
    if not settings.get('company_operating'):
        parser.error('Definition changes require an ordinary company operating run')
    state = json.loads((args.run / 'state.json').read_text())
    on = (date(2027, 1, 1) + timedelta(days=state['day'])).isoformat()
    item = args.item if args.action == 'propose' else args.family
    if item is None:
        cfg, product = authority.settings(), None
    else:
        cfg, product = authority.route(item)
    if args.action != 'propose' and args.source is not None and args.source != cfg['source']:
        raise Blocked('Selected family is on a different PLM source')
    actors = None
    if cfg is product:
        scope = change_scope(args.run, item)
        if scope is None:
            raise Blocked('Registered family uses external definition changes')
        actors = change_actors(scope, cfg, product)

    if args.action == 'propose':
        erp = cfg['erp']
        roles = json.loads((args.run / 'env/role-tokens.json').read_text())
        planner = ErpClient(erp['base'], erp['site'], roles['PLANNER'])
        print(propose(PLM(**cfg['plm']), planner, product, item, args.revision, args.bom, on))
    else:
        role = 'engineering' if args.action == 'review' else 'approver'
        if actors:
            actor = actors[role]['decision_actor']
            token = os.environ[actors[role]['token_env']]
            if token == cfg['plm']['token']:
                raise Blocked('A decision requires a separate native credential')
        else:
            actor = cfg.get('plm_commissioning', {}).get(role, {}).get('decision_actor', 'norvale-' + role)
            token = os.environ['PLM_DECISION_TOKEN']
        client = PLM(cfg['plm']['url'], token, publisher=cfg['plm']['publisher'], decision_actor=actor)
        client.require_change_guard()
        definition = client.definition(args.part)
        if item is None:
            product = cfg['products'].get(definition['name'])
        if (not product or (item is not None and item != definition['name'])
                or definition['revision_of'] != product['family'] or 'proposal' not in definition
                or definition.get('material_kind', 'finished') != product.get('material_kind', 'finished')):
            raise Blocked('Part is not a change proposal for the selected production family')
        decision = client._meta(args.part).get('norvale_' + {
            'review': 'review', 'approve': 'approval', 'reject': 'rejection',
        }[args.action])
        kwargs = dict(on=decision['on'] if decision else on, evidence=args.evidence)
        if args.action == 'approve':
            kwargs['effective'] = args.effective
        print(json.dumps(getattr(client, args.action)(args.part, **kwargs)))


if __name__ == '__main__':
    main()
