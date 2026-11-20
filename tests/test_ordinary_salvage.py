"""Offline ordinary salvage qualification across native protocol boundaries."""
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from integrations.company_run.authority import Authority
from integrations.company_run.quality import finished_specification
from integrations.company_run.test_registered_lab import catalog_spec, host_profile
from integrations.company_run.timing import DEFAULT_TIMING
from integrations.lake.boundary import Frappe
from integrations.mes.client import LINE, LOT, MO, Mes
from integrations.qms.adapter import INSPECTION, STATES
from integrations.qms.sample_reference import COMPANY, WAREHOUSE
from integrations.revisions.receivers import ERP_SITE
from native.company import Engine, save
from native.recurring_extensions import MIDNIGHT
from norvale_goals_fixture import Authority as FixtureAuthority, CompanyFixture, ERP


class StockERP(ERP):
    """Expose native row bundle links as well as the fixture's ledger links."""

    base, site = 'https://erp.example', 'erp.localhost'

    def metadata(self, entity):
        assert entity == 'BOM'
        return {'fields': [dict(fieldname=ERP_SITE, read_only=1, default='00000000000040008000000000000001')]}

    def create(self, kind, fields):
        if kind == 'Stock Entry':
            fields = dict(fields, purpose=fields['stock_entry_type'])
        return super().create(kind, fields)

    def stock(self, doc, row, warehouse, sign):
        super().stock(doc, row, warehouse, sign)
        row['serial_and_batch_bundle'] = f"Serial and Batch Bundle-{self.counters['Serial and Batch Bundle']}"


class QualitySite:
    """Serve Frappe resource and workflow replies from independent QMS records."""

    def __init__(self, authority, reference, revoke_on_acquire, *, deny_qa=False):
        self.authority = authority
        self.docs = {('Stock Entry', reference['name']): deepcopy(reference)} if reference else {}
        self.deny_qa = deny_qa
        self.serial = 0
        self.revoke_on_acquire = revoke_on_acquire

    def touch(self, doc, actor):
        self.serial += 1
        doc.update(modified=f'qms-{self.serial:04}', modified_by=actor)

    def open(self, request, *, timeout):
        actor = request.headers['Authorization'].split()[1].split(':')[0]
        url = urlsplit(request.full_url)
        data = json.loads(request.data) if request.data else None
        return BytesIO(json.dumps(self.reply(actor, request.get_method(), url, data)).encode())

    def reply(self, actor, method, url, data):
        path = [unquote(part) for part in url.path.split('/')[2:]]
        if path == ['method', 'frappe.auth.get_logged_user']:
            return {'message': 'unauthorized' if self.deny_qa and actor == 'qa' else actor}
        if path == ['method', 'norvale_dispatch_lease']:
            self.assert_actor(actor, 'dispatcher')
            request, outcome = data['lease'], data['outcome']
            batch = request['scopes'][0]['batch']
            if outcome == 'active' and self.revoke_on_acquire:
                self.revoke_on_acquire = False
                self.authority.revoke_transfer_inspection(batch)
            return {'message': self.authority.qms_lease(request, outcome=outcome, commit=data.get('commit'))}
        if path == ['method', 'frappe.model.workflow.apply_workflow']:
            doc = self.docs[INSPECTION, data['doc']['name']]
            action = data['action']
            self.assert_actor(actor, 'lab' if action == 'Record' else 'qa')
            assert doc.get('workflow_state') == ('Draft' if action == 'Record' else 'Recorded')
            if action == 'Record':
                reading, = doc['readings']
                status = 'Accepted' if reading['min_value'] <= float(reading['reading_1']) <= reading['max_value'] else 'Rejected'
                reading['status'] = doc['status'] = status
            else:
                assert doc['status'] == ('Accepted' if action == 'Release Lot' else 'Rejected')
            doc.update(workflow_state=STATES[action], docstatus=1)
            self.touch(doc, actor)
            self.authority.inspections[doc['batch_no']] = doc
            return {'message': doc}

        assert path[0] == 'resource', path
        entity = path[1]
        if method == 'GET':
            return resource(self.docs | {
                ('Norvale Dispatch Lease', name): row for name, row in self.authority.release_leases.items()
            }, url)
        self.assert_actor(actor, 'reference' if entity in ('Batch', 'Stock Entry') else 'lab')
        if method == 'POST':
            name = f'{entity}-{1 + sum(dt == entity for dt, _ in self.docs)}'
            doc = dict(data, name=name, owner=actor, docstatus=0, creation=f'qms-{self.serial:04}')
            if entity == 'Batch':
                name = doc['name'] = doc['batch_id']
            if entity == 'Stock Entry':
                for row in doc['items']:
                    row.update(name='QMS-ROW-1', stock_uom='Nos')
            self.docs[entity, name] = doc
            if entity == INSPECTION:
                doc['workflow_state'] = 'Draft'
                self.authority.inspections[doc['batch_no']] = doc
        else:
            assert method == 'PUT' and entity == INSPECTION
            doc = self.docs[entity, path[2]]
            assert doc['docstatus'] == 0
            doc.update(data)
        self.touch(doc, actor)
        return {'data': doc}

    @staticmethod
    def assert_actor(actual, expected):
        assert actual == expected, (actual, expected)


def resource(docs, url):
    _, _, _, entity, *name = [unquote(part) for part in url.path.split('/')]
    if name:
        return {'data': docs[entity, name[0]]}
    query = parse_qs(url.query)
    filters = json.loads(query.get('filters', ['{}'])[0])
    assert isinstance(filters, dict), filters
    fields = json.loads(query['fields'][0])
    rows = [doc for (kind, _), doc in docs.items()
            if kind == entity and all(doc.get(k) == v for k, v in filters.items())]
    newest = query['order_by'][0].startswith('creation desc')
    rows.sort(key=lambda row: (row.get('creation', '') if newest else '', row['name']), reverse=newest)
    start = int(query.get('limit_start', ['0'])[0])
    limit = int(query['limit_page_length'][0])
    return {'data': [{k: row.get(k) for k in fields} for row in rows[start:start + limit]]}


class MesSite:
    """XML-RPC fixture serving Odoo work, movement, lot and scrap records."""

    def __init__(self, source, item='FG', *, bridge_group=True):
        self.settings = dict(url=f'https://{source}.example', db='plant', source_id=source,
                             login='fixture', password='fixture')
        self.docs = {('product.product', 5): dict(id=5, default_code=item),
                     ('product.product', 6): dict(id=6, default_code='RM')}
        self.execution = None
        self.bridge_group = bridge_group
        self.rework_calls = []

    def order(self, identifier, input_lot, output_lot, quantity, *, input_product=5):
        recorded = f'2027-01-01T{7 if identifier == 1 else 11:02}:00:00Z'
        self.docs[MO, identifier] = dict(id=identifier, origin='WO-1', state='done',
            norvale_revision='1', bom_id=[11, 'BOM'], company_id=[4, 'Plant'], workorder_ids=[],
            date_finished=recorded, lot_producing_id=[output_lot, f'Lot {output_lot}'],
            location_dest_id=[20, 'Hold'], procurement_group_id=False,
            move_raw_ids=[2 * identifier - 1], move_finished_ids=[2 * identifier])
        for lot, product, line in ((input_lot, input_product, 2 * identifier - 1),
                                   (output_lot, 5, 2 * identifier)):
            self.docs.setdefault((LOT, lot), dict(id=lot, name=f'Lot {lot}', product_id=[product, 'Item']))
            self.docs['stock.move', line] = dict(id=line, move_line_ids=[line])
            self.docs[LINE, line] = dict(id=line, state='done', lot_id=[lot, f'Lot {lot}'],
                product_id=[product, 'Item'], product_uom_id=[2, 'Units'], quantity=quantity,
                location_id=[20, 'Hold'], location_dest_id=[20, 'Hold'],
                date=recorded, write_date=recorded, consume_line_ids=[2 * identifier - 1],
                produce_line_ids=[2 * identifier])

    def authenticate(self, db, login, password, context):
        assert (db, context) == ('plant', {})
        assert (login, password) in {('fixture', 'fixture'), ('operator', 'operator'), ('rework', 'rework')}
        return {'fixture': 1, 'operator': 2, 'rework': 3}[login]

    def execute_kw(self, db, uid, password, model, method, args, kwargs):
        assert db == 'plant' and password == {1: 'fixture', 2: 'operator', 3: 'rework'}[uid]
        if (model, method) == ('res.users', 'has_group'):
            assert args == ([uid], 'norvale_mes.group_rework_bridge')
            return uid == 3 and self.bridge_group
        if method == 'read':
            return [deepcopy(self.docs[model, identifier]) for identifier in args[0]]
        if method == 'fields_get':
            return {field: {} for field in args[0]} if model == MO else {}
        assert model == MO, (model, method)
        if method == 'search':
            assert args == ([['norvale_revision', '!=', False], ['state', '=', 'done']],)
            return sorted(identifier for entity, identifier in self.docs if entity == MO)
        if method == 'search_read':
            assert args == ([['norvale_rework_of', '=', 1]],)
            return [deepcopy(self.execution)] if self.execution else []
        assert method == 'norvale_rework'
        self.rework_calls.append(uid)
        assert uid == 3 and self.bridge_group, 'Only the scoped bridge can execute physical rework'
        identifier, authority, total, salvage = args
        assert identifier == 1 and not self.execution and (total, salvage) == (12, 11)
        self.order(2, 1, 2, salvage)
        self.execution = dict(id=2, state='done', norvale_rework_of=[1, 'Original'],
                              norvale_rework_authority=authority, norvale_rework_scrap=[3, 'Scrap'])
        self.docs[MO, 2].update(self.execution)
        self.docs['stock.scrap', 3] = dict(id=3, state='done', production_id=[2, 'Rework'],
            location_id=[20, 'Hold'], scrap_location_id=[21, 'Scrap'], move_ids=[5])
        self.docs['stock.move', 5] = dict(id=5, move_line_ids=[5])
        self.docs[LINE, 5] = dict(self.docs[LINE, 3], id=5, location_dest_id=[21, 'Scrap'], quantity=1)
        return 2


class OrdinaryCompany(CompanyFixture):
    scheduled_company_event = Engine.scheduled_company_event


class OrdinarySalvage(TestCase):
    def test_scheduled_salvage_transfer_recovers_lost_erp_reply(self):
        self.exercise(revoke=False)

    def test_revocation_at_acquire_denies_salvage_transfer(self):
        self.exercise(revoke=True)

    def test_changed_original_mes_completion_blocks_physical_rework(self):
        self.exercise(revoke=False, change_completion=True)

    def test_registered_sites_require_their_own_mes_bridge(self):
        self.exercise(revoke=False, registered_block='bridge')

    def test_registered_sites_require_their_own_qms_approver(self):
        self.exercise(revoke=False, registered_block='qa')

    def test_registered_rework_requires_the_exact_assay_and_procedure(self):
        for mismatch in ('assay', 'procedure', 'uncommissioned'):
            with self.subTest(mismatch=mismatch):
                self.exercise(revoke=False, registered_block=mismatch)

    def test_registered_rework_keeps_the_original_completion_pin(self):
        self.exercise(revoke=False, registered_block='qa', change_completion=True)

    def exercise(self, *, revoke, change_completion=False, registered_block=None):
        root = Path(self.enterContext(TemporaryDirectory()))
        run = root/'run'
        company = OrdinaryCompany(run, capacity=12)
        company.config.update(seed=18 if registered_block else 4, company_timing=DEFAULT_TIMING)
        company.additions = None
        company.state['ops'].update(purchases={})
        company.erp = company.admin = StockERP(company)
        company.api = dict.fromkeys(company.api, company.erp)
        flow, authority = company.company_erp, company.authority

        # Start at the boundary of a completed manufacture with pinned MES inputs.
        raw = company.receipt('opening-material', 24 if registered_block else 12)
        mesh = MesSite('mes-a')
        other_mes = MesSite('mes-b', 'OTHER' if registered_block else 'FG',
                            bridge_group=registered_block != 'bridge')
        mes_sites = {site.settings['url']: site for site in (mesh, other_mes)}
        self.enterContext(patch('integrations.mes.client.ServerProxy',
            side_effect=lambda url, **kwargs: mes_sites[url.split('/xmlrpc/')[0]]))
        def manufacture(site, item, work, work_order, revision):
            site.order(1, 10, 1, 12, input_product=6)
            site.docs[MO, 1]['origin'] = work_order
            site.docs[LOT, 10]['name'] = raw
            source = site.settings['source_id']
            batch = flow.batch(['MES', source, '1'], item)
            original = Mes(**site.settings).completion(1)
            version = original['recorded_at_utc']
            lot_id = f'{source}:stock.lot:1'
            post = company.write('original-manufacture:' + source, 'Stock Entry', dict(company=company.company,
                stock_entry_type='Manufacture', work_order=work_order, remarks=json.dumps(dict(
                    completion_id=original['completion_ref'], version=version, mes_source=source,
                    dispatch_id='1', output_lot_id=lot_id)),
                items=[dict(batch_no=raw, item_code='RM', qty=12, s_warehouse='Hold'),
                       dict(batch_no=batch, item_code=item, qty=12, is_finished_item=1, t_warehouse='Hold')]),
                'PRODUCTION', True)
            flow.register(batch, item, 12, 'Nos', 'finished', post, revision=revision,
                          source_row=post['items'][1]['name'], work_order=work_order,
                          mes_lot_id=lot_id, completion_id=original['completion_ref'], completion_version=version)
            flow.state['work'][work] = dict(request=dict(work_order=work_order), equipment=1, qty=12, produced=12)
            flow.state['completions'][work] = dict(done=True, work=work, target_id=post['name'], report=dict(
                id=original['completion_ref'], version=version, source=source, revision=revision,
                work_order=work_order, authorized=True, output=dict(lot_id=lot_id, item=item, qty=12, uom='Nos'),
                inputs=[dict(batch=raw, qty=12, uom='Nos')]))
            return batch, post

        def revision(item, site):
            spec = finished_specification(item, 'method-1', site + '-plm',
                                          dict(native_id=12, source_version='version'))
            return dict(inspection_spec=spec, mes_source=site, plm_source=site + '-plm',
                        item=item, id='12', version='version')

        original_revision = revision('FG', 'mes-a') if registered_block else dict(
            inspection_spec='FG-assay', mes_source='mes-a')
        batch, post = manufacture(mesh, 'FG', 'work', 'WO-1', original_revision)
        if registered_block:
            other_batch, _ = manufacture(other_mes, 'OTHER', 'other-work', 'WO-2', revision('OTHER', 'mes-b'))
        company.persist()

        def account(role, site='qms'):
            return dict(url=f'https://{site}.example', api_key=role, api_secret='fixture')

        reference = dict(name='QMS-RECEIPT-1', company=COMPANY, stock_entry_type='Material Receipt',
            docstatus=0, owner='Administrator', remarks=f"ERP Stock Entry {post['name']} batch {batch}",
            items=[dict(name='QMS-ROW-1', batch_no=batch, item_code='FG', qty=12,
                        stock_uom='Nos', t_warehouse=WAREHOUSE)])
        primary = QualitySite(authority, reference, revoke)
        cfg = dict(authority.settings(), qms_reader=account('reader'), qms_dispatcher=account('dispatcher'),
                   erp=dict(base=company.erp.base, site=company.erp.site), plm_source='plm-a', plm=account('reader', 'plm'), products={}, mes=mesh.settings, rework_mes=dict(mesh.settings, login='rework', password='rework'), uoms={'Nos': 2},
                   lab=dict(analyst=account('lab'), approver=account('qa'), analyst_user='lab',
                            parameter='pH', procedure='QC-ADJUST-1', references={batch: reference['name']}))
        if registered_block:
            legacy_mes = MesSite('mes-legacy').settings
            cfg.update(qms_source='qms-legacy', erp_source='erp-a', mes=legacy_mes,
                       rework_mes=dict(legacy_mes, login='rework', password='rework'))
        save(run/'env/company-sources.json', cfg)
        authority.settings = lambda: cfg
        if registered_block:
            authority.route = Authority.route.__get__(authority)
            authority.release_transfer_source = Authority.release_transfer_source.__get__(authority)
            primary = QualitySite(authority, None, revoke)
            other = QualitySite(FixtureAuthority(run), None, False, deny_qa=registered_block == 'qa')
            sites = {'qms-a.example': primary, 'qms-b.example': other,
                     'qms.example': QualitySite(FixtureAuthority(run), None, False)}
            self.register_sites(company, mesh, other_mes, blocked=registered_block)
            self.enterContext(patch('integrations.qms.adapter.build_opener',
                return_value=SimpleNamespace(open=lambda request, **kwargs:
                    sites[urlsplit(request.full_url).hostname].open(request, **kwargs))))
        else:
            authority.route = lambda *args, **kwargs: (cfg, None)
            self.enterContext(patch('integrations.qms.adapter.build_opener', return_value=primary))
        if registered_block:
            for site, item in ((primary, 'FG'), (other, 'OTHER')):
                site.docs['Quality Inspection Parameter', 'pH'] = dict(name='pH')
                site.docs['Quality Inspection Parameter', 'Viscosity'] = dict(name='Viscosity')
                site.docs['Quality Procedure', 'QC-ADJUST-1'] = dict(name='QC-ADJUST-1')
                site.docs['Quality Procedure', 'Investigate'] = dict(name='Investigate')
                site.docs['Item', item] = dict(name=item, stock_uom='Nos', has_batch_no=1)
        company.advance_clock(MIDNIGHT + timedelta(minutes=480))
        company.run_additions(480)
        if not revoke:
            company.erp.lost.add('Material Transfer')
        company.inspections()
        if change_completion:
            mesh.docs[MO, 1]['date_finished'] = '2027-01-01 23:59:59'
        company.run_additions(735 if registered_block else 705)
        if registered_block:
            self.assertTrue(any(kind == INSPECTION for kind, _ in primary.docs), flow.state["pending"])
            failure_a, = [doc for (kind, _), doc in primary.docs.items()
                          if kind == INSPECTION and doc['batch_no'] == batch and not doc.get('custom_retest_of')]
            failure_b, = [doc for (kind, _), doc in other.docs.items() if kind == INSPECTION]
            self.assertEqual((failure_a['name'], failure_b['name']), ('Quality Inspection-1',) * 2)
            self.assertEqual(failure_b['status'], 'Rejected')
            self.assertEqual(other_mes.rework_calls, [])
            self.assertEqual(company.erp.balance(other_batch, 'OTHER', 'Hold'), 12)
            self.assertEqual(company.erp.balance(other_batch, 'OTHER', 'FG'), 0)
            missing_authority = {'bridge': 'bridge authority', 'qa': 'commissioned lab identity'}
            if registered_block in missing_authority:
                self.assertEqual(failure_b['workflow_state'], 'Recorded')
                self.assertIn(missing_authority[registered_block],
                              flow.state['pending']['operating-review:' + other_batch]['last_error'])
            else:
                self.assertEqual(failure_b['workflow_state'], 'Lot Rejected')
                self.assertFalse(failure_b.get('custom_rework_salvage_quantity'))
        if change_completion:
            self.assertIsNone(mesh.execution)
            self.assertEqual(company.erp.balance(batch, 'FG', 'Hold'), 12)
            self.assertTrue(any('completion version' in str(row) for row in
                                flow.state['pending'].values()), flow.state['pending'])
            return
        self.assertTrue(flow.state.get('rework'), flow.state.get('pending'))
        operation, = flow.state['rework'].values()
        output = operation['output_batch']
        failure = primary.docs[INSPECTION, operation['authority']['inspection']]
        retest = primary.docs[INSPECTION, operation['retest']]
        self.assertEqual(mesh.rework_calls, [3])
        self.assertEqual(retest['docstatus'], 0)
        self.assertFalse(retest.get('readings'))
        self.assertIsNone(flow.disposition(flow.state['lots'][output], 'QA'))
        self.assertEqual(company.erp.balance(output, 'FG', 'Hold'), 11)
        self.assertFalse(flow.state['moves'])
        company.run_additions(1438)
        self.assertNotEqual(output, batch)
        self.assertEqual((failure['status'], failure['workflow_state']), ('Rejected', 'Rework Authorized'))
        self.assertEqual((retest['status'], retest['workflow_state']), ('Accepted', 'Revoked' if revoke else 'Released'))
        self.assertEqual(retest['batch_no'], batch)
        self.assertEqual(retest['custom_rework_output_batch'], output)
        self.assertGreater(float(failure['readings'][0]['reading_1']), 6)
        self.assertLessEqual(float(retest['readings'][0]['reading_1']), 6)
        self.assertEqual(operation['scrapped'], 1)
        self.assertEqual(mesh.docs[LINE, 5]['quantity'], 1)
        self.assertEqual(mesh.docs[LINE, 5]['lot_id'][0], 1)
        self.assertEqual(mesh.docs[LINE, 4]['quantity'], 11)
        repack = company.erp.get('Stock Entry', operation['receipt'])
        self.assertEqual((repack['docstatus'], repack['stock_entry_type']), (1, 'Repack'))
        self.assertEqual([(row['batch_no'], row['qty']) for row in repack['items']], [(batch, 12), (output, 11)])
        self.assertNotEqual(failure['name'], retest['name'])
        self.assertEqual(retest['custom_retest_of'], failure['name'])
        self.assertEqual(company.erp.balance(batch, 'FG', 'Hold'), 0)
        self.assertEqual(company.erp.balance(batch, 'FG', 'FG'), 0)
        move, = flow.state['moves'].values()
        self.assertFalse(move.get('done'))
        receipt, = authority.release_leases.values()
        self.assertEqual(receipt['lease_state'], 'denied' if revoke else 'armed')
        pending = flow.state['pending']['sync:' + output]['last_error']
        self.assertIn('denied' if revoke else 'committed before reply was lost', pending)
        self.assertEqual(company.erp.balance(output, 'FG', 'Hold'), 11 if revoke else 0)

        # Keep native fixture records alive while reloading the controller checkpoint.
        company.reload()
        company.company_erp.inspections()
        moves = company.company_erp.state['moves'].values()
        submitted = [doc for doc in company.erp.list('Stock Entry', {'stock_entry_type': 'Material Transfer'})
                     if doc['docstatus'] == 1]
        if revoke:
            self.assertFalse(submitted)
            self.assertFalse(any(m.get('done') for m in moves))
            self.assertEqual(company.erp.balance(output, 'FG', 'FG'), 0)
            draft, = company.erp.list('Stock Entry', {'stock_entry_type': 'Material Transfer'})
            self.assertEqual(draft['docstatus'], 0)
            self.archive(company, root, mesh, other_mes, primary, cfg, draft, operation, revoke=True)
            return
        transfer, = submitted
        recovered, = moves
        self.assertTrue(recovered['done'])
        self.assertEqual(company.erp.calls.count(('submit', 'Stock Entry', transfer['name'])), 1)
        self.assertEqual(company.erp.balance(output, 'FG', 'FG'), 11)
        if registered_block:
            self.assertEqual(recovered['disposition']['qms_source'], 'qms-a')
            self.assertEqual(transfer['items'][0]['batch_no'], output)
            self.assertEqual(transfer['items'][0]['qty'], 11)
            self.assertEqual(receipt['lease_state'], 'submitted')
            self.assertFalse(other.authority.release_leases)
            self.assertEqual(company.erp.balance(other_batch, 'OTHER', 'Hold'), 12)
        else:
            self.archive(company, root, mesh, other_mes, primary, cfg, transfer, operation)

    def register_sites(self, company, *sites, blocked):
        profiles = {}
        env = dict(READER='reader', SECRET='fixture', ANALYST='lab', APPROVER='qa',
                   REFERENCE_WRITER='reference', DISPATCHER='dispatcher',
                   FIXTURE_MES='fixture', OPERATOR_MES='operator', REWORK_MES='rework')
        run = company.run
        for suffix, site in zip(('a', 'b'), sites, strict=True):
            source = site.settings['source_id']
            item = site.docs['product.product', 5]['default_code']
            cfg = host_profile(source)
            assay = dict(method='method-1', parameter='pH', min=5, max=6)
            cfg.update(item=item, specifications={item: assay['method']},
                       mapping={item: dict(erp=dict(output=dict(item=item, unit='Nos')),
                                          mes=dict(output=dict(item=5, unit=2)))},
                       erp=dict(base=company.erp.base, site=company.erp.site, token_env='READER'),
                       qms_source='qms-' + suffix, qms_approver='qa',
                       mes={**{key: value for key, value in site.settings.items() if key != 'password'},
                            'password_env': 'FIXTURE_MES'},
                       mes_operator=dict(login='operator', password_env='OPERATOR_MES'),
                       physical_rework=dict(login='rework', password_env='REWORK_MES'))
            cfg['qms_reader']['url'] = f'https://qms-{suffix}.example'
            cfg['qms_dispatcher'] = dict(cfg['qms_reader'], api_key_env='DISPATCHER')
            cfg['recurring_lab'].update(analyst_user='lab', reference_user='reference',
                                        procedure='QC-ADJUST-1', assays={item: assay})
            if suffix == 'b':
                if blocked == 'assay':
                    assay['parameter'] = 'Viscosity'
                elif blocked == 'procedure':
                    cfg['recurring_lab']['procedure'] = 'Investigate'
                elif blocked == 'uncommissioned':
                    del cfg['physical_rework']
            profiles[source] = cfg
        registry = run/'registry.json'
        save(registry, profiles)
        save(run/'env/endpoints.json', dict(erp=company.erp.base, site=company.erp.site))
        self.enterContext(patch.dict('os.environ', dict(env, NORVALE_CONNECTIONS_FILE=str(registry))))
        company.run_additions(480)
        company.additions.extend('registered-salvage', systems={
            'production-' + cfg['item']: catalog_spec(source, cfg) for source, cfg in profiles.items()})

    def archive(self, company, root, mesh, other_mes, primary, cfg, transfer, operation, *, revoke=False):
        archive = root/'lake'
        settings = root/'analytics.json'
        erp_entities = ['Stock Entry', 'Serial and Batch Bundle', 'Stock Ledger Entry']
        qms_entities = [INSPECTION, 'Non Conformance']
        other_qms = deepcopy(primary.docs)
        # Exact native IDs and versions from another site, with incompatible evidence.
        other_qms[INSPECTION, operation['retest']]['custom_rework_execution'] = 'mes-b:mrp.production:2'
        for name, receipt in company.authority.release_leases.items():
            other_qms['Norvale Dispatch Lease', name] = dict(receipt, lease_state='denied')
        other_erp = deepcopy(company.erp.docs)
        other_erp['Stock Entry', transfer['name']]['docstatus'] = 2
        other_mes.docs = deepcopy(mesh.docs)
        other_mes.docs[MO, 2]['norvale_rework_authority'] = 'wrong-site-authority'

        tables = {'https://erp.example': company.erp.docs, 'https://erp-other.example': other_erp,
                  'https://qms-other.example': other_qms}

        def response(reader, path):
            url = urlsplit(reader.url + path)
            if reader.url == 'https://qms.example':
                return primary.reply('reader', 'GET', url, None)
            return resource(tables[reader.url], url)

        def reader(site):
            return dict(url=f'https://{site}.example', api_key='reader', api_secret='fixture')

        # The extra sources go through their normal connectors in the same scheduled cycle.
        save(settings, dict(erp=dict(source='erp-a', reader=reader('erp'), entities=erp_entities),
                            qms=dict(source='qms-a', reader=reader('qms'), entities=qms_entities),
                            mes=dict(reader=mesh.settings),
                            plm=dict(source='plm-a', reader=cfg['plm'], parts=[])))
        save(company.run/'env/planning-report.json', dict(settings=str(settings), archive=str(archive)))

        from integrations.company_run import lake
        scan = lake.scan

        def scan_with_collisions(kind, config, destination):
            scan(kind, config, destination)
            if kind == 'erp':
                scan(kind, dict(config, source='erp-b', reader=reader('erp-other')), destination)
            if kind == 'qms':
                scan(kind, dict(config, source='qms-b', reader=reader('qms-other')), destination)
            if kind == 'mes':
                scan(kind, {'reader': other_mes.settings}, destination)

        with patch.object(Frappe, 'response', response), patch(
                'integrations.plm.PLM'), patch('integrations.company_run.publications.collect'), patch.object(
                lake, 'scan', side_effect=scan_with_collisions):
            company.run_phase(0, 'd0000:native-clock', lambda: company.run_additions(1439))
        self.assertEqual(company.state['lake_scans']['errors'], [])
        self.assertEqual(company.state['lake_scans']['last_success_minute'],
                         dict.fromkeys(('erp-a', 'qms-a', 'mes-a', 'plm-a'), 1439))

        result = subprocess.run([sys.executable, '-c', '''
import json, sys
from integrations.lake.adapter import Lake
from integrations.lake.genealogy import ShipmentHistory
from integrations.lake.rework_archive import reconcile
rows = Lake(sys.argv[1], {})._rows()
history = ShipmentHistory(rows, 'erp-a')
transfer_id, retest_id, output_batch, expected = sys.argv[2:]
transfer = history.transfer(transfer_id)
assert bool(transfer) == (expected == 'released'), history.transfer_lineage(transfer_id)
for source, entity, native in [('erp-b', 'Stock Entry', transfer_id),
        ('qms-b', 'Quality Inspection', retest_id), ('mes-b', 'mrp.production', '2')]:
    assert (source, entity, native) in history.latest
    narrowed = [row for row in rows if (row['source'], row['entity'], row['native_id']) !=
                (source.replace('-b', '-a'), entity, native)]
    assert ShipmentHistory(narrowed, 'erp-a').transfer(transfer_id) is None, (source, entity, native)
rework = next(row for row in reconcile(rows, 'erp-a') if row.get('batch') == output_batch)
print(json.dumps([transfer, rework]))
''', str(archive/'delta'), transfer['name'], operation['retest'], operation['output_batch'],
            'pending' if revoke else 'released'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lineage, rework = json.loads(result.stdout)
        self.assertEqual(rework['unresolved'], [])
        if revoke:
            self.assertIsNone(lineage)
            self.assertEqual(rework['status'], 'pending')
            return
        self.assertEqual(lineage['batch'], operation['output_batch'])
        self.assertEqual((lineage['source'], lineage['target'], lineage['qty']), ('Hold', 'FG', 11))
        self.assertEqual(set(lineage['legs']), {'Outward', 'Inward'})
        self.assertEqual(lineage['release'][:3], ['qms-a', INSPECTION, operation['retest']])
        self.assertEqual(lineage['authorization_receipt'][:2], ['qms-a', 'Norvale Dispatch Lease'])
        self.assertEqual(rework['status'], 'released')
        self.assertEqual(rework['rework']['mes'][:3], ['mes-a', 'mrp.production', '2'])
        self.assertEqual(rework['rework']['original_mes'][:3], ['mes-a', 'mrp.production', '1'])
        self.assertEqual(rework['rework']['scrapped'], 1)
