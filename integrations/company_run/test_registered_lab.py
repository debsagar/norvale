"""Recurring clock and QMS REST contract with independent, colliding native IDs."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from engine import DAY
from integrations.lake.adapter import Lake
from integrations.lake.boundary import Frappe, crosslinks, trace
from integrations.qms.sample_reference import ENTITY as SAMPLE_ENTITY, decode_registered, registered_note
from integrations.qms.adapter import ACTION, APIError, CASE, INSPECTION, OP, QMS, STATES
from native.company import DEFAULT_CONFIG, Engine, save
from native.company_erp import CompanyERP, Pending
from native.production_profiles import ADAPTER, LAB_MODEL, profile
from native.recurring_extensions import open_catalog
from .authority import Authority
from .operating import Operating
from .quality import finished_specification


def host_profile(site, *, opt_in=True):
    item = site.upper()
    cfg = dict(adapter=ADAPTER, item=item, family=12, mapping={item: {'erp': {
        'components': {'component': {'item': 'RAW-' + item}}}}},
        source=site + '-plm', hold_location_id=1, customer='Customer', order_roles=['sales'],
        plm=dict(url=f'http://{site}.plm.test', publisher='plm', token_env='READER'),
        erp=dict(base='https://erp.test', site='erp', token_env='READER'),
        mes=dict(url=f'http://{site}.mes.test', db='mes', source_id=site, login='mes', password_env='READER'),
        qms_reader=dict(url=f'https://{site}.qms.test', site=f'{site}.qms',
                        api_key_env='READER', api_secret_env='SECRET'),
        qms_approver='approver', specifications={item: 'method-1', 'RAW-' + item: 'incoming-1'},
        units={'Units': 'Nos'})
    if opt_in:
        auth = {role: {'api_key_env': role.upper(), 'api_secret_env': 'SECRET'}
                for role in ('analyst', 'approver', 'reference_writer')}
        bounds = (5, 7) if site == 'east' else (2, 3)
        cfg['recurring_lab'] = dict(**auth, analyst_user='analyst', reference_user='reference_writer',
            company='Lab Corp', warehouse='Quarantine', procedure='PROCEDURE', model=LAB_MODEL,
            assays={item: dict(method='method-1', parameter='INDEX', min=bounds[0], max=bounds[1]),
                    'RAW-' + item: dict(method='incoming-1', parameter='INDEX', min=5, max=6)})
    return cfg


def catalog_spec(site, cfg):
    return dict(adapter=ADAPTER, connection=site,
                target_hash=sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest())


class NativeLab:
    def __init__(self, item):
        self.docs = {('Quality Inspection Parameter', 'INDEX'): {'name': 'INDEX'},
                     ('Quality Procedure', 'PROCEDURE'): {'name': 'PROCEDURE'}}
        for code in (item, 'RAW-' + item):
            self.docs['Item', code] = dict(name=code, stock_uom='Nos', has_batch_no=1)
        self.fail_reference = self.crash_release = self.deny_qa = False
        self.transitions = []
        self.calls = []

    def respond(self, user, method, path, fields, event):
        if path == 'method/frappe.auth.get_logged_user':
            return {'message': 'wrong-user' if self.deny_qa and user == 'approver' else user}
        if path == 'method/frappe.model.workflow.apply_workflow':
            action = fields['action']
            assert user == ('analyst' if action == 'Record' else 'approver')
            self.transitions.append((action, event['role'], event['at']))
            assert event['role'] == ('LAB' if action == 'Record' else 'QA')
            doc = self.docs[INSPECTION, fields['doc']['name']]
            row, = doc['readings']
            outcome = 'Accepted' if row['min_value'] <= float(row['reading_1']) <= row['max_value'] else 'Rejected'
            row['status'] = outcome
            doc.update(docstatus=1, workflow_state=STATES[action], status=outcome,
                       modified_by=user, modified=str(len(self.transitions)))
            if self.crash_release and action == 'Release Lot':
                self.crash_release = False
                raise RuntimeError('controller stopped after release')
            return {'message': deepcopy(doc)}
        endpoint = urlsplit(path)
        parts = [unquote(part) for part in endpoint.path.split('/')]
        _, doctype, *name = parts
        if name:
            try:
                return {'data': deepcopy(self.docs[doctype, name[0]])}
            except KeyError:
                raise APIError(404, 'DoesNotExist') from None
        args = parse_qs(endpoint.query)
        if method == 'GET':
            filters = json.loads(args['filters'][0])
            rows = [deepcopy(doc) for (kind, _), doc in self.docs.items()
                    if kind == doctype and all(doc.get(k) == v for k, v in filters.items())]
            if args['order_by'][0].startswith('creation'):
                rows.sort(key=lambda row: row['creation'], reverse=True)
            return {'data': rows[:int(args['limit_page_length'][0])]}
        assert method == 'POST' and event['role'] == 'LAB'
        assert user == ('reference_writer' if doctype in ('Batch', 'Stock Entry') else 'analyst')
        count = 1 + sum(kind == doctype for kind, _ in self.docs)
        name = fields.get('batch_id', f'{doctype}-{count}')
        self.calls.append((doctype, name))
        doc = self.docs[doctype, name] = dict(deepcopy(fields), name=name, docstatus=0, creation=count, owner=user)
        for row in doc.get('items', []):
            row.update(name=name + '-row', stock_uom='Nos')
        if self.fail_reference and doctype == 'Stock Entry':
            self.fail_reference = False
            raise TimeoutError('reference committed before reply')
        return {'data': deepcopy(doc)}


class RegisteredLabTest(TestCase):
    def test_normal_inspections_run_on_shifts_and_reconcile_two_sites_after_restart(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            profiles = {site: host_profile(site, opt_in=site != 'manual')
                        for site in ('east', 'west', 'manual')}
            config = dict(DEFAULT_CONFIG, company_operating=True, company_timing=dict(
                shifts={'LAB': [[600, 630], [660, 700]], 'QA': [[750, 780]]},
                staff={'LAB': 1, 'QA': 1}, task_minutes={'sample': 10, 'result': 10, 'review': 10},
                lab_minutes=25))
            save(run/'env/company-sources.json', dict(erp_source='erp-source', mes={'source_id': 'primary'}, products={}))
            save(run/'env/endpoints.json', {'erp': 'https://erp.test', 'site': 'erp'})
            for cfg in profiles.values():
                product = cfg['item']
                cfg['mapping'][product]['erp']['output'] = {'item': product}
                cfg['mapping'][product]['mes'] = dict(output={'item': product}, settings={'company_id': 1})
            save(run/'registry.json', profiles)
            company_sources = run/'env/company-sources.json'
            save(company_sources, dict(json.loads(company_sources.read_text()), plm_source='primary-plm',
                                      plm={'url': 'https://primary.plm.test'}))
            archive = run/'lake'
            settings = run/'env/analytics.json'
            def reader(host, site):
                return dict(url=f'https://{host}.test', site=site, api_key='reader', api_secret='secret')
            save(settings, dict(
                erp=dict(reader=reader('erp', 'erp'), source='erp-source', entities=['Purchase Receipt', 'Stock Entry']),
                qms=dict(reader=reader('primary.qms', 'primary.qms'), source='primary-qms',
                         entities=[INSPECTION]),
                plm=dict(reader=reader('primary.plm', 'plm'), source='primary-plm', parts=[])))
            save(run/'env/planning-report.json', dict(settings=str(settings), archive=str(archive)))
            logs = []
            erp = {}
            engine = None
            labs = {site: NativeLab(site.upper()) for site in profiles}
            east, west = labs['east'], labs['west']
            east.fail_reference = east.crash_release = west.deny_qa = True
            parameter = west.docs.pop(('Quality Inspection Parameter', 'INDEX'))

            class Client(QMS):
                def __init__(self, url, api_key, **kwargs):
                    self.lab = labs[urlsplit(url).hostname.split('.')[0]]
                    self.user = api_key

                def request(self, method, path, data=None):
                    return self.lab.respond(self.user, method, path, data, engine.additions.event)

            unavailable = set()
            def observe(client, path):
                hostname = urlsplit(client.url).hostname
                site = hostname.split('.')[0]
                if site in unavailable:
                    raise TimeoutError('QMS offline')
                docs = erp if hostname == 'erp.test' else labs[site].docs if site in labs else {}
                parts = [unquote(part) for part in urlsplit(path).path.split('/')]
                entity = parts[3]
                if len(parts) == 5:
                    return {'data': deepcopy(docs[entity, parts[4]])}
                return {'data': [{'name': name} for kind, name in sorted(docs) if kind == entity]}

            def controller(state):
                company = Engine.__new__(Engine)
                company.run, company.config, company.day = run, config, state['day']
                company.state, company.docs = state, state['docs']
                company.wh = {'hold': 'Hold', 'rm': 'RM', 'fg': 'FG'}
                company.advance_clock = lambda _: None
                company.run_additions = lambda _: None
                company.log = lambda event, **details: logs.append((event, details))
                client = SimpleNamespace(base='https://erp.test', site='erp', get=lambda dt, name: erp[dt, name])
                company.api = dict(BUYER=client, WAREHOUSE=client)
                company.company_erp = CompanyERP(company, Authority(run))
                # Run the native disposition and sync planner. Stock ledger mutations
                # are outside this QMS qualification.
                company.company_erp.balance = lambda *args: 0
                company.additions = open_catalog(run, config, state, company=company)
                return company

            env = {key.upper(): key for key in ('reader', 'secret', 'analyst', 'approver', 'reference_writer')}
            env['NORVALE_CONNECTIONS_FILE'] = str(run/'registry.json')
            with (patch.dict('os.environ', env),
                  patch('integrations.company_run.operating.QMS', Client),
                  patch('integrations.company_run.authority.QMS', Client),
                  patch.object(Frappe, 'response', observe),
                  patch('integrations.company_run.publications.collect'),
                  patch('integrations.plm.PLM'),
                  patch('integrations.mes.client.Mes'),
                  patch('integrations.lake.mes_reader.scan'),
                  patch('integrations.lake.mes_reader.scan_labor'),
                  patch('integrations.lake.mes_reader.scan_staging'),
                  patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Unexpected I/O'))):
                state = dict(day=0, docs={}, ops={'orders': {}, 'purchases': {}})
                engine = controller(state)
                flow = engine.company_erp
                engine.run_phase(0, 'd0000:management', lambda: None)
                self.assertIn('primary-qms', engine.state['lake_scans']['last_success_minute'])
                engine.additions.extend('families', systems={
                    'production-' + cfg['item']: catalog_spec(site, cfg) for site, cfg in profiles.items()})
                for site, cfg in profiles.items():
                    item = cfg['item']
                    batch = 'batch-' + site
                    raw = 'raw-' + site
                    stock_entry = 'stock-' + site
                    state['ops']['orders'][site] = dict(item=item, production_profile='production-' + item)
                    state['docs']['incoming-' + site] = {'doctype': 'Purchase Receipt', 'name': 'receipt-' + site}
                    revision = dict(item=item, mes_source=site, plm_source=site + '-plm', id='12',
                        version='version', inspection_spec=finished_specification(
                            item, 'method-1', cfg['source'], dict(native_id=12, source_version='version')))
                    output = dict(lot_id=f'{site}:stock.lot:1', item=item, qty=10, uom='Nos')
                    flow.register(raw, 'RAW-' + item, 10, 'Nos', 'incoming',
                                  {'doctype': 'Purchase Receipt', 'name': 'receipt-' + site}, source_row='receipt-row')
                    flow.register(batch, item, 10, 'Nos', 'finished', {'doctype': 'Stock Entry', 'name': stock_entry},
                                  source_row='row-1', completion_id='1', completion_version='time-1',
                                  mes_lot_id=output['lot_id'], work_order='work-' + site, revision=revision)
                    for batch_id in (raw, batch):
                        lot = flow.state['lots'][batch_id]
                        dt, name = lot['source_type'], lot['source_id']
                        erp[dt, name] = dict(doctype=dt, name=name, company=Engine.company, docstatus=1,
                            items=[dict(name=lot['source_row'], batch_no=batch_id, item_code=lot['item'],
                                        qty=10, stock_uom='Nos', warehouse='Hold')])
                    report = dict(id='1', source=site, version='time-1', revision=revision, authorized=True,
                        work_order='work-' + site, output=output, inputs=[dict(batch=raw, qty=10, uom='Nos')])
                    # Unit/native completion IDs are reused at every deployment.
                    flow.state['completions'][site] = dict(done=True, report=report, work='native-work-1',
                                                          target_id=stock_entry)
                    flow.state['work'][site] = dict(request={'work_order': 'work-' + site}, order=site, qty=10, produced=10)
                manual = flow.state['lots']['batch-manual']
                labs['manual'].docs[INSPECTION, 'Quality Inspection-1'] = dict(
                    name='Quality Inspection-1', creation=1, modified='1', modified_by='approver',
                    docstatus=1, status='Accepted', workflow_state='Released', batch_no=manual['batch'],
                    item_code=manual['item'], custom_spec_revision=manual['revision']['inspection_spec'],
                    custom_lot_quantity=manual['qty'], custom_lot_uom=manual['uom'], report_date='2027-01-01')
                # Exercise the recurring controller discovery, normal lab handoffs,
                # and a new controller instance on the same durable queue.
                engine.inspections()
                with self.assertRaisesRegex(RuntimeError, 'stopped after release'):
                    engine.additions.run(until=780)
                self.assertIn('operating-sample:batch-west', flow.state['pending'])
                self.assertEqual(east.transitions[0], ('Record', 'LAB', 670))
                self.assertEqual(east.transitions[1], ('Record', 'LAB', 680))
                self.assertEqual(east.transitions[2], ('Release Lot', 'QA', 750))
                engine = controller(json.loads((run/'state.json').read_text()))
                west.docs['Quality Inspection Parameter', 'INDEX'] = parameter
                engine.additions.run(until=DAY + 780)
                self.assertIn('operating-review:batch-west', engine.company_erp.state['pending'])
                self.assertIn('operating-review:raw-west', engine.company_erp.state['pending'])
                self.assertEqual([action for action, *_ in west.transitions], ['Record', 'Record'])
                self.assertEqual(sum(action == 'Release Lot' for action, *_ in east.transitions), 2)
                self.assertIsNone(engine.company_erp.state['synced']['batch-west'])

                actor = Operating(engine)
                with patch('integrations.company_run.demo.Demo.eligible_samples',
                           return_value={'raw-manual': flow.state['lots']['raw-manual']}):
                    self.assertNotIn('raw-manual', actor.eligible_samples())
                # An already queued inbound sample may now have a manual-only owner.
                with patch.object(actor, 'primary_sample', side_effect=AssertionError('wrong QMS site')):
                    with self.assertRaisesRegex(Pending, 'manual QMS'):
                        actor.sample('raw-manual')
                sample = actor.sample('batch-east')
                wrong = dict(inspection='Quality Inspection-1', sample=sample)
                west.deny_qa = False
                with self.assertRaisesRegex(Pending, 'different lab or lot'):
                    actor.review('batch-west', wrong)
                engine.additions.run(until=2 * DAY + 780)
                finished_east, = [doc for (kind, _), doc in east.docs.items()
                                  if kind == INSPECTION and doc['batch_no'] == 'batch-east']
                finished_west, = [doc for (kind, _), doc in west.docs.items()
                                  if kind == INSPECTION and doc['batch_no'] == 'batch-west']
                self.assertIn((INSPECTION, finished_west['name']), east.docs)
                self.assertEqual((finished_east['workflow_state'], finished_west['workflow_state']),
                                 ('Released', 'Lot Rejected'))
                self.assertNotEqual(finished_east[OP], finished_west[OP])
                self.assertEqual(finished_west['readings'][0]['max_value'], 3)
                self.assertEqual(finished_east['report_date'], '2027-01-01')
                self.assertEqual(finished_west['report_date'], '2027-01-02')
                synced = engine.company_erp.state['synced']
                self.assertEqual({synced[key]['id'] for key in ('batch-east', 'raw-east', 'raw-west')},
                                 {'Quality Inspection-1', 'Quality Inspection-2'})
                self.assertNotEqual(synced['raw-east']['qms_source'], synced['raw-west']['qms_source'])
                rejection = engine.state['operating_lot_rejections']['batch-west']
                self.assertEqual(rejection, {'source': synced['raw-west']['qms_source'], 'id': finished_west['name']})
                self.assertFalse(labs['manual'].calls)
                self.assertEqual(synced['batch-manual']['id'], 'Quality Inspection-1')
                self.assertNotEqual(synced['batch-manual']['qms_source'], synced['batch-east']['qms_source'])
                self.assertEqual(sum(key[0] == 'Stock Entry' for key in east.docs), 2)
                self.assertEqual(sum(key[0] == CASE for key in west.docs), 1)
                self.assertEqual(sum(key[0] == ACTION for key in west.docs), 1)
                self.assertFalse(any(action == 'Authorize Rework' for action, *_ in west.transitions))
                with self.assertRaisesRegex(Pending, 'no verified physical rework authority'):
                    actor.rework_request('batch-west', finished_west['name'])
                east_ref = east.docs['Stock Entry', finished_east['reference_name']]
                original_note = east_ref['remarks']
                east_ref['remarks'] = actor.registered_lab(flow.state['lots']['batch-east']).note
                replayed = actor.sample('batch-east')
                self.assertEqual((replayed['scope'], replayed['reference']), (sample['scope'], sample['reference']))
                east_ref['remarks'] = original_note

                east_source = synced['batch-east']['qms_source']
                west_source = synced['raw-west']['qms_source']
                self.assertEqual(finished_east['name'], finished_west['name'])
                self.assertEqual(finished_east['reference_name'], finished_west['reference_name'])
                west_ref = west.docs['Stock Entry', finished_west['reference_name']]
                scope = decode_registered(west_ref)
                self.assertEqual(scope['source_row'], 'row-1')
                self.assertEqual(scope['writer'], 'reference_writer')
                self.assertEqual((scope['erp_source'], scope['erp_site'], scope['qms_source'], scope['qms_site']),
                                 ('erp-source', 'erp', west_source, 'west.qms'))
                # A held inbound lot can be traced without a disposition, transfer or delivery.
                raw_west, = [doc for (kind, _), doc in west.docs.items()
                             if kind == INSPECTION and doc['batch_no'] == 'raw-west']
                legacy = dict(finished_west, name='legacy-inspection', reference_name='legacy-ref',
                              child_row_reference='legacy-row')
                west.docs[INSPECTION, legacy['name']] = legacy
                west.docs['Stock Entry', 'legacy-ref'] = dict(
                    deepcopy(west_ref), name='legacy-ref',
                    remarks='ERP Stock Entry stock-west row row-1 / ' + finished_west[OP].replace(':initial', ':reference'))
                west.docs['Stock Entry', 'legacy-ref']['items'][0]['name'] = 'legacy-row'

                # Native rework keeps the failed receipt as the retest's sample reference.
                salvage = finished_west['custom_lot_quantity'] / 2
                finished_west.update(workflow_state='Rework Authorized', custom_rework_input_batch='batch-west',
                                     custom_rework_salvage_quantity=salvage)
                retest = dict(finished_west, name='000-retest', status='Accepted', workflow_state='Released',
                              custom_retest_of=finished_west['name'], custom_rework_execution='west:mrp.production:8',
                              custom_rework_output_batch='salvage-west', custom_lot_quantity=salvage)
                retest[OP] = 'rework:west'
                west.docs[INSPECTION, retest['name']] = retest

                unavailable.add('west')
                engine.run_phase(2, 'd0002:native-clock', lambda: None)
                cutoff = 3 * DAY - 1
                self.assertNotIn(east_source, dict(engine.state['lake_scans']['errors']), engine.state['lake_scans'])
                self.assertEqual(engine.state['lake_scans']['last_success_minute'][east_source], cutoff)
                self.assertNotIn(west_source, engine.state['lake_scans']['last_success_minute'])
                self.assertIn((west_source, 'ExceptionGroup'), engine.state['lake_scans']['errors'])
                lake = Lake(archive/'delta', {})
                before = lake._rows()
                self.assertFalse(any(row['source'] == west_source for row in before))

                unavailable.clear()
                engine = controller(json.loads((run/'state.json').read_text()))
                engine.run_phase(2, 'd0002:native-clock', lambda: None)
                self.assertEqual(engine.state['lake_scans']['errors'], [])
                self.assertEqual(engine.state['lake_scans']['last_success_minute'][west_source], cutoff)
                after = lake._rows()
                self.assertCountEqual([row for row in after if row['source'] != west_source], before)
                history = {}
                for row in after:
                    history.setdefault(row['source'], {}).setdefault(row['entity'], []).append(json.loads(row['payload']))
                # Another ERP deployment may reuse the exact native receipt and row IDs.
                history['unrelated-erp'] = history['erp-source']
                self.assertIn((INSPECTION, retest['name'], INSPECTION, finished_west['name']),
                              trace(history[west_source]))
                edges = crosslinks(history)['edges']
                for source, inspection, origin in (
                    (east_source, finished_east, ('Stock Entry', 'stock-east')),
                    (west_source, finished_west, ('Stock Entry', 'stock-west')),
                    (west_source, raw_west, ('Purchase Receipt', 'receipt-west')),
                ):
                    sample = (source, SAMPLE_ENTITY, inspection['reference_name'])
                    self.assertIn(((source, INSPECTION, inspection['name']), 'sample_reference', sample), edges)
                    self.assertIn((sample, 'sample_erp_row', ('erp-source', *origin)), edges)
                self.assertFalse(any(target[0] == 'unrelated-erp' for _, _, target in edges))
                self.assertNotIn('legacy-ref', {doc['name'] for doc in history[west_source][SAMPLE_ENTITY]})
                engine.run_phase(2, 'd0002:native-clock', lambda: None)
                self.assertCountEqual(lake._rows(), after)

                # A valid-looking note cannot substitute another native employee or source.
                for field, value in (('writer', 'other-user'), ('erp_source', 'unrelated-erp'),
                                     ('erp_site', 'other-site'), ('qms_site', 'east.qms')):
                    with self.subTest(field=field):
                        west_ref['remarks'] = registered_note(dict(scope, **{field: value}))
                        west_ref['owner'] = value if field == 'writer' else scope['writer']
                        engine.run_phase(3, 'd0003:native-clock', lambda: None)
                        self.assertIn((west_source, 'ExceptionGroup'), engine.state['lake_scans']['errors'])
                        self.assertEqual(engine.state['lake_scans']['last_success_minute'][west_source], cutoff)
                        self.assertCountEqual(lake._rows(), after)
                west_ref.update(remarks=registered_note(scope), owner=scope['writer'])
                for record, changes in ((retest, {'owner': 'other-analyst'}),
                                        (retest, {'custom_lot_quantity': salvage + 1}),
                                        (finished_west, {OP: 'other-sample'}),
                                        (finished_west, {'workflow_state': 'Lot Rejected'})):
                    with self.subTest(changes=changes), patch.dict(record, changes):
                        engine.run_phase(3, 'd0003:native-clock', lambda: None)
                        self.assertIn((west_source, 'ExceptionGroup'), engine.state['lake_scans']['errors'])
                        self.assertEqual(engine.state['lake_scans']['last_success_minute'][west_source], cutoff)
                east.docs[INSPECTION, 'separate-review'] = dict(
                    finished_east, name='separate-review', creation=3, custom_qms_operation='manual-review')
                with self.assertRaisesRegex(Pending, 'cannot supersede'):
                    Operating(engine).sample('batch-east')
                profiles['east']['recurring_lab']['assays']['EAST']['max'] = 9
                save(run/'registry.json', profiles)
                with self.assertRaisesRegex(Pending, 'scope could not be verified'):
                    Operating(engine).sample('batch-east')

    def test_rejects_uncommissioned_lab_configuration(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            cfg = host_profile('east')
            save(run/'env/endpoints.json', {'erp': 'https://erp.test', 'site': 'erp'})
            env = {key.upper(): key for key in ('reader', 'secret', 'analyst', 'approver', 'reference_writer')}
            env['NORVALE_CONNECTIONS_FILE'] = str(run/'registry.json')
            with patch.dict('os.environ', env):
                cases = [('model', 'unmodeled', 'lab scope'),
                         ('analyst_user', 'approver', 'lab scope'),
                         ('assays', {'EAST': dict(method='wrong', parameter='INDEX', min=5, max=6)}, 'assay'),
                         ('assays', {'EAST': dict(method='method-1', parameter='INDEX', min=6, max=5)}, 'assay'),
                         ('analyst', dict(api_key_env='ABSENT', api_secret_env='SECRET'), 'credentials')]
                for field, value, reason in cases:
                    with self.subTest(field=field, value=value):
                        changed = dict(cfg, recurring_lab={**cfg['recurring_lab'], field: value})
                        save(run/'registry.json', {'east': changed})
                        with self.assertRaisesRegex(ValueError, reason):
                            profile(run, 'production-EAST', catalog_spec('east', changed))
