"""Exercise recurring CLI, ERP planner and source-scoped native handoffs offline."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlsplit
from xmlrpc.client import Fault
from unittest import TestCase
from unittest.mock import patch

import generate
import company
from integrations.company_run.authority import Authority
from integrations.company_run.maintenance_staff import ACTORS
from integrations.mes.client import CENTER, EVENT, MO, WO, Mes
from integrations.qms.adapter import CASE, INSPECTION, OP
from native.company_erp import CompanyERP, Pending
from native.production_profiles import FACTORY, registered
from native.recurring_extensions import open_catalog


class ERP:
    def __init__(self):
        self.lose_submit = False
        self.docs = {('Sales Order', 'original'): {
            'name': 'original', 'docstatus': 1, 'items': [{'qty': 12, 'delivered_qty': 12}]}}

    def get(self, doctype, identifier):
        if doctype == 'Customer':
            return {'name': identifier}
        if doctype == 'UOM':
            return {'must_be_whole_number': 1}
        if doctype == 'Item':
            return {'name': identifier, 'has_batch_no': 1}
        return deepcopy(self.docs[doctype, identifier])

    def list(self, doctype, filters, **kwargs):
        if doctype == 'Custom Field':
            return [{'name': 'operation'}]
        return [deepcopy(doc) for (dt, name), doc in self.docs.items()
                if dt == doctype and all(doc.get(k) == v for k, v in filters.items())]

    def create(self, doctype, data):
        name = data.get('batch_id', f'{doctype}-{len(self.docs)}')
        row = self.docs[doctype, name] = dict(deepcopy(data), name=name, docstatus=0, doctype=doctype)
        if doctype == 'Work Order':
            row.update(stock_uom='Nos', produced_qty=0)
        for i, item in enumerate(row.get('items', [])):
            item.update(name=f'{name}-{i}', stock_uom='Nos', conversion_factor=1)
        return deepcopy(row)

    def update(self, doctype, name, data):
        row = self.docs[doctype, name]
        if doctype == 'Stock Entry' and row['docstatus'] == 0 and data.get('docstatus') == 1:
            self.docs['Work Order', row['work_order']]['produced_qty'] += row['fg_completed_qty']
        row.update(data)
        if doctype == 'Sales Order' and self.lose_submit:
            self.lose_submit = False
            raise TimeoutError('lost submitted Sales Order response')
        return self.get(doctype, name)


class PLM:
    def __init__(self, url, **kwargs):
        self.item = url.rsplit('/', 1)[-1].upper()

    def pin(self, family, work_id, *, on):
        return dict(work_id=work_id, released_on=on, native_id=family, source_version='approval',
                    receiver_ids={'erp': 'BOM-' + self.item, 'mes': '7'})

    def resolve_pin(self, pin, **kwargs):
        return dict(native_id=pin['native_id'], source_version=pin['source_version'],
                    definition={'name': self.item, 'lines': [
                        {'source_id': 'bulk', 'kind': 'formulation', 'unit': 'kg', 'quantity': 1},
                        {'source_id': 'pack', 'kind': 'packaging', 'unit': 'ea', 'quantity': 1},
                    ]}, approval={'effective': '2027-01-01', 'evidence': 'approval-record'})


class MES:
    def __init__(self, source_id):
        self.source_id = source_id
        self.work = set()
        self.done = False

    def call(self, model, method, *args, **kwargs):
        if model == 'stock.location':
            return [dict(id=9, active=True, usage='internal', company_id=[1])]
        if model == 'mrp.bom':
            return [{'operation_ids': [1]}]
        if model == 'mrp.routing.workcenter':
            return [{'workcenter_id': [4]}]
        if (model, method) == ('mrp.production', 'search'):
            return [1] if args[0][0][2] in self.work else []
        raise AssertionError((model, method, args))

    def availability(self, center):
        assert center == 4
        return {'working_state': 'normal', 'available': True}

    def dispatch(self, work, product, bom, revision, qty, hold):
        assert (product, bom, revision, qty, hold) == (2, 7, 'fingerprint', 2, 9)
        self.work.add(work)
        return {'id': 1, 'norvale_revision': revision, 'workorder_ids': [1]}

    def read(self, model, identifier):
        assert identifier == 1
        return {'state': 'done' if self.done else 'confirmed', 'workcenter_id': [4]}

    def completion(self, identifier):
        def ref(entity, pk):
            return dict(system='mes', source_id=self.source_id, entity=entity, native_id=pk)
        def movement(item, lot_name, pk):
            return dict(item_code=item, lot_name=lot_name, quantity=2, uom_name='Units',
                        lot=ref('stock.lot', pk), record=ref('stock.move.line', pk))
        origin, = self.work
        return dict(revision='fingerprint', bom=ref('mrp.bom', 7), erp_work_ref=origin,
                    work=ref('mrp.production', identifier), output_hold_location=ref('stock.location', 9),
                    completion_ref=f'{self.source_id}:mrp.production:{identifier}',
                    recorded_at_utc='2027-01-02T12:00:00', backorder=None,
                    output=movement(self.source_id.upper(), 'finished', 1),
                    inputs=[movement('BULK', 'bulk-lot', 2), movement('PACK', 'pack-lot', 3)])



class OperatingMES(MES):
    """Independent MES records. Both endpoints intentionally reuse all numeric IDs."""

    def __init__(self, source_id, *, bounded=False):
        super().__init__(source_id)
        self.bounded = bounded
        self.orders = {1: dict(state='confirmed', qty=2)}
        self.reservations = {}
        self.posted = {}
        self.cut_replay = None
        self.state = 'confirmed'
        self.lots, self.receipts, self.calls = {}, {}, []
        self.legacy_receipts = []
        self.staging_writes = []
        self.lose_receipt = source_id == 'east'
        self.before_receipt = lambda ref, lot, location, quantity: None
        self.labor = None
        self.online = lambda: True

    def call_as(self, login, model, method, *args, **kwargs):
        if (model, method) == (MO, 'search') and args[0] == [['procurement_group_id', '=', 11]]:
            return list(self.orders)
        if (model, method) == ('product.product', 'read'):
            assert login in ('operator', 'planner')
            product, = args[0]
            return [dict(id=product, default_code={2: self.source_id.upper(), 3: 'BULK', 4: 'PACK'}[product],
                         uom_id=[1, 'Units'], company_id=[1, 'Site'], tracking='lot', is_storable=True,
                         active=True)]
        if (model, method) == ('stock.location', 'read'):
            location, = args[0]
            return [dict(id=location, active=True, usage='internal', company_id=[1, 'Site'])]
        if model == 'stock.lot':
            assert login == 'planner'
            if method == 'search':
                name, product, owner = [condition[2] for condition in args[0]]
                assert owner == 1
                return [key for key, lot in self.lots.items()
                        if (lot['name'], lot['product_id']) == (name, product)]
            if method == 'norvale_stage_input':
                (lot,), receipt, location, quantity, original_mo = args
                assert int(original_mo) > 0
                self.before_receipt(receipt, lot, location, quantity)
                self.staging_writes.append(receipt)
                assert location == 8 and receipt not in self.receipts
                line = self.receipt_line(lot, location, quantity, len(self.receipts) + 1)
                line["move"] = dict(norvale_staging_order_id=original_mo, norvale_staging_receipt=receipt,
                                    is_inventory=True, product_uom=1, product_uom_qty=quantity, quantity=quantity)
                self.receipts[receipt] = line
                if self.lose_receipt:
                    self.lose_receipt = False
                    raise TimeoutError('east committed staging before reply')
                return line['id']
            assert method == 'create' and args[0]['company_id'] == 1
            identifier = len(self.lots) + 1
            self.lots[identifier] = args[0]
            return identifier
        if model == 'stock.quant':
            assert login == 'planner' and method == 'search_read'
            lot = args[0][0][2]
            return [{'quantity': self.staged(lot)}] if self.staged(lot) else []
        if model == 'stock.move.line':
            if method == 'search':
                identifier, *predicates = args[0]
                return [r['id'] for r in self.receipts.values() if r['id'] == identifier[2]
                        and all(r['move'][key.removeprefix('move_id.')] == value for key, _, value in predicates)]
            assert login == 'planner' and method == 'search_read'
            field, _, value = args[0][0]
            if field == 'lot_id':
                return deepcopy([row for row in self.legacy_receipts if row['lot_id'][0] == value])
            assert field == 'move_id.origin'
            return [deepcopy(self.receipts[value])] if value in self.receipts else []
        return super().call(model, method, *args, **kwargs)

    def receipt_line(self, lot, location, quantity, identifier):
        return dict(id=identifier, state='done', lot_id=[lot], product_id=[self.lots[lot]['product_id']],
                    product_uom_id=[1], quantity=quantity, location_id=[99], location_dest_id=[location],
                    company_id=[1], owner_id=False, package_id=False, result_package_id=False)

    def staged(self, lot):
        return sum(row['quantity'] for row in self.receipts.values() if row['lot_id'][0] == lot)

    def client(self, login):
        site = self

        class Client:
            source_id = site.source_id
            availability = site.availability
            dispatch = site.dispatch
            def completion(self, mo):
                return site.completion(mo)

            def call(self, *args, **kwargs):
                return site.call_as(login, *args, **kwargs)

            def read(self, model, mo):
                if login == 'operator' and not site.online():
                    raise TimeoutError('operator read unavailable')
                if site.bounded:
                    if model == 'product.product':
                        return {'default_code': site.source_id.upper()}
                    native = site.orders[mo]
                    origin, = site.work
                    if model == WO:
                        return dict(state='ready', workcenter_id=[4], production_id=[mo])
                    return dict(id=mo, state=native['state'], origin=native.get('origin', origin),
                                norvale_revision='fingerprint', bom_id=[7], product_id=[2],
                                product_uom_id=[1, 'Units'], product_qty=native['qty'],
                                company_id=[1, 'Site'], workorder_ids=[mo], location_src_id=[8],
                                location_dest_id=[9], procurement_group_id=[11], backorder_sequence=mo)
                assert mo == 1
                if model == WO:
                    return dict(state='ready', workcenter_id=[4], production_id=[1])
                assert model == MO
                origin, = site.work
                return dict(id=1, state=site.state, origin=origin, norvale_revision='fingerprint',
                            bom_id=[7], product_id=[2], product_uom_id=[1], product_qty=2, company_id=[1, 'Site'],
                            workorder_ids=[1], location_src_id=[8], location_dest_id=[9])

            def acknowledge(self, bom):
                assert login == 'operator' and bom == 7
                return {'revision': 'fingerprint'}

            def start(self, mo, rows, released):
                if site.bounded:
                    native = site.orders[mo]
                    assert login == 'operator' and released == [1, 2]
                    assert rows == [{'lot_id': lot, 'quantity': native['qty']} for lot in released]
                    assert [site.staged(lot) for lot in released] == [2, 2]
                    site.reservations[mo] = deepcopy(rows)
                    native['state'] = 'progress'
                    return
                assert login == 'operator' and mo == 1 and released == [1, 2]
                assert rows == [{'lot_id': item, 'quantity': 2} for item in released]
                assert [site.staged(lot) for lot in released] == [2, 2]
                site.calls.append('start')
                if site.state == 'confirmed':
                    site.state = 'progress'
                    if site.source_id == 'east':
                        raise TimeoutError('east committed start before reply')

            def complete(self, mo, lot, qty, rows, released, *, labor):
                if site.bounded:
                    native = site.orders[mo]
                    assert login == 'operator' and (qty, lot) == (1, mo + 2)
                    assert rows == [{'lot_id': item, 'quantity': 1} for item in (1, 2)]
                    assert (labor['source'], labor['dispatch'], labor['center']) == (
                        site.source_id, str(mo), site.source_id + ':4')
                    assert labor['work_order'] in site.work
                    arguments = deepcopy((lot, qty, rows, labor))
                    if native['state'] == 'done':
                        assert not released and site.posted[mo] == arguments
                        if site.cut_replay == mo:
                            site.cut_replay = None
                            raise InterruptedError('restart after lost native reply')
                        site.calls.append(('replay', mo))
                        return
                    assert native['state'] == 'progress' and released == [1, 2]
                    if qty < native['qty']:
                        site.orders[mo + 1] = dict(state='confirmed', qty=native['qty'] - qty)
                    native.update(state='done', qty=qty)
                    site.posted[mo] = arguments
                    site.calls.append(('post', mo))
                    if site.source_id == 'east' and mo == 2:
                        site.cut_replay = mo
                        raise TimeoutError('east backorder committed before reply')
                    return
                assert login == 'operator' and (mo, lot, qty) == (1, 3, 2)
                assert rows == [{'lot_id': item, 'quantity': 2} for item in (1, 2)]
                assert labor['source'] == site.source_id and labor['center'] == site.source_id + ':4'
                assert labor['work_order'] in site.work
                site.calls.append('complete')
                if site.state == 'progress':
                    assert released == [1, 2]
                    site.state, site.labor = 'done', deepcopy(labor)
                    if site.source_id == 'west':
                        raise TimeoutError('west committed completion before reply')
                else:
                    assert released == [] and labor == site.labor

        return Client()

    def completion(self, identifier):
        result = super().completion(identifier)
        if self.bounded:
            lot, qty, rows, _ = self.posted[identifier]
            result['output'].update(quantity=qty, lot_name='NV-FG-' + str(identifier))
            result['output']['lot']['native_id'] = lot
            for movement, row in zip(result['inputs'], rows, strict=True):
                movement['quantity'] = row['quantity']
                movement['lot']['native_id'] = row['lot_id']
            if identifier + 1 in self.orders:
                result['backorder'] = dict(result['work'], native_id=identifier + 1)
        else:
            result['output']['lot']['native_id'] = 3
        return result

def approved_profile(site):
    item = site.upper()

    def target(output, items, unit, settings):
        return {'output': {'item': output, 'unit': unit, 'qty': 1}, 'settings': settings,
                'components': {
                    source: {'item': native, 'unit': unit, 'factor': 1, 'kind': kind, 'source_unit': uom}
                    for source, native, kind, uom in zip(
                        ('bulk', 'pack'), items, ('formulation', 'packaging'), ('kg', 'ea'))}}

    return dict(adapter='company-production', item=item, customer='Native Customer',
                order_roles=['add-on-sales'], source='plm-' + site, family=12, hold_location_id=9,
                plm={'url': 'http://plm.test/' + site, 'publisher': 'publisher', 'token_env': 'SECRET'},
                erp={'base': 'http://erp.test', 'site': 'site', 'token_env': 'SECRET'},
                mes={'url': 'http://' + site + '.test', 'db': 'mes', 'login': 'planner',
                     'password_env': 'SECRET', 'source_id': site},
                qms_reader={'url': 'http://' + site + '.qms.test', 'site': 'qms',
                            'api_key_env': 'SECRET', 'api_secret_env': 'SECRET'},
                qms_approver='qa', specifications={item: 'assay'},
                units={'Units': 'Nos'}, mapping={item: {
                    'erp': target(item, ['BULK', 'PACK'], 'Nos', {'company': company.Engine.company}),
                    'mes': target(2, [3, 4], 1, {'company_id': 1})}})


def maintenance_scope(site):
    return dict(login='maintenance', ticket_url='http://' + site + '.tickets.test',
                ticket_repo='plant/maintenance',
                **{field + '_env': site.upper() + '_' + field.upper()
                   for field in ('password', 'ticket_operator', *ACTORS)})


class MaintenanceMES(Mes):
    def __init__(self, site, login):
        self.site, self.login, self.uid = site, login, 2 if login == 'maintenance' else 1
        self.source_id, self.url = site['source'], 'http://' + site['source'] + '.test'

    def call(self, model, method, *args, **kw):
        site = self.site
        if (model, method) == (CENTER, 'norvale_fault'):
            self.mutator()
            assert args[0] == [4]
            row = site['events'].setdefault(91, dict(
                id=91, workcenter_id=[4], norvale_interrupted_id=[1] if site['source'] == 'east' else False,
                loss_type='availability', date_end=False, description='Fault ' + args[1]))
            assert row['description'] == 'Fault ' + args[1]
            if site.pop('lose_reply', False):
                raise TimeoutError('fault committed before response was lost')
            return 91
        if (model, method) == (CENTER, 'norvale_restart'):
            self.mutator()
            assert args[0:2] == ([4], 91)
            row = site['events'][91]
            if not row['date_end']:
                row.update(date_end='now', description=row['description'] + '\nVerified restart ' + args[2])
            return 91
        if (model, method) == (MO, 'norvale_reject_interrupted'):
            self.mutator()
            assert args[0:2] == ([1], 91) and site['source'] == 'east'
            site['events'][91]['norvale_disposition'] = args[2]
            site['cancelled'] = True
            if site.pop('lose_cancel', False):
                raise TimeoutError('QMS-bound cancellation committed before response was lost')
            return 1
        if (model, method) == (EVENT, 'search'):
            return [i for i, row in site['events'].items() if all(
                row[field].startswith(value[:-1]) if op == '=like'
                else row[field][0] in value if op == 'in'
                else row[field][0] == value if field == 'workcenter_id'
                else row[field] == value for field, op, value in args[0])]
        if (model, method) == (EVENT, 'create'):
            self.mutator()
            row = dict(args[0], id=92, workcenter_id=[4], loss_type='performance', workorder_id=False,
                       user_id=[2], create_uid=[2], write_date='v')
            site['events'][92] = row
            return 92
        if (model, method) == (EVENT, 'read'):
            return [deepcopy(site['events'][i]) for i in args[0]]
        if model == 'mrp.workcenter.productivity.loss':
            return [3]
        if model == 'res.users':
            assert args[0] == [['login', '=', 'maintenance']]
            return [2]
        if model == CENTER:
            fault = site['events'].get(91)
            return [dict(id=4, working_state='blocked' if fault and not fault['date_end'] else 'normal')]
        if (model, method) == (WO, 'search_count'):
            return int(site['source'] == 'east' and not site['cancelled'])
        if model == WO:
            return [dict(id=1, workcenter_id=[4], production_id=[1])]
        if model == MO:
            return [dict(id=1, origin=site['origin'], product_id=[2], product_qty=1, product_uom_id=[1, 'Units'],
                         state='cancel' if site['cancelled'] else 'progress')]
        if model == 'product.product':
            return [dict(id=2, default_code=site['source'].upper())]
        return MES(site['source']).call(model, method, *args, **kw)

    def mutator(self):
        if self.login != 'maintenance' or self.site.get('deny'):
            raise Fault(403, 'MES denied equipment mutation')


class MaintenanceTickets:
    """Offline HTTP transport with issue/comment numbers local to each repository."""
    def __init__(self):
        self.issues, self.comments = {}, {}

    def request(self, request, **kwargs):
        url = urlsplit(request.full_url)
        site = url.hostname.split('.')[0]
        assert url.path.startswith('/api/v1/repos/plant/maintenance/issues')
        tail = url.path.removeprefix('/api/v1/repos/plant/maintenance/issues')
        method = request.method
        actor = request.get_header('Authorization').removeprefix('token ')
        scope, role = actor.split('-', 1)
        assert scope == site
        comments = self.comments.setdefault(site, [])
        if method == 'GET':
            if tail == '':
                return [deepcopy(self.issues[site])] if site in self.issues else []
            if tail == '/1/comments':
                return deepcopy(comments)
            if tail.startswith('/comments/'):
                return deepcopy(comments[int(tail.rsplit('/', 1)[1]) - 1])
            assert tail == '/1'
            return deepcopy(self.issues[site])
        data = json.loads(request.data)
        issue_url = f'{url.scheme}://{url.netloc}/plant/maintenance/issues/1'
        if tail == '':
            assert role == 'ticket_operator' and site not in self.issues
            self.issues[site] = dict(data, number=1, html_url=issue_url, created_at='2027-01-01T00:00:00Z',
                                     state='open')
            return deepcopy(self.issues[site])
        if method == 'PATCH':
            assert role == 'ticket_operator' and tail == '/1'
            self.issues[site].update(data)
            return deepcopy(self.issues[site])
        assert tail == '/1/comments' and role != 'reader'
        login = ACTORS.get(role, 'ticket-operator')
        if role == 'tester' and site == 'west' and self.deny_west_tester:
            from urllib.error import HTTPError
            raise HTTPError(request.full_url, 403, 'tester denied', None, None)
        comment = dict(data, user={'login': login}, id=len(comments) + 1, issue_url=issue_url,
                       created_at='2027-01-02T00:00:00Z', updated_at='2027-01-02T00:00:00Z')
        comments.append(comment)
        return deepcopy(comment)


class ProductionProfiles(TestCase):
    def test_family_shares_legacy_primary_plm_identity(self):
        primary = dict(source='company', plm={'url': 'http://plm/'},
                       mes=dict(url='http://mes', db='mes', source_id='mes'))
        family = dict(primary, item='EAST', source='company-plm', plm_source='company-plm')
        Authority.check_sources(family, primary)
        with self.assertRaises(Pending):
            Authority.check_sources(dict(family, plm={'url': 'http://other'}), primary)

    def test_cli_admits_submitted_orders_and_planner_dispatches_to_both_sites(self):
        self.exercise_sites(operating=False)

    def test_ordinary_cli_runs_both_registered_sites_across_lost_replies(self):
        self.exercise_sites(operating=True)

    def test_ordinary_cli_isolates_multiday_operator_outages_and_resumes(self):
        self.exercise_sites(operating=True, outage=True)

    def test_ordinary_cli_restarts_from_posted_partial_and_continues_native_backorder(self):
        self.exercise_sites(operating=True, bounded=True)

    def test_input_staging_reuses_verified_legacy_stock_only_at_its_original_site(self):
        from integrations.company_run.input_staging import stage

        state = {'work': {}}
        lot = dict(batch='opening', item='BULK', uom='Nos', qty=20)
        inp = dict(batch='opening', item='BULK', uom='Nos', qty=2, disposition={'qty': 20})
        flow = SimpleNamespace(state=state, available=lambda *_: (20, inp['disposition']))
        engine = SimpleNamespace(company_erp=flow, persist=lambda: None)

        def setup(key, source, *, scoped=True):
            work = dict(key=key, request={'key': key}, inputs=[inp], dispatch={'source': source, 'id': len(state['work']) + 1},
                        scoped_mes_staging=scoped)
            state['work'][key] = work
            site = sites[source]
            return work, SimpleNamespace(e=engine, mes=site.client('planner'), cfg=dict(company_id=1, stock=8))

        sites = {source: OperatingMES(source) for source in ('east', 'west')}
        for site in sites.values():
            site.lose_receipt = False
            site.lots[1] = {'product_id': 3}
        east = sites['east']
        east.legacy_receipts.append(east.receipt_line(1, 8, 20, 42))
        original, actor = setup('old', 'east', scoped=False)
        new, _ = setup('new', 'east')
        other, west_actor = setup('other', 'west')
        with self.assertRaisesRegex(Pending, 'Historical cross-site'):
            stage(west_actor, other, inp, lot, 3, 1)
        stage(actor, original, inp, lot, 3, 1)
        stage(actor, new, inp, lot, 3, 1)
        ledger = state['mes_input_staging']['opening']
        self.assertEqual([ledger[key]['legacy'] for key in ('old', 'new')], [42, 42])
        with self.assertRaisesRegex(Pending, 'could not be verified'):
            stage(west_actor, other, inp, lot, 3, 1)
        self.assertTrue(all(not site.staging_writes for site in sites.values()))

        fresh = dict(lot, batch='fresh', qty=4)
        draw = dict(inp, batch='fresh', disposition={'qty': 4})
        flow.available = lambda *_: (4, draw['disposition'])
        for site in sites.values():
            site.lots[2] = {'product_id': 3}
        for key, source in [('first', 'east'), ('second', 'east'), ('excess', 'west')]:
            job, runner = setup(key, source)
            job['inputs'] = [draw]
            if key == 'excess':
                with self.assertRaisesRegex(Pending, 'would exceed'):
                    stage(runner, job, draw, fresh, 3, 2)
            else:
                stage(runner, job, draw, fresh, 3, 2)
                stage(runner, job, draw, fresh, 3, 2)
        self.assertEqual((len(east.staging_writes), east.staged(2)), (2, 4))
        self.assertEqual(sites['west'].staging_writes, [])

        first = state['work']['first']
        ref = state['mes_input_staging']['fresh']['first']['receipt']
        for field, wrong in [('quantity', 20), ('lot_id', [1]), ('location_dest_id', [9])]:
            with self.subTest(field=field), patch.dict(east.receipts[ref], {field: wrong}):
                with self.assertRaisesRegex(Pending, 'receipt differs'):
                    stage(actor, first, draw, fresh, 3, 2)
        for field, wrong in [('quantity', 1), ('product_uom_qty', 3), ('is_inventory', False),
                             ('product_uom', 9), ('norvale_staging_receipt', 'other')]:
            with self.subTest(field=field), patch.dict(east.receipts[ref]['move'], {field: wrong}):
                with self.assertRaisesRegex(Pending, 'allocated stock move'):
                    stage(actor, first, draw, fresh, 3, 2)
        self.assertEqual(len(east.staging_writes), 2)

    def exercise_sites(self, *, operating, outage=False, bounded=False):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run/'env').mkdir()
            (run/'env/site-created').touch()
            (run/'env/endpoints.json').write_text('{"erp":"http://erp.test","site":"site"}')
            cfg = dict(company.DEFAULT_CONFIG, days=3, company_authority=FACTORY)
            if operating:
                cfg.update(company_operating=True, company_timing=dict(
                    shifts={'PRODUCTION': [[720, 722], [750, 760]], 'LAB': [[540, 600]]}, lab_minutes=30))
                (run/'env/company-sources.json').write_text(json.dumps(dict(
                    source='primary-plm', plm={'url': 'http://primary-plm.test'},
                    qms_reader={'url': 'http://primary-qms.test', 'site': 'qms'},
                    mes={'source_id': 'primary', 'url': 'http://primary-mes.test', 'db': 'mes'},
                    operator={'source_id': 'primary'}, products={})))
                (run/'env/controller-token.json').write_text('{"token":"offline"}')
            (run/'config.json').write_text(json.dumps(cfg))
            engine = company.Engine.__new__(company.Engine)
            engine.run, engine.config, engine.day, engine.event = run, cfg, 0, 'setup'
            engine.additions = None
            engine.wh = {'rm': 'RM', 'wip': 'WIP', 'hold': 'Hold', 'fg': 'FG'}
            engine.advance_clock = lambda target: None
            engine.log = lambda *args, **kwargs: None
            engine.inventory = lambda role: {('BULK', 'RM'): 20, ('PACK', 'RM'): 20}
            erp = ERP()
            for item in ('EAST', 'WEST'):
                erp.docs['BOM', 'BOM-' + item] = dict(item=item, docstatus=1, is_active=1,
                    quantity=1, items=[dict(item_code=code, stock_uom='Nos', qty=1)
                                       for code in ('BULK', 'PACK')])
            engine.api = {role: erp for role in ('SALES', 'PLANNER', 'IT_ADMIN', 'PRODUCTION')}
            engine.admin = erp
            original = {'item': 'OLD', 'qty': 12}
            if operating:
                original['production_profile'] = 'production-OLD'
            engine.docs = {'old': {'doctype': 'Sales Order', 'name': 'original'}}
            engine.state = {'day': 0, 'completed': ['seed', 'recurring-setup'], 'docs': engine.docs,
                            'physical': {}, 'ops': {'maintenance': {}, 'orders': {'old': original}, 'meter': 0}}
            authority = Authority(run)
            flow = engine.company_erp = CompanyERP(engine, authority)
            for batch, item in (('bulk-lot', 'BULK'), ('pack-lot', 'PACK')):
                flow.state['lots'][batch] = dict(batch=batch, item=item, qty=20, uom='Nos', kind='opening')
            engine.persist()
            before = (run/'state.json').read_bytes()
            registry = run/'operator.json'
            profiles = {site: dict(approved_profile(site), maintenance=maintenance_scope(site))
                        for site in ('east', 'west')}
            if operating:
                for site, profile in profiles.items():
                    profile['mes_operator'] = dict(login='operator', password_env=site.upper() + '_OPERATOR_SECRET')
            if bounded:
                profiles['east']['production_batch'] = dict(target=1, backorder_not_before=2)
                profiles['west']['production_batch'] = dict(target=1)
            registry.write_text(json.dumps(profiles))
            command = run/'command.json'
            event = lambda site, role='add-on-sales': {
                'id': site, 'kind': 'order', 'at': 1440 + 600, 'role': role,
                'payload': {'case': 'PO-' + site, 'product': site, 'quantity': 2, 'rate': 5}}
            systems = {'production-' + site.upper(): {'connection': site} for site in profiles}
            extension = {
                'id': 'add-families', 'systems': systems,
                'products': {site: {'system': 'production-' + site.upper(), 'item': site.upper()}
                             for site in profiles},
                'roles': {'add-on-sales': {'permissions': ['write:' + s + ':order' for s in systems],
                                           'shifts': [[540, 720]]},
                          'unapproved': {'permissions': ['write:production-EAST:order'],
                                         'shifts': [[540, 720]]}},
                'events': [event('east'), event('west')],
            }
            def submit(addition):
                command.write_text(json.dumps(addition))
                generate.main(['--run', str(run), '--extend', str(command)])

            sites = {site: OperatingMES(site, bounded=bounded) if operating else MES(site)
                     for site in profiles}

            def connect_mes(**cfg):
                site = sites[cfg['source_id']]
                if operating:
                    site = sites[urlsplit(cfg['url']).hostname.split('.')[0]]
                    if cfg['login'] == 'operator':
                        self.assertEqual(cfg['source_id'], site.source_id)
                    self.assertEqual(cfg['db'], 'mes')
                    password = site.source_id + '-operator-only' if cfg['login'] == 'operator' else 'never-persist'
                    self.assertEqual(cfg['password'], password)
                    return site.client(cfg['login'])
                return site

            qms_calls = []

            class QMS:
                def __init__(self, url, **kwargs):
                    qms_calls.append(url)

                def release(self, batch, spec, qty, uom):
                    self.doc = dict(name='QI-1', modified='v', modified_by='qa',
                        item_code=batch, batch_no=batch, custom_spec_revision=spec, report_date='2027-01-02',
                        custom_lot_quantity=qty, custom_lot_uom=uom, docstatus=1,
                        status='Accepted', workflow_state='Released')
                    return self.doc

                def get(self, doctype, name):
                    return self.doc

            with patch.dict(os.environ, NORVALE_CONNECTIONS_FILE=str(registry), SECRET='never-persist',
                            EAST_OPERATOR_SECRET='east-operator-only', WEST_OPERATOR_SECRET='west-operator-only'), \
                 patch('integrations.company_run.authority.PLM', PLM), \
                 patch('integrations.company_run.authority.Mes', side_effect=connect_mes), \
                 patch('integrations.company_run.authority.ErpClient', return_value=erp), \
                 patch('integrations.company_run.authority.QMS', QMS), \
                 patch('integrations.revisions.receivers.ERPNext.verify',
                       side_effect=lambda pk, marker, plan: {'bom_id': pk, 'import_key': marker}), \
                 patch('integrations.revisions.receivers.Odoo.verify',
                       side_effect=lambda pk, marker, plan: {
                           'bom_id': int(pk), 'import_key': marker, 'revision': 'fingerprint'}):
                with self.assertRaises(SystemExit):
                    submit({**extension, 'events': [event('east', 'unapproved')]})
                self.assertEqual(registered(run), {})
                self.assertEqual((run/'state.json').read_bytes(), before)
                self.assertFalse((run/'revision-pins.sqlite').exists())
                profiles['west']['mes']['source_id'] = 'east'
                registry.write_text(json.dumps(profiles))
                with self.assertRaises(SystemExit):
                    submit(extension)
                self.assertEqual(registered(run), {})
                profiles['west']['mes']['source_id'] = 'west'
                if operating:
                    for field, reserved in (('source', 'primary-plm-plm'), ('qms_source', 'primary-plm-qms')):
                        invalid = deepcopy(profiles)
                        invalid['west'][field] = reserved
                        registry.write_text(json.dumps(invalid))
                        with self.assertRaises(SystemExit):
                            submit(extension)
                        self.assertEqual(registered(run), {})
                        self.assertEqual((run/'state.json').read_bytes(), before)
                registry.write_text(json.dumps(profiles))
                submit(extension)
                submit(extension)
                self.assertEqual((run/'state.json').read_bytes(), before)
                self.assertEqual(set(registered(run)), set(systems))
                self.assertNotIn(b'never-persist', (run/'scheduled-additions/engine.sqlite3').read_bytes())

                engine.day, engine.event = 1, 'd0001:production'
                erp.lose_submit = True
                with self.assertRaises(TimeoutError):
                    engine.run_additions(1440 + 720)
                engine.state = json.loads((run/'state.json').read_text())
                engine.docs = engine.state['docs']
                self.assertEqual(engine.state['ops']['orders'], {'old': original})
                engine.additions = None
                engine.run_additions(1440 + 720)
                engine.run_additions(1440 + 720)
                self.assertEqual(engine.state['ops']['orders']['old'], original)
                sales = [row for (dt, _), row in erp.docs.items() if dt == 'Sales Order']
                self.assertEqual(len(sales), 3)
                self.assertTrue(all(row['docstatus'] == 1 for row in sales))
                self.assertEqual(len(open_catalog(run, cfg, engine.state).history()), 4)

                flow.available = lambda *args: (20, {'id': 'raw-release'})
                flow.balance = lambda *args: 20
                flow.verify_row = lambda *args: None
                if operating:
                    self.run_operating(engine, erp, sites, connect_mes, outage=outage, bounded=bounded)
                    return
                engine.production()
                works = sorted(flow.state['work'].values(), key=lambda work: work['item'])
                self.assertEqual([w['equipment'] for w in works], ['east:4', 'west:4'])
                self.assertEqual([w['dispatch']['id'] for w in works], ['1', '1'])
                self.assertEqual([w['dispatch']['source'] for w in works], ['east', 'west'])
                for work in works:
                    self.assertEqual(authority.find_dispatch(work['request']['key'], role='PRODUCTION')['source'],
                                     work['dispatch']['source'])
                    sites[work['dispatch']['source']].done = True
                east = sites['east']
                correct = east.completion(1)
                for override in ({'erp_work_ref': 'different work'}, {'completion_ref': 'west:mrp.production:1'},
                                 {'backorder': {**correct['work'], 'source_id': 'west'}},
                                 {'output_hold_location': {**correct['output_hold_location'], 'native_id': 44}}):
                    with self.subTest(fields=list(override)), patch.object(
                            east, 'completion', return_value={**correct, **override}):
                        with self.assertRaises(Pending):
                            authority.completions(works[0]['dispatch'], as_of=engine.date(), role='PRODUCTION')
                flow.poll_work()
                flow.poll_work()
                self.assertEqual(len(flow.state['completions']), 2)
                lots = [lot for lot in flow.state['lots'].values() if lot['kind'] == 'finished']
                self.assertEqual(len(lots), 2)
                stock = [row for (dt, _), row in erp.docs.items() if dt == 'Stock Entry']
                self.assertEqual({json.loads(row['remarks'])['mes_source'] for row in stock}, {'east', 'west'})
                self.assertTrue(all(row['docstatus'] == 1 for row in stock))
                for work in works:
                    item = work['item']
                    lot = dict(batch=item, item=item, kind='finished', revision=work['revision'], qty=2, uom='Nos')
                    self.assertEqual(authority.disposition(lot, as_of=engine.date(), role='QA')['id'], 'QI-1')
                    with self.assertRaises(Pending):
                        authority.disposition(lot, as_of=engine.date(), role='SALES')
                    with self.assertRaises(Pending):
                        authority.dispatch(work['request'], role='SALES')
                    with self.assertRaises(Pending):
                        authority.completions(work['dispatch'], as_of=engine.date(), role='SALES')
                    with self.assertRaisesRegex(Pending, 'Pinned MES source'):
                        authority.disposition({**lot, 'revision': {**work['revision'], 'mes_source': 'wrong'}},
                                              as_of=engine.date(), role='QA')
                self.assertEqual(qms_calls, ['http://east.qms.test', 'http://west.qms.test'])
                self.check_maintenance(engine, profiles, works)
                profiles['east']['mes']['url'] = 'http://changed.test'
                registry.write_text(json.dumps(profiles))
                with self.assertRaises(Pending):
                    authority.definition('EAST', as_of=engine.date(), role='PLANNER')

    def run_operating(self, engine, erp, sites, connect_mes, *, outage, bounded):
        run = engine.run
        # Real recurring production phase, clock, work dispatch and ERP posting.
        # Inject the endpoints and isolate unrelated recurring phases.
        now = [None]
        lab = []
        if outage:
            from integrations.company_run.timing import schedule
            schedule(engine.additions, ['lab', 'unrelated', None], 'sample', 2 * 1440 + 540, role='LAB')
            # The east start commits on day 1 and its operator remains offline
            # until day 4. West recovers within the original CLI invocation.
            east, west = sites['east'], sites['west']
            east.online = lambda: east.state == 'confirmed' or now[0].day >= 5
            west.online = lambda: west.state != 'done' or now[0].day >= 3

        def before_receipt(ref, lot, location, quantity):
            state = json.loads((run/'state.json').read_text())['company_erp']
            intent, = [entry for ledger in state['mes_input_staging'].values() for entry in ledger.values()
                       if entry['receipt'] == ref]
            self.assertEqual((intent['lot'], intent['location'], intent['qty']), (lot, location, quantity))
            self.assertNotIn('native_line', intent)

        for site in sites.values():
            site.before_receipt = before_receipt

        def initialize(instance, *args, **kwargs):
            for attr in ('wh', 'inventory', 'api', 'admin'):
                setattr(instance, attr, getattr(engine, attr))
            instance.advance_clock = lambda target: now.__setitem__(0, target)
            instance.company_erp.available = lambda *args: (20, {'id': 'raw-release'})
            instance.company_erp.disposition = lambda *args: {'id': 'raw-release'}
            instance.company_erp.balance = lambda *args: 20
            instance.company_erp.verify_row = lambda *args: None

        noops = {phase: lambda _: None for phase in (
            'service', 'arrivals', 'inspections', 'demand', 'fulfillment', 'replenishment',
            'approvals', 'bank_statements', 'settlements', 'management')}
        original_init = company.Engine.__init__

        def fresh(instance, *args, **kwargs):
            original_init(instance, *args, **kwargs)
            initialize(instance)

        erp.login = lambda *args: None

        def unexpected_stage_retry(_):
            raise AssertionError('MES outage fell through to the global retry loop')

        def unrelated_lab(company, clock, *args):
            lab.append(clock.now)
            west, = [work for work in company.company_erp.state['work'].values() if work['item'] == 'WEST']
            self.assertEqual(west['scheduled_labor'], sites['west'].labor)
            self.assertEqual(west['mes_consumption']['output'], 3)
            self.assertFalse(west.get('mes_execution_confirmed'))
            self.assertEqual(Authority(run).completions(
                west['dispatch'], as_of=company.date(), role='PRODUCTION'), [])

        with ExitStack() as stack:
            stack.enter_context(patch.multiple(company.Engine, __init__=fresh, **noops))
            stack.enter_context(patch('company.Stack',
                                      lambda _: SimpleNamespace(endpoints=lambda: {'erp': '', 'site': ''})))
            stack.enter_context(patch('company.ErpClient', return_value=erp))
            for module in ('demo', 'operating'):
                stack.enter_context(patch('integrations.company_run.' + module + '.Mes', side_effect=connect_mes))
            stack.enter_context(patch('socket.create_connection',
                                      side_effect=AssertionError('Unexpected network connection')))
            stack.enter_context(patch('integrations.company_run.timing.queue_samples',
                                      side_effect=AssertionError('Primary lab must not receive site output')))
            stack.enter_context(redirect_stdout(StringIO()))
            stack.enter_context(patch('time.sleep', side_effect=unexpected_stage_retry))
            stack.enter_context(patch('integrations.company_run.timing.operating_quality',
                                      side_effect=unrelated_lab))
            if bounded:
                self.run_bounded(run, erp, sites)
                self.check_staging(sites)
                return
            generate.main(['--run', str(run)])
            if outage:
                from integrations.company_run.operating import Operating
                pending = company.Engine(run)
                east, west = sorted(pending.company_erp.state['work'].values(), key=lambda w: w['item'])
                self.assertEqual((east.get('produced', 0), west['produced']), (0, 2))
                self.assertEqual(lab, [2 * 1440 + 540])
                self.assertNotIn('scheduled_labor', east)
                self.assertEqual(east['mes_start']['at'], 2161)
                self.assertEqual(east['mes_start']['operation'], 1)
                start = deepcopy(east['mes_start'])
                clock = open_catalog(run, pending.config, pending.state)
                slot = clock.state['company_running']['east:4']
                self.assertEqual(slot['owner'][-1], east['key'])
                self.assertNotIn('west:4', clock.state['company_running'])
                sites['east'].online = lambda: True
                actor = Operating(pending).scheduled(east)
                with patch.object(pending.company_erp, 'available', return_value=(0, None)):
                    with self.assertRaisesRegex(Pending, 'no longer has released ERP stock'):
                        actor.start(east, scheduled=True)
                pending.company_erp.equipment.cases['east-fault'] = {'center': 'east:4'}
                with self.assertRaisesRegex(Pending, 'awaits a verified restart'):
                    actor.start(east, scheduled=True)
                self.assertEqual(sites['east'].calls, ['start'])
                sites['east'].online = lambda: now[0].day >= 5
                generate.main(['--run', str(run), '--days', '5'])
                finished = json.loads((run/'state.json').read_text())['company_erp']['work'][east['key']]
                self.assertEqual(finished['mes_start'], start)
            generate.main(['--run', str(run)])

        self.check_staging(sites)
        state = json.loads((run/'state.json').read_text())['company_erp']
        self.assertEqual({w['equipment']: (w['dispatch']['id'], w['produced']) for w in state['work'].values()},
                         {'east:4': ('1', 2), 'west:4': ('1', 2)})
        self.assertEqual(sites['east'].calls, ['start', 'start', 'complete'])
        self.assertEqual(sites['west'].calls, ['start', 'complete', 'complete'])
        for site in sites.values():
            intervals = [[2161, 2162], [2190, 2191]]
            if site.source_id == 'east':
                intervals = [[6480, 6482]] if outage else [[3600, 3602]]
            self.assertEqual(site.labor['intervals'], intervals)
            self.assertEqual(site.labor['minutes'], 2)
        docs = [row for (dt, _), row in erp.docs.items() if dt == 'Stock Entry']
        self.assertEqual(len(docs), 2)
        self.assertEqual({json.loads(row['remarks'])['mes_source'] for row in docs}, {'east', 'west'})
        self.assertTrue(all(row['docstatus'] == 1 for row in docs))
        self.assertEqual([line['t_warehouse'] for row in docs for line in row['items']
                          if line.get('is_finished_item')], ['Hold', 'Hold'])
        self.assertEqual({lot['mes_lot_id'] for lot in state['lots'].values() if lot['kind'] == 'finished'},
                         {'east:stock.lot:3', 'west:stock.lot:3'})
        self.assertNotIn(b'operator-only', (run/'state.json').read_bytes())

    def check_staging(self, sites):
        for lot in (1, 2):
            self.assertEqual(sum(site.staged(lot) for site in sites.values()), 4)
        self.assertFalse(sites['east'].lose_receipt)
        for site in sites.values():
            self.assertEqual(len(site.staging_writes), 2)
            self.assertEqual(set(site.staging_writes), set(site.receipts))

    def run_bounded(self, run, erp, sites):
        def invoke():
            generate.main(['--run', str(run)])

        def works():
            state = json.loads((run/'state.json').read_text())['company_erp']
            return {w['dispatch']['source']: w for w in state['work'].values()}

        post = CompanyERP.post_completion
        def post_then_cut(flow, key, operation):
            post(flow, key, operation)
            if operation['report']['id'] == 'east:mrp.production:1':
                raise InterruptedError('restart after ERP partial posting')

        with patch.object(CompanyERP, 'post_completion', post_then_cut):
            with self.assertRaisesRegex(InterruptedError, 'after ERP partial posting'):
                invoke()
        east = works()['east']
        self.assertEqual((east['dispatch']['id'], east['produced'], east['used']),
                         ('1', 1, {'bulk-lot': 1, 'pack-lot': 1}))
        self.assertEqual(sites['east'].orders[2]['state'], 'confirmed')
        from integrations.company_run.production_batch import backorder
        sites['east'].orders[2]['origin'] = next(iter(sites['west'].work))
        engine = company.Engine(run)
        with self.assertRaisesRegex(Pending, 'does not match the posted original ERP work'):
            backorder(engine, east)
        del sites['east'].orders[2]['origin']

        with self.assertRaisesRegex(InterruptedError, 'restart after lost native reply'):
            invoke()
        east = works()['east']
        child = east['production_operations']['2']
        self.assertEqual(child['scheduled_labor'], sites['east'].posted[2][3])
        self.assertNotIn('completed_day', child)
        self.assertEqual(len(Authority(run).completions(east['dispatch'], as_of='2027-01-03', role='PRODUCTION')), 1)

        invoke()
        invoke()
        current = works()
        self.assertEqual({source: w['produced'] for source, w in current.items()}, {'east': 2, 'west': 1})
        self.assertEqual(sites['west'].orders[2]['state'], 'confirmed')
        self.assertNotIn(2, sites['west'].reservations)
        self.assertEqual(sites['east'].reservations[2], [{'lot_id': 1, 'quantity': 1}, {'lot_id': 2, 'quantity': 1}])
        self.assertEqual(sites['east'].calls, [('post', 1), ('replay', 1), ('post', 2), ('replay', 2)])
        self.assertEqual(sites['west'].calls, [('post', 1)])
        root, child = (current['east']['production_operations'][str(mo)] for mo in (1, 2))
        self.assertEqual((root['scheduled_labor']['minutes'], child['scheduled_labor']['minutes']), (1, 1))
        self.assertNotEqual(root['scheduled_labor']['event'], child['scheduled_labor']['event'])
        self.assertEqual(current['east']['used'], {'bulk-lot': 2, 'pack-lot': 2})
        docs = [doc for (doctype, _), doc in erp.docs.items() if doctype == 'Stock Entry']
        self.assertEqual({json.loads(doc['remarks'])['completion_id'] for doc in docs},
                         {'east:mrp.production:1', 'east:mrp.production:2', 'west:mrp.production:1'})
        self.assertEqual(len(docs), 3)
        for doc in docs:
            self.assertEqual((doc['docstatus'], doc['fg_completed_qty']), (1, 1))
            self.assertEqual([row['qty'] for row in doc['items']], [1, 1, 1])

    def check_maintenance(self, engine, profiles, works):
        east, west = works
        engine.config.update(days=10, maintenance_days=1, maintenance_units=2)
        flow = engine.company_erp
        sites = {site: dict(source=site, events={}, cancelled=False) for site in (*profiles, 'primary')}
        primary_mes = dict(profiles['east']['mes'], url='http://primary.test', source_id='primary')
        (engine.run/'env/company-sources.json').write_text(json.dumps(
            {'mes': primary_mes, 'operator': primary_mes, 'equipment_centers': [4]}))
        sites['east'].update(lose_reply=True, lose_cancel=True, origin='WO-INTERRUPTED')
        sites['west']['deny'] = True
        interrupted = {**deepcopy(east), 'key': 'east-interrupted', 'qty': 1, 'produced': 0}
        interrupted['request']['work_order'] = sites['east']['origin']
        flow.state['work'][interrupted['key']] = interrupted
        unchanged = deepcopy([east, west])
        legacy = dict(ticket=3, meter=2, closed=1)
        primary_case = {'center': 4, 'day': 0, 'meter': 2, 'reason': 'primary checkpoint',
                        'ticket': {'number': 1, 'html_url': '/primary/issues/1'},
                        'fault': {'source_id': 'primary', 'native_id': 91}, 'restart': {'native_id': 91},
                        'verification': {'ref': 'existing', 'id': 3}}
        engine.state['ops']['maintenance']['primary'] = legacy
        flow.equipment.cases['primary-case'] = primary_case
        engine.state['completed'] = [key for day, key, _ in engine.recurring_events()
                                     if day < 2 or not key.endswith(':service')]
        engine.persist()

        def service(day):
            event = f'd{day:04}:service'
            self.assertEqual(engine.step(expected=event)['event'], event)
            self.assertTrue(engine.step(expected=event)['already_completed'])
            self.assertEqual(flow.equipment.available(4, 'PRODUCTION'), 4)
            return {c['center']: c for c in flow.equipment.cases.values() if isinstance(c['center'], str)}

        qms_urls = []
        batch = None

        rationale = 'Disposition of interrupted east:mrp.production:1.'
        reading = dict(numeric=1, specification='assay', min_value=5, max_value=6,
                       reading_1='7.2', status='Rejected')

        class Disposition:
            def __init__(self, url, **kw):
                qms_urls.append(url)

            def list(self, doctype, filters, **kw):
                if not batch:
                    return []
                if doctype == CASE:
                    assert filters == {'custom_qms_batch': batch, 'custom_failed_inspection': 'QI-EAST'}
                    return [{'name': 'CASE-EAST'}]
                assert doctype == INSPECTION and filters == {'batch_no': batch}
                return [dict(name='QI-EAST', modified='pinned', creation='now')]

            def get(self, doctype, name):
                return dict(name=name, modified='pinned', docstatus=1, status='Rejected',
                            workflow_state='Lot Rejected', modified_by='qa', batch_no=batch,
                            item_code='EAST', custom_spec_revision=interrupted['revision']['inspection_spec'],
                            report_date='2027-01-04', custom_lot_uom='Nos',
                            custom_lot_quantity=1, custom_decision_rationale=rationale, readings=[reading],
                            **{OP: batch + ':interrupted'})

        def mes(**cfg):
            return MaintenanceMES(sites[cfg['source_id']], cfg['login'])

        credentials = {env: site + '-' + field.removesuffix('_env')
                       for site, cfg in profiles.items() for field, env in cfg['maintenance'].items()
                       if field.endswith('_env')}
        west_password = profiles['west']['maintenance']['password_env']
        west_secret = credentials.pop(west_password)
        tickets = MaintenanceTickets()
        tickets.deny_west_tester = True
        with patch.dict(os.environ, credentials), \
             patch('integrations.company_run.authority.Mes', side_effect=mes), \
             patch('integrations.company_run.maintenance.Mes', side_effect=mes), \
             patch('integrations.company_run.maintenance.QMS', Disposition), \
             patch('router.request_json', side_effect=tickets.request), \
             patch('integrations.company_run.maintenance_staff.vibration', return_value=370):
            cases = service(2)
            self.assertEqual(set(cases), {'east:4', 'west:4'})
            self.assertFalse(sites['west']['events'])
            self.assertEqual(set(sites['east']['events']), {91})
            self.assertFalse(cases['east:4']['fault'])
            self.assertFalse(tickets.issues)
            for center in cases:
                with self.assertRaises(Pending):
                    flow.equipment.available(center, 'PRODUCTION')
            os.environ[west_password] = west_secret
            cases = service(3)
            self.assertFalse(cases['west:4']['fault'])
            self.assertEqual(cases['east:4']['fault']['native_id'], 91)
            self.assertFalse(cases['east:4'].get('disposition'))
            self.assertEqual(cases['east:4']['interrupted']['completion_ref'], 'east:mrp.production:1')
            with self.assertRaises(Pending):
                flow.equipment.guard_execution('east:mrp.production:1')
            flow.equipment.guard_execution('west:mrp.production:1')
            batch = cases['east:4']['interrupted']['batch']
            case = cases['east:4']
            valid_rationale = rationale
            for rationale in ('east:mrp.production:10', 'other-east:mrp.production:1'):
                with self.assertRaises(Pending):
                    flow.equipment.dispose(case)
            rationale = valid_rationale
            reading.update(reading_1='5.5', status='Accepted')
            with self.assertRaises(Pending):
                flow.equipment.dispose(case)
            self.assertFalse(sites['east']['cancelled'])
            self.assertNotIn('qa_evidence', case)
            reading.update(reading_1='7.2', status='Rejected')
            sites['west']['deny'] = False
            cases = service(4)
            self.assertTrue(sites['east']['cancelled'])
            self.assertFalse(cases['east:4'].get('disposition'))
            self.assertTrue(cases['east:4']['qa_evidence'])
            self.assertFalse(sites['west']['cancelled'])
            self.assertEqual({case['ticket']['number'] for case in cases.values()}, {1})
            self.assertEqual(len({case['ticket']['source_id'] for case in cases.values()}), 2)
            self.assertEqual({case['fault']['native_id'] for case in cases.values()}, {91})
            self.assertEqual(len({case['fault']['source_id'] for case in cases.values()}), 2)
            cases = service(5)
            self.assertEqual(cases['east:4']['disposition']['source_id'], 'east')
            for day in (6, 7):
                cases = service(day)
            self.assertTrue(cases['east:4']['restart'])
            east_completed = deepcopy(cases['east:4'])
            self.assertFalse(cases['west:4'].get('restart'))
            self.assertEqual(tickets.issues['east']['state'], 'closed')
            self.assertEqual(tickets.issues['west']['state'], 'open')
            self.assertEqual(flow.equipment.available('east:4', 'PRODUCTION'), 'east:4')
            with self.assertRaises(Pending):
                flow.equipment.available('west:4', 'PRODUCTION')
            tickets.deny_west_tester = False
            service(8)
            cases = service(9)
            self.assertTrue(cases['west:4']['restart'])
            self.assertEqual(cases['east:4'], east_completed)
            self.assertEqual(tickets.issues['west']['state'], 'closed')
            self.assertEqual(flow.equipment.available('west:4', 'PRODUCTION'), 'west:4')
            self.assertTrue(all(set(sites[source]['events']) == {91, 92} for source in profiles))
            self.assertFalse(sites['primary']['events'])
            self.assertEqual([row['user']['login'] for row in tickets.comments['west'][:3]],
                             [ACTORS[role] for role in ('technician', 'tester', 'verifier')])
            self.assertEqual(set(qms_urls), {'http://east.qms.test'})
            self.assertEqual(engine.state['ops']['maintenance']['primary'], legacy)
            self.assertEqual(flow.equipment.cases['primary-case'], primary_case)
            self.assertEqual([flow.state['work'][w['key']] for w in unchanged], unchanged)
            self.assertEqual(flow.state['work'][interrupted['key']]['rejected_remainder'], 1)
            self.assertFalse(any(value.encode() in (engine.run/'state.json').read_bytes()
                                 for value in credentials.values()))
