"""Run the recurring planning phase across an effective PLM revision offline."""
from copy import deepcopy
from contextlib import closing
from dataclasses import dataclass, field
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from integrations.company_run.authority import Authority
from integrations.company_run.definition_change import propose
from integrations.company_run.test_definition_change import FixturePLM
from integrations.company_run.timing import DEFAULT_TIMING
from integrations.mes.client import Mes
from integrations.qms.adapter import INSPECTION, QMS
from integrations.revisions.receivers import ERP_KEY, ERPNext
from integrations.revisions.test_bridge import EARLY, LATER, PRODUCT, FrappeFixture, MesFixture, components, mapping
from native.company import DEFAULT_CONFIG, Engine, save
from native.company_erp import Pending
from native.recurring_extensions import open_catalog

BULK, BOTTLE = 'NV-BRIDGE-BULK', 'NV-BRIDGE-BOTTLE'
PLANNER = 'planner@norvale.test'


class PlanningERP(FrappeFixture):
    def __init__(self):
        super().__init__()
        self.docs = {}
        self.stock = {}
        self.calls = []

    def role(self, actor):
        return ERPLogin(self, actor)


@dataclass
class ERPLogin:
    store: PlanningERP
    actor: str

    def get(self, kind, key):
        self.store.calls.append((self.actor, 'get', kind, key))
        if (kind, key) in self.store.docs:
            return deepcopy(self.store.docs[kind, key])
        return self.store.get(kind, key)

    def list(self, kind, filters, *, limit=20):
        if kind == 'BOM' and ERP_KEY in filters:
            return self.store.list(kind, filters, limit=limit)
        if kind == 'Custom Field':
            assert self.actor == 'Administrator'
            return [{'name': 'operation'}]
        assert self.actor == PLANNER
        return [deepcopy(row) for (dt, _), row in self.store.docs.items()
                if dt == kind and all(row.get(k) == v for k, v in filters.items())][:limit]

    def metadata(self, kind):
        return self.store.metadata(kind)

    def create(self, kind, fields):
        assert self.actor == PLANNER
        self.store.calls.append((self.actor, 'create', kind))
        if kind == 'BOM':
            row = self.store.create(kind, fields)
        else:
            assert kind in ('Work Order', 'ToDo')
            name = f'{kind}-{len(self.store.docs) + 1}'
            row = self.store.docs[kind, name] = dict(deepcopy(fields), name=name, docstatus=0)
        row['owner'] = self.actor
        return deepcopy(row)

    def update(self, kind, key, fields):
        assert self.actor == PLANNER and kind == 'Work Order' and fields == {'docstatus': 1}
        self.store.calls.append((self.actor, 'submit', kind, key))
        row = self.store.docs[kind, key]
        assert row['docstatus'] == 0
        row.update(fields)
        return deepcopy(row)

    def call(self, method, args):
        if method == 'frappe.auth.get_logged_user':
            return self.actor
        assert self.actor == PLANNER
        if method.endswith('.get_batch_qty'):
            assert args['warehouse'] == 'Raw Materials - NCC'
            return self.store.stock[args['item_code'], args['batch_no']]
        assert method == 'frappe.client.submit'
        return self.store.call(method, args)


class PlanningMES(MesFixture):
    def __init__(self):
        super().__init__()
        self.orders = {}
        self.calls = []

    def endpoint(self, url, db, login, password, source_id):
        assert (url, db, source_id) == ('http://mes.test', 'mes', 'test-mes')
        assert (login, password) in [('receiver', 'receiver'), ('operator', 'operator')]
        client = Mes.__new__(Mes)
        client.url, client.db, client.source_id = url, db, source_id
        client.call = lambda *args, **kwargs: self.rpc(login, *args, **kwargs)
        return client

    def rpc(self, actor, model, method, *args, **kwargs):
        self.calls.append((actor, model, method))
        if method == 'norvale_acknowledge':
            return self.acknowledge(args[0])
        if method == 'norvale_dispatch':
            assert actor == 'receiver'
            origin, product, bom, revision, qty, hold = args
            assert revision == self.acknowledge(bom)['revision']
            pk = next((k for k, row in self.orders.items() if row['origin'] == origin), len(self.orders) + 1)
            row = dict(id=pk, origin=origin, product_id=[product, ''], bom_id=[bom, ''],
                       norvale_revision=revision, product_qty=qty, location_dest_id=[hold, ''],
                       state='confirmed', procurement_group_id=False, workorder_ids=[pk])
            assert self.orders.setdefault(pk, row) == row
            return pk
        if (model, method) == ('mrp.workorder', 'search_count'):
            assert actor == 'operator'
            return 0
        if method == 'read':
            if model == 'mrp.production':
                return [deepcopy(self.orders[pk]) for pk in args[0]]
            if model == 'mrp.workorder':
                assert actor == 'operator'
                return [dict(id=pk, workcenter_id=[6, 'Fill']) for pk in args[0]]
            if model == 'mrp.workcenter' and 'working_state' in kwargs['fields']:
                assert actor == 'operator'
                return [dict(id=pk, working_state='normal') for pk in args[0]]
        if method == 'create':
            assert actor == 'receiver'
        return super().call(model, method, *args, **kwargs)


@dataclass
class Parts:
    parts: dict = field(default_factory=dict)
    lines: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    guard: bool = True


class OrdinaryPlanningTest(TestCase):
    def test_effective_recipe_drives_ordinary_planning_and_reopen(self):
        for scoped in (False, True):
            with self.subTest(scoped=scoped), TemporaryDirectory() as tmp:
                self.exercise(Path(tmp), scoped)

    def exercise(self, run, scoped):
        (run/'env').mkdir()
        erp, mes, parts = PlanningERP(), PlanningMES(), Parts()
        ERPNext(erp).provision(scoped=scoped)
        endpoints = dict(erp='http://erp.test', site='company.test')
        save(run/'env/role-tokens.json', dict(PLANNER=PLANNER))
        save(run/'env/controller-token.json', dict(token='Administrator'))
        save(run/'env/endpoints.json', endpoints)
        config = dict(DEFAULT_CONFIG, company_operating=True, company_timing=DEFAULT_TIMING,
                      company_authority='integrations.company_run.authority:Authority', capacity_units=20)
        save(run/'config.json', config)
        product = dict(family=1, publication=1, published=True, hold_location_id=9, quality_method='QA-1',
                       mapping=mapping(Engine.company, 1, 1, 2, 3, 4, 5, 6))
        receiver = dict(url='http://mes.test', db='mes', source_id='test-mes',
                        login='receiver', password='receiver')
        specs = {BULK: 'Incoming base', BOTTLE: 'Incoming bottle'}
        save(run/'env/company-sources.json', dict(
            source='test-plm', products={PRODUCT: product}, scoped_receipts=scoped,
            erp=dict(base=endpoints['erp'], site=endpoints['site']), center=6,
            plm=dict(url='http://plm.test', token='publisher', publisher='publisher'),
            mes=receiver, operator=dict(receiver, login='operator', password='operator'),
            qms_reader=dict(url='http://qms.test', api_key='reader', api_secret='reader'),
            qms_approver='qa', specifications=specs))
        save(run/'state.json', dict(day=3, completed=[], docs={}, physical={},
                                  ops=dict(orders={}, maintenance={}, shortage_days={}, changes={})))

        def erp_endpoint(base, site, token):
            self.assertEqual((base, site), (endpoints['erp'], endpoints['site']))
            self.assertIn(token, (PLANNER, 'Administrator'))
            return erp.role(token)

        def plm_endpoint(url, token, **kwargs):
            self.assertEqual(url, 'http://plm.test')
            return FixturePLM(parts, token, **kwargs)

        def reopen():
            e = Engine(run)
            e.additions = open_catalog(run, e.config, e.state, company=e)
            return e

        def queue():
            path = run/'scheduled-additions/engine.sqlite3'
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                return [json.loads(row[0]) for row in db.execute('SELECT edge FROM queue ORDER BY sequence')]

        def order(e, key, qty):
            name = 'SO-' + key
            erp.docs['Sales Order', name] = dict(name=name, docstatus=1, items=[
                dict(name=name + '-1', item_code=PRODUCT, qty=qty, stock_uom='Nos')])
            e.docs[key] = dict(doctype='Sales Order', name=name)
            e.state['ops']['orders'][key] = dict(item=PRODUCT)
            e.persist()

        inspections = {}
        qms = QMS(url='http://qms.test', api_key='reader', api_secret='reader')

        def qms_request(method, path, data=None):
            self.assertEqual(method, 'GET')
            if path.startswith(qms.resource(INSPECTION) + '/'):
                return {'data': deepcopy(inspections[path.rsplit('/', 1)[1]])}
            raise AssertionError(path)

        def qms_list(kind, filters, **kwargs):
            self.assertEqual(kind, INSPECTION)
            return [deepcopy(row) for row in inspections.values() if row['batch_no'] == filters['batch_no']]

        def qms_endpoint(**cfg):
            self.assertEqual(cfg, dict(url='http://qms.test', api_key='reader', api_secret='reader'))
            return qms

        with (patch('native.company.Stack.endpoints', return_value=endpoints),
              patch('native.company.ErpClient', erp_endpoint),
              patch('integrations.company_run.authority.ErpClient', erp_endpoint),
              patch('integrations.company_run.authority.PLM', plm_endpoint),
              patch('integrations.company_run.authority.Mes', mes.endpoint),
              patch('integrations.company_run.maintenance.Mes', mes.endpoint),
              patch('integrations.company_run.authority.QMS', qms_endpoint),
              patch.object(qms, 'request', qms_request),
              patch.object(qms, 'list', qms_list)):
            e = reopen()
            authority = e.company_erp.authority
            _, publisher, bridge, _ = authority.clients(PRODUCT)
            engineer, approver = [plm_endpoint('http://plm.test', role, publisher='publisher', decision_actor=role)
                                  for role in ('engineering', 'approver')]
            original = publisher.draft(PRODUCT, 'A-1', components(1))
            engineer.review(original, on=EARLY, evidence='Baseline review')
            approver.approve(original, on=EARLY, effective=EARLY, evidence='Baseline approval')
            bridge.publish(original, on=EARLY)

            for code, uom, qty in ((BULK, 'Kg', 2.5), (BOTTLE, 'Nos', 10)):
                batch = 'LOT-' + code
                lot = dict(batch=batch, item=code, uom=uom, qty=qty, kind='incoming')
                e.company_erp.state['lots'][batch] = lot
                erp.stock[code, batch] = qty
                name = 'QI-' + code
                inspections[name] = dict(name=name, modified='signed', modified_by='qa', batch_no=batch,
                    item_code=code, docstatus=1, status='Accepted', workflow_state='Released', report_date=EARLY,
                    custom_spec_revision=specs[code], custom_lot_quantity=qty, custom_lot_uom=uom)
                disposition = authority.disposition(lot, as_of=EARLY, role='QA')
                e.company_erp.state.setdefault('synced', {})[batch] = disposition

            order(e, 'original', 4)
            e.event = '3:production'
            e.production()
            old_key = 'd0003-work-original'
            self.assertEqual(e.company_erp.state['pending'], {})
            old = deepcopy(e.company_erp.state['work'][old_key])
            old_wo = e.api['PLANNER'].get('Work Order', old['request']['work_order'])
            old_mo = mes.endpoint(**receiver).read('mrp.production', int(old['dispatch']['id']))
            self.assertEqual({r['item']: r['qty'] for r in old['inputs']}, {BULK: 1, BOTTLE: 4})

            # The retained native qualification used 0.25 -> 0.30 kg of base.
            # After the old work's 1 kg reservation, 1.5 kg can make five units.
            evidence = dict(name='SIGNED-BOM', item=PRODUCT, company=Engine.company, docstatus=1,
                            modified='signed-2', quantity=1, uom='Nos', items=[
                                dict(item_code=BULK, stock_uom='Kg', stock_qty=.30),
                                dict(item_code=BOTTLE, stock_uom='Nos', stock_qty=1)])
            erp.docs['BOM', evidence['name']] = evidence
            revised = propose(publisher, e.api['PLANNER'], product, PRODUCT, 'B', evidence['name'], EARLY)
            engineer.review(revised, on=EARLY, evidence=evidence['name'])
            approver.approve(revised, on=EARLY, effective=LATER, evidence='Revised formulation')
            self.assertEqual(publisher._meta(revised)['norvale_approval']['actor'], 'approver')
            self.assertEqual(publisher._meta(revised)['norvale_review']['actor'], 'engineering')
            with self.assertRaisesRegex(Pending, 'Role is not authorized'):
                authority.definition(PRODUCT, as_of=LATER, role='PRODUCTION')

            e.day, e.event = 30, '30:production'
            order(e, 'revised', 5)
            order(e, 'short', 1)
            # An acknowledgement alone cannot qualify a changed receiver BOM.
            old_bom = mes.boms[old_mo['bom_id'][0]]
            base_line = next(mes.lines[pk] for pk in old_bom['bom_line_ids']
                             if mes.lines[pk]['product_id'] == 2)
            with patch.dict(base_line, product_qty=.5):
                e.production()
            self.assertIn('definition:' + PRODUCT, e.company_erp.state['pending'])
            self.assertEqual(e.state['plans'][e.event], dict(work=[], shortages=[]))
            self.assertEqual(e.company_erp.state['work'], {old_key: old})
            self.assertEqual(len([kind for kind, _ in erp.docs if kind == 'Work Order']), 1)
            self.assertEqual(len(mes.orders), 1)

            e.day, e.event = 31, '31:production'
            e.production()
            self.assertEqual(e.company_erp.state['pending'], {})
            self.assertEqual(set(e.company_erp.state['work']), {old_key, 'd0031-work-revised'})
            new = e.company_erp.state['work']['d0031-work-revised']
            self.assertEqual(e.company_erp.reserved_inputs(), {'LOT-' + BULK: 2.5, 'LOT-' + BOTTLE: 9})
            self.assertEqual({r['item']: r['qty'] for r in new['inputs']}, {BULK: 1.5, BOTTLE: 5})
            self.assertEqual(e.state['ops']['shortage_days'], {BULK: [31]})
            shortage = e.get('shortage:' + BULK, 'PLANNER')
            self.assertEqual((shortage['owner'], shortage['allocated_to']), (PLANNER, 'scm_mgr@norvale.test'))
            new_wo = e.api['PLANNER'].get('Work Order', new['request']['work_order'])
            self.assertEqual((new_wo['owner'], new_wo['docstatus'], new_wo['qty'],
                              new_wo['sales_order'], new_wo['sales_order_item']),
                             (PLANNER, 1, 5, 'SO-revised', 'SO-revised-1'))
            pin = json.loads(new_wo['custom_norvale_definition'])
            self.assertEqual((pin['id'], pin['erp_released_on']), (str(revised), LATER))
            self.assertEqual(pin, dict(new['revision'], erp_released_on=LATER))
            self.assertEqual(new_wo['bom_no'], pin['erp_ack_id'])
            erp_bom = e.api['PLANNER'].get('BOM', pin['erp_ack_id'])
            self.assertEqual((erp_bom['docstatus'], erp_bom['owner']), (1, PLANNER))
            self.assertEqual({row['item_code']: row['stock_qty'] for row in erp_bom['items']},
                             {BULK: .30, BOTTLE: 1})
            mes_reader = mes.endpoint(**dict(receiver, login='operator', password='operator'))
            new_mo = mes_reader.read('mrp.production', int(new['dispatch']['id']))
            mes_bom_id = new_mo['bom_id'][0]
            self.assertEqual((new['dispatch']['source'], new_mo['origin'], new_mo['product_qty'], str(mes_bom_id)),
                             ('test-mes', new_wo['name'], 5, pin['mes_ack_id']))
            mes_bom, = mes_reader.call('mrp.bom', 'read', [mes_bom_id], fields=['bom_line_ids'])
            mes_lines = mes_reader.call('mrp.bom.line', 'read', mes_bom['bom_line_ids'],
                                        fields=['product_id', 'product_qty'])
            self.assertEqual({row['product_id'][0]: row['product_qty'] for row in mes_lines}, {2: .30, 3: 1})
            self.assertNotEqual(erp_bom['name'], old_wo['bom_no'])
            self.assertNotEqual(mes_bom_id, old_mo['bom_id'][0])
            self.assertIn(('operator', 'mrp.workorder', 'read'), mes.calls)

            snapshot = deepcopy(e.company_erp.state['work'])
            queued = queue()
            schedule = [ev for ev in queued if ev[5] == 'company-native']
            self.assertEqual([ev[8] for ev in schedule], ['PRODUCTION', 'PRODUCTION'])
            self.assertEqual({ev[6]['job'][2] for ev in schedule},
                             {old['dispatch']['id'], new['dispatch']['id']})
            e = reopen()
            e.event = '31:production'
            e.production()
            self.assertEqual(e.company_erp.state['pending'], {})
            self.assertEqual(e.company_erp.state['work'], snapshot)
            self.assertEqual(e.company_erp.state['work'][old_key], old)
            self.assertEqual(e.api['PLANNER'].get('Work Order', old_wo['name']), old_wo)
            self.assertEqual(mes_reader.read('mrp.production', old_mo['id']), old_mo)
            self.assertEqual(queue(), queued)
            self.assertEqual(len(mes.orders), 2)
            self.assertEqual(len([row for row in erp.calls if row[1:3] == ('create', 'Work Order')]), 2)
            reopened = Authority(run).clients(PRODUCT)[2]
            for work, wo in ((old, old_wo), (new, new_wo)):
                on = work['request']['released_on']
                saved = reopened.release(original, wo['name'], on=on)
                self.assertEqual(saved['pin']['source_version'], work['revision']['version'])
                self.assertEqual(saved['pin']['receiver_ids'],
                                 dict(erp=wo['bom_no'], mes=work['revision']['mes_ack_id']))
