"""Exercise the decision CLI and ordinary daily publication with native API fixtures."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from hashlib import sha256
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from integrations.company_run import definition_change
from integrations.company_run.authority import Authority
from integrations.plm import Blocked, PLM
from integrations.revisions.receivers import ERPNext
from integrations.revisions.test_bridge import EARLY, LATER, PRODUCT, FrappeFixture, MesFixture, components, mapping
from native.company import Engine, save
from native.company_erp import CompanyERP, Pending
from native.production_profiles import ADAPTER, profile


class FixturePLM(PLM):
    """Keep PLM transition, digest, effectivity and receipt logic in the real client."""
    def __init__(self, store, token, **kwargs):
        super().__init__('http://plm.test', token, **kwargs)
        self.store = store

    def request(self, path, method='GET', data=None, *, require_guard=False):
        if require_guard and not self.store.guard:
            raise Blocked('Fixture guard disabled')
        if path == '/api/':
            return {}
        if path == '/api/user/me/':
            return {'username': self.auth.removeprefix('Token ')}
        if path == '/api/bom/':
            part = self.store.parts[data['sub_part']]
            self.store.lines.setdefault(data['part'], []).append(dict(
                native_id=len(self.store.lines.get(data['part'], [])) + 1, component_id=part['pk'],
                source_id=part['IPN'], unit=part['units'], kind=data['reference'],
                quantity=data['quantity'], specification=data['note']))
            return {}
        if method == 'PATCH':
            self.store.parts[int(path.split('/')[-2])].update(data)
            return {}
        raise AssertionError(path)

    def _ensure_part(self, fields):
        part = self._find_part(fields['IPN'])
        if part is None:
            pk = len(self.store.parts) + 1
            part = self.store.parts[pk] = dict(fields, pk=pk, locked=False)
            self.store.meta[pk] = {}
        assert all(part[k] == v for k, v in fields.items())
        return part['pk']

    def _list(self, path, **filters):
        assert path == '/api/part/'
        for part in self.store.parts.values():
            if all(part.get(k) == v for k, v in filters.items()):
                yield deepcopy(part)

    def _part(self, pk):
        return deepcopy(self.store.parts[pk])

    def _bom(self, pk):
        return deepcopy(self.store.lines.get(pk, []))

    def _meta(self, pk, update=None):
        if update:
            self.store.meta[pk].update(deepcopy(update))
        return deepcopy(self.store.meta[pk])


class EastMES(MesFixture):
    def call(self, model, method, *args, **kwargs):
        if (model, method) == ('mrp.bom', 'create') and self.interrupt:
            self.interrupt = False
            super().call(model, method, *args, **kwargs)
            raise TimeoutError('MES committed before losing the reply')
        rows = super().call(model, method, *args, **kwargs)
        if (model, method) == ('product.product', 'read'):
            for row in rows:
                row['uom_id'] = [4, 'kg']
        return rows

    interrupt = False


class DefinitionChangeTest(TestCase):
    def test_two_sources_through_cli_and_ordinary_production(self):
        with TemporaryDirectory() as tmp:
            self.exercise(Path(tmp))

    def exercise(self, run):
        (run/'env').mkdir()
        (run/'scheduled-additions').mkdir()
        east = 'EAST-BULK'
        erp = FrappeFixture()
        ERPNext(erp).provision()
        actors = dict(engineering=dict(decision_actor='east-engineer', token_env='EAST_ENGINEER'),
                      approver=dict(decision_actor='east-approver', token_env='EAST_APPROVER'))
        erp_target = dict(base='http://erp.test', site='company.test')
        primary_mapping = mapping('Company', 1, 1, 2, 3, 4, 5, 6)
        east_mapping = deepcopy(primary_mapping[PRODUCT])
        east_mapping['erp']['output'].update(item=east, unit='Kg')
        east_mapping['mes']['output']['unit'] = 4
        for target in east_mapping.values():
            target['components']['BASE'] = target['components'].pop('NV-BRIDGE-BULK')

        def native(source):
            return dict(source=source, plm=dict(url=f'http://{source}.test', publisher=f'{source}-publisher'))

        primary = dict(native('primary'), products={PRODUCT: dict(
            family=1, publication=1, published=True, mapping=primary_mapping)},
            erp=dict(erp_target, token='receiver'), mes=dict(source_id='primary-mes'))
        primary['plm']['token'] = 'primary-publisher'
        host = dict(native('east'), adapter=ADAPTER, item=east, family=1,
                    mapping={east: east_mapping}, material_kind='intermediate', stock_step=.01,
                    hold_location_id=9, erp=dict(erp_target, token_env='RECEIVER'),
                    mes=dict(url='http://east-mes.test', db='mes', login='planner',
                             source_id='east-mes', password_env='MES_PASSWORD'),
                    qms_reader=dict(url='http://qms.test', site='qms.test',
                                    api_key_env='QMS_KEY', api_secret_env='QMS_SECRET'),
                    qms_approver='qa', specifications={east: 'bulk test'}, units={'kg': 'Kg'})
        host['plm']['token_env'] = 'EAST_PUBLISHER'
        system = 'production-' + east
        spec = dict(adapter=ADAPTER, connection='east',
                    target_hash=sha256(json.dumps(host, sort_keys=True).encode()).hexdigest())
        save(run/'env/connections.json', {'east': host})
        save(run/'env/company-sources.json', primary)
        save(run/'env/endpoints.json', dict(erp=erp_target['base'], site=erp_target['site']))
        save(run/'env/role-tokens.json', dict(PLANNER='planner'))
        save(run/'config.json', dict(company_operating=True))
        save(run/'state.json', dict(day=3))
        with sqlite3.connect(run/'scheduled-additions/engine.sqlite3') as db:
            db.execute('CREATE TABLE checkpoint(id INTEGER PRIMARY KEY, data TEXT)')
            db.execute('INSERT INTO checkpoint VALUES(1, ?)', (json.dumps(
                dict(extensions={'east': {'systems': {system: spec}}})),))

        stores = {f'http://{source}.test': SimpleNamespace(parts={}, lines={}, meta={}, guard=True)
                  for source in ('primary', 'east')}
        mes = {'primary-mes': MesFixture(), 'east-mes': EastMES()}

        def plm(url, token, **kwargs):
            return FixturePLM(stores[url], token, **kwargs)

        evidence = {}
        planner = SimpleNamespace(get=lambda dt, key: deepcopy(evidence[key]))

        def receiver_client(base, site, token):
            self.assertEqual((base, site), tuple(erp_target.values()))
            self.assertIn(token, ('planner', 'receiver'))
            if token == 'planner':
                from unittest.mock import Mock
                client = Mock(wraps=erp)
                client.call.side_effect = lambda method, args: (planner_actor if method == 'frappe.auth.get_logged_user'
                                                                else erp.call(method, args))
                return client
            return erp

        planner_actor = 'planner@norvale.test'
        def erp_client(base, site, token):
            self.assertEqual((base, site), tuple(erp_target.values()))
            if token == 'planner':
                return planner
            self.assertEqual(token, 'receiver')
            return erp

        env = dict(NORVALE_CONNECTIONS_FILE=str(run/'env/connections.json'), RECEIVER='receiver',
                   MES_PASSWORD='mes', QMS_KEY='qms', QMS_SECRET='secret', EAST_PUBLISHER='east-publisher',
                   EAST_ENGINEER='east-engineer', EAST_APPROVER='east-approver')

        def cli(*args):
            output = StringIO()
            with redirect_stdout(output):
                definition_change.main([str(run), *args])
            return json.loads(output.getvalue())

        with (patch.dict('os.environ', env),
              patch.object(definition_change, 'PLM', plm),
              patch.object(definition_change, 'ErpClient', erp_client),
              patch('integrations.company_run.authority.PLM', plm),
              patch('integrations.company_run.authority.ErpClient', receiver_client),
              patch('integrations.company_run.authority.Mes', side_effect=lambda **cfg: mes[cfg['source_id']])):
            authority = Authority(run)
            _, site = profile(run, system, spec)
            planner_actor = 'Administrator'
            with self.assertRaisesRegex(Pending, 'native planner'):
                authority.clients(PRODUCT)
            planner_actor = 'planner@norvale.test'
            with self.assertRaisesRegex(Pending, 'commissioned ERP site'):
                authority.family_clients(dict(primary, erp=dict(erp_target, site='other')), primary['products'][PRODUCT])
            pubs = {}
            for item, cfg, product, lines in (
                (PRODUCT, primary, primary['products'][PRODUCT], components(1)),
                (east, site, site, [{**components(1)[0], 'source_id': 'BASE'}]),
            ):
                pub, bridge, _ = authority.family_clients(cfg, product)
                pubs[item] = pub
                base = pub.draft(item, 'A-1', lines, material_kind=product.get('material_kind', 'finished'))
                self.assertEqual(base, 1)
                pub.review(base, on=EARLY, evidence='installed review')
                pub.approve(base, on=EARLY, effective=EARLY, evidence='installed baseline')
                bridge.publish(base, on=EARLY)
                evidence[item] = dict(name=item, item=item, docstatus=1, modified='native-version',
                                      company='Company', uom='Kg' if item == east else 'Nos', quantity=1,
                                      items=[dict(item_code='NV-BRIDGE-BULK', stock_uom='Kg', stock_qty=.3)])
            evidence[PRODUCT]['items'].append(dict(item_code='NV-BRIDGE-BOTTLE', stock_uom='Nos', stock_qty=1))
            # Primary has two component Parts, so reserve a Part in the east store to collide.
            pubs[east]._ensure_part(dict(IPN='unused'))
            old_bridge = authority.clients(east)[2]
            old = old_bridge.release(1, 'old-work', on=EARLY, installed=True)
            self.assertEqual(cli('propose', PRODUCT, 'B', PRODUCT), 4)
            with self.assertRaisesRegex(Blocked, 'external'):
                cli('propose', east, 'B', east)
            binding = run/'env/definition-changes.json'
            save(binding, {east: dict(source='east', family=1, **actors)})
            evidence[east]['docstatus'] = 0
            with self.assertRaisesRegex(Blocked, 'submitted'):
                cli('propose', east, 'B', east)
            evidence[east]['docstatus'] = 1
            self.assertEqual(cli('propose', east, 'B', east), 4)
            with self.assertRaisesRegex(Blocked, 'different PLM source'):
                cli('review', '4', 'review', '--family', east, '--source', 'primary')
            with patch.dict('os.environ', {'EAST_ENGINEER': 'east-approver'}):
                with self.assertRaisesRegex(Blocked, 'native identity'):
                    cli('review', '4', 'review', '--family', east)
            with patch.dict('os.environ', {'PLM_DECISION_TOKEN': 'norvale-engineering'}):
                cli('review', '4', 'legacy review')
            self.assertNotIn('norvale_review', pubs[east]._meta(4))
            cli('review', '4', 'east review', '--family', east)
            rejected = cli('propose', east, 'rejected', east)
            cli('review', str(rejected), 'alternative review', '--family', east)
            cli('reject', str(rejected), 'alternative declined', '--family', east)
            with self.assertRaises(Blocked):
                cli('approve', str(rejected), 'alternative approval', '--effective', LATER, '--family', east)
            cli('approve', '4', 'east approval', '--effective', LATER, '--family', east)
            self.assertNotIn('norvale_approval', pubs[PRODUCT]._meta(4))
            self.assertEqual(pubs[east].definition(4)['material_kind'], 'intermediate')
            self.assertEqual(pubs[east].pin(1, 'early', on=EARLY)['native_id'], 1)

            events, planning = [], []
            engine = SimpleNamespace(run=run, state={'ops': {'orders': {}}}, day=31, additions=None,
                                     config={'company_operating': True}, date=lambda: LATER,
                                     log=lambda kind, **data: events.append((kind, data)))
            checkpoint = run/'phase-state.json'
            engine.persist = lambda: save(checkpoint, engine.state)
            flow = engine.company_erp = CompanyERP(engine, authority)

            def plan():
                try:
                    planning.append(pubs[east].pin(1, 'planning', on=LATER)['native_id'])
                except Blocked:
                    planning.append(None)
            engine.plan_production = plan
            engine.date = lambda: '2027-01-03'
            Engine.production(engine)
            self.assertNotIn('norvale_ack_erp', pubs[east]._meta(4))
            engine.date = lambda: LATER
            binding.unlink()
            Engine.production(engine)
            self.assertNotIn('norvale_ack_erp', pubs[east]._meta(4))
            save(binding, {east: dict(source='east', family=1, **actors)})
            stores['http://east.test'].guard = False
            Engine.production(engine)
            self.assertNotIn('norvale_ack_erp', pubs[east]._meta(4))
            stores['http://east.test'].guard = True
            mes['east-mes'].interrupt = True
            Engine.production(engine)
            self.assertIn('definition-publication:' + east, flow.state['pending'])
            self.assertIn('norvale_ack_erp', pubs[east]._meta(4))
            self.assertNotIn('norvale_ack_mes', pubs[east]._meta(4))
            Engine.production(engine)
            engine.state = json.loads(checkpoint.read_text())
            Engine.production(engine)
            self.assertEqual(planning, [None, None, None, None, 4, 4])
            self.assertEqual(flow.state['pending'], {})
            published, = [row for kind, row in events if kind == 'definition_published']
            self.assertEqual((published['source'], published['family'], published['part']), ('east', 1, 4))
            self.assertNotIn('norvale_ack_erp', pubs[east]._meta(rejected))
            self.assertEqual(len(erp.boms), 3)
            self.assertEqual(len(mes['east-mes'].boms), 2)
            self.assertEqual(len(mes['primary-mes'].boms), 1)
            reopened = authority.clients(east)[2]
            self.assertEqual(reopened.release(1, 'old-work', on=EARLY, installed=True), old)
            new = reopened.release(1, 'new-work', on=LATER, installed=True)
            self.assertEqual(new['pin']['native_id'], 4)
            self.assertNotEqual(new['pin']['receiver_ids'], old['pin']['receiver_ids'])
            save(run/'state.json', dict(day=31))
            self.assertEqual(cli('propose', east, 'B', east), 4)
            cli('approve', '4', 'east approval', '--effective', LATER, '--family', east)
            with patch.dict('os.environ', {'PLM_DECISION_TOKEN': 'norvale-approver'}):
                cli('approve', '4', 'legacy approval', '--effective', '2027-02-02')
            Engine.production(engine)
            Engine.production(engine)
            self.assertEqual(len([kind for kind, _ in events if kind == 'definition_published']), 2)
            self.assertEqual(len(mes['primary-mes'].boms), 2)
            self.assertEqual(len(mes['east-mes'].boms), 2)
