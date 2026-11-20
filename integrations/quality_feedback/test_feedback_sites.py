import ast
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import generate
from integrations.lake.boundary import crosslinks, project
from integrations.qms.adapter import ACTION, CASE, INSPECTION, QMS
from .test_returns import ERP, checkpoint_engine


# Exercise the actual QMS Server Script bodies with the in-memory QMS transport.
configure = Path(__file__).parents[1] / 'qms' / 'configure.py'
guards = {
    statement.targets[0].id: compile(ast.literal_eval(statement.value), str(configure), 'exec')
    for statement in ast.parse(configure.read_text()).body
    if isinstance(statement, ast.Assign) and isinstance(statement.targets[0], ast.Name)
    and statement.targets[0].id in ('INVESTIGATION_GUARD', 'EFFECTIVENESS_GUARD')
}


class Document(SimpleNamespace):
    def __getattr__(self, key):
        return None


class Site:
    def __init__(self, batch, shipped):
        self.records = {'Batch': {batch: {'item': 'Item'}}, CASE: {}, ACTION: {}, INSPECTION: {}}
        self.records[INSPECTION] = {
            name: dict(name=name, batch_no=batch, item_code='Item', docstatus=1, status='Accepted',
                       workflow_state=state, creation=creation, modified='v1', custom_lot_quantity=4, custom_lot_uom='Nos',
                       custom_rework_output_batch=shipped if shipped != batch else None)
            for name, state, creation in [('QI', 'Released', 1), ('NEXT', 'Released', 3), ('OTHER', 'Revoked', 3)]}

    def client(self, api_key, **_):
        client = QMS('http://unused', api_key, 'test')

        def request(method, resource, fields=None):
            from urllib.parse import unquote, parse_qs
            path, _, query = resource.partition('?')
            if path.startswith('method/'):
                assert api_key == 'approver'
                sample = self.records[INSPECTION][fields['doc']['name']]
                sample.update(workflow_state='Revoked', modified='v2')
                return {}
            _, entity, *identifier = unquote(path).split('/')
            records = self.records[entity]
            if method == 'GET':
                if identifier:
                    return {'data': deepcopy(records[identifier[0]])}
                filters = json.loads(parse_qs(query)['filters'][0])
                return {'data': [{'name': key} for key, doc in records.items()
                                 if all(doc.get(k) == v for k, v in filters.items())]}
            name = identifier[0] if identifier else {CASE: 'NC', ACTION: 'CAPA'}[entity]
            prior = records.get(name)
            doc = {**(prior or {}), **deepcopy(fields), 'name': name, 'creation': 2}
            if entity == ACTION and all(r['status'] == 'Completed' for r in doc['resolutions']):
                doc['status'] = 'Completed'
            else:
                doc.setdefault('status', 'Open')
            records[name] = self.validate(entity, doc, prior, api_key)
            return {'data': deepcopy(records[name])}

        client.request = request
        return client

    def validate(self, entity, doc, prior, user):
        native = Document(**doc)
        previous = Document(**prior) if prior else None
        native.get_doc_before_save = lambda: previous
        if entity == ACTION:
            native.resolutions = [Document(**row) for row in doc['resolutions']]
            if previous:
                previous.resolutions = [Document(**row) for row in prior['resolutions']]

        def value(doctype, name, fields, as_dict=False):
            record = self.records[doctype].get(name)
            if record:
                return Document(**{f: record.get(f) for f in fields}) if as_dict else record.get(fields)

        def reject(message):
            raise ValueError(message)

        frappe = Document(session=Document(user=user), throw=reject,
                          db=Document(exists=lambda *a: user == 'approver', get_value=value))
        exec(guards['INVESTIGATION_GUARD' if entity == CASE else 'EFFECTIVENESS_GUARD'],
             {'doc': native, 'frappe': frappe})
        if native.custom_withdrawal_approver:
            doc['custom_withdrawal_approver'] = native.custom_withdrawal_approver
        return doc


class FeedbackSitesTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'env').mkdir()
        self.erp = ERP()
        self.qms = {'main': Site('ORIGINAL', 'LOT'), 'east': Site('ORIGINAL', 'LOT')}
        self.sites = {site: self.config(site) for site in self.qms}
        self.cfg = dict(self.sites['main'], source='company', feedback_sites={'east': self.sites['east']})
        self.note = self.erp.notes['DN']
        self.pin = dict(id='QI', qty=4, version='v1', status='released', batch='LOT', qms_batch='ORIGINAL', qms_source='east', item='Item')
        self.batch, self.owner, self.registered = 'ORIGINAL', 'east', 'east'
        self.bind_pin()
        self.report = dict(operation='feedback', delivery='DN', line='ROW', batch='LOT', description='Leak')
        self.receipt = dict(complaint='NC', delivery='DN', line='ROW', batch='LOT',
                            quantity='1', observation='receipt', observed_on='2026-01-09')
        self.syncs = []

    @staticmethod
    def config(site):
        def account(role):
            return dict(url='http://' + site, api_key=role, api_secret='test')
        return dict(qms_source=site, qms_reader=account('reader'),
                    lab=dict(analyst=account('analyst'), approver=account('approver'), procedure='P'))

    def bind_pin(self, field='custom_norvale_quality_releases'):
        self.note.pop('custom_norvale_quality_releases', None)
        self.note[field] = json.dumps([dict(batch='LOT', qty=4, disposition=self.pin)])

    def make_engine(self, path):
        e = checkpoint_engine(path, self.erp)
        flow = e.company_erp
        prior = json.loads(self.note.get('custom_norvale_quality_releases') or self.note['remarks'])
        flow.state['shipments']['ship'].setdefault('rows', prior)
        lot = flow.state['lots']['LOT'] = dict(item='Item', revision={'mes_source': 'plant'})
        if self.batch != 'LOT':
            lot['rework_key'] = 'rework'
            flow.state.setdefault('rework', {})['rework'] = dict(
                output_batch='LOT', authority={'batch': self.batch}, retest='QI')
        def route(item, mes_source, *, pinned):
            self.assertEqual((item, mes_source, pinned), ('Item', 'plant', True))
            return self.sites[self.registered], {}
        flow.authority = SimpleNamespace(route=route)
        flow.sync = self.syncs.append
        write = e.write
        e.write = lambda key, entity, fields, *a, **k: (
            dict(name=key, **fields) if entity == 'ToDo' else write(key, entity, fields, *a, **k))
        return e

    def invoke(self, command, **fields):
        def client(url, **kwargs):
            return self.qms[url.removeprefix('http://')].client(**kwargs)
        (self.root / 'env/company-sources.json').write_text(json.dumps(self.cfg))
        stream = io.StringIO()
        with (patch('native.company.Engine', self.make_engine),
              patch('integrations.quality_feedback.__main__.QMS', client), redirect_stdout(stream)):
            generate.main(['feedback', '--run', str(self.root), command] +
                          [part for k, v in fields.items() for part in ('--' + k.replace('_', '-'), v)])
        return json.loads(stream.getvalue())

    def test_salvage_lifecycle_with_colliding_native_ids(self):
        with self.assertRaisesRegex(ValueError, 'posted evidence'):
            self.invoke('report', **{**self.report, 'batch': 'ORIGINAL'})
        with self.assertRaisesRegex(ValueError, 'another QMS source'):
            self.invoke('report', **self.report, qms_source='main')
        result = self.invoke('report', **self.report)
        self.assertEqual(result, dict(complaint='NC', action='CAPA', delivery='DN', qms_source='east'))
        case = self.qms['east'].records[CASE]['NC']
        self.assertEqual(tuple(case[key] for key in ('custom_qms_batch', 'custom_delivery_batch',
                                                   'custom_delivery_qms_source', 'custom_delivery_inspection')),
                         ('ORIGINAL', 'LOT', 'east', 'QI'))
        self.qms['main'].records[CASE]['NC'] = deepcopy(case)
        self.qms['main'].records[ACTION]['CAPA'] = deepcopy(self.qms['east'].records[ACTION]['CAPA'])
        with self.assertRaisesRegex(ValueError, 'another QMS source'):
            self.invoke('contain', complaint='NC', release='QI', qms_source='main')
        with self.assertRaisesRegex(ValueError, 'shipped release'):
            self.invoke('contain', complaint='NC', release='OTHER', qms_source='east')
        with self.assertRaisesRegex(ValueError, 'native withdrawal'):
            self.invoke('receive-return', **self.receipt)
        with self.assertRaisesRegex(ValueError, 'revoked shipment release'):
            self.invoke('authorize-withdrawal', complaint='NC', release='QI', reason='Collect', qms_source='east')
        hold = self.invoke('contain', complaint='NC', release='QI', qms_source='east')
        self.assertEqual(hold['disposition'], 'Revoked')
        self.assertEqual(self.syncs[-1]['rework_key'], 'rework')
        self.assertEqual(self.qms['main'].records[INSPECTION]['QI']['workflow_state'], 'Released')

        direct = self.qms['east'].client('approver')
        with self.assertRaisesRegex(ValueError, 'pinned revoked release'):
            direct.update(CASE, 'NC', custom_withdrawal_release='OTHER', custom_withdrawal_reason='Wrong')
        with self.assertRaisesRegex(ValueError, 'release binding is final'):
            direct.update(CASE, 'NC', custom_delivery_qms_source='main')
        decision = dict(complaint='NC', release='QI', reason='Collect', qms_source='east')
        self.assertEqual(self.invoke('authorize-withdrawal', **decision),
                         self.invoke('authorize-withdrawal', **decision))
        with self.assertRaisesRegex(ValueError, 'posted evidence'):
            self.invoke('receive-return', **{**self.receipt, 'batch': 'ORIGINAL'})
        with self.assertRaisesRegex(ValueError, 'another QMS source'):
            self.invoke('receive-return', **self.receipt, qms_source='main')
        receipt = self.invoke('receive-return', **self.receipt)
        self.assertEqual(receipt, self.invoke('receive-return', **self.receipt))
        self.assertEqual((receipt['batch'], receipt['qms_batch'], receipt['qms_source']), ('LOT', 'ORIGINAL', 'east'))
        returned = self.erp.notes[receipt['return_note']]
        self.assertEqual((returned['docstatus'], returned['items'][0]['batch_no']), (1, 'LOT'))
        self.assertEqual(self.note['docstatus'], 1)

        self.qms['east'].records[INSPECTION]['BAD'] = dict(
            self.qms['east'].records[INSPECTION]['NEXT'], name='BAD', custom_rework_output_batch='WRONG')
        changes = dict(custom_effectiveness_inspection='BAD', custom_effectiveness_result='Passed',
                       resolutions=[dict(problem='Leak', status='Completed')])
        with self.assertRaisesRegex(ValueError, 'shipped lot'):
            direct.update(ACTION, 'CAPA', **changes)
        with self.assertRaisesRegex(ValueError, 'shipped lot'):
            self.invoke('close', action='CAPA', inspection='BAD', assessment='Passed', qms_source='east')
        with self.assertRaisesRegex(ValueError, 'another QMS source'):
            self.invoke('close', action='CAPA', inspection='NEXT', assessment='Passed', qms_source='main')
        closure = dict(action='CAPA', inspection='NEXT', assessment='Passed', qms_source='east')
        closed = self.invoke('close', **closure)
        self.assertEqual(self.invoke('close', **closure), closed)
        before = deepcopy(self.qms['east'].records)
        self.assertEqual(closed['status'], 'Completed')
        self.assertEqual(self.invoke('report', **self.report), result)
        self.assertEqual(self.qms['east'].records, before)
        with self.assertRaisesRegex(ValueError, 'readback mismatch'):
            self.invoke('report', **{**self.report, 'description': 'Changed'})

        scope = self.qms['east'].records
        data = {'company-erp': {
                    'Delivery Note': [project('Delivery Note', doc) for doc in (self.note, returned)],
                    'Serial and Batch Bundle': [self.erp.bundle, dict(
                        name=receipt['bundle'], voucher_type='Delivery Note', voucher_no=returned['name'],
                        voucher_detail_no=receipt['return_row'], docstatus=1, item_code='Item',
                        warehouse='Quarantine', type_of_transaction='Inward', entries=[dict(batch_no='LOT', qty=1)])]},
                'east': {CASE: [project(CASE, scope[CASE]['NC'])], INSPECTION: [scope[INSPECTION]['QI']]},
                'main': {CASE: [case], INSPECTION: [self.qms['main'].records[INSPECTION]['QI']]}}
        edges = crosslinks(data)['edges']
        self.assertIn((('east', CASE, 'NC'), 'shipment', ('company-erp', 'Delivery Note', 'DN')), edges)
        self.assertIn((('east', CASE, 'NC'), 'complaint_release', ('east', INSPECTION, 'QI')), edges)
        self.assertIn((('company-erp', 'Delivery Note', returned['name']), 'return_case', ('east', CASE, 'NC')), edges)
        self.assertNotIn((('main', CASE, 'NC'), 'complaint_release', ('east', INSPECTION, 'QI')), edges)

    def test_dispatch_version_and_partial_allocation_must_match_native_evidence(self):
        def write_entry(qty=4, lease=None):
            row = dict(batch='LOT', qty=qty, disposition=self.pin)
            if lease:
                row['qms_lease'] = lease
            self.note['custom_norvale_quality_releases'] = json.dumps([row])
        self.pin.update(version='earlier', qty=4)
        write_entry()
        with self.assertRaisesRegex(ValueError, 'release version'):
            self.invoke('report', **self.report)
        self.pin['version'] = 'v1'
        write_entry(qty=2)
        with self.assertRaisesRegex(ValueError, 'released allocation'):
            self.invoke('report', **self.report)
        write_entry(lease='lease1')
        site = self.qms['east']
        sample = site.records[INSPECTION]['QI']
        sample.update(workflow_state='Revoked', modified='v2')
        claim = dict(batch='ORIGINAL', item='Item', uom='Nos', qty=4, dispatch_qty=4,
                     inspection='QI', version='v1')
        lease = dict(name='lease1', lease_state='submitted')
        site.records['Norvale Dispatch Lease'] = {'lease1':lease}
        for field, wrong in [('dispatch_qty',2), ('qty',2), ('version','different')]:
            lease['request_json'] = json.dumps(dict(id='lease1', delivery='DN', scopes=[{**claim,field:wrong}]))
            with self.assertRaisesRegex(ValueError, 'QMS dispatch lease'):
                self.invoke('report', **self.report)
        lease['request_json'] = json.dumps(dict(id='lease1', delivery='DN', scopes=[claim]))
        self.assertEqual(self.invoke('report', **self.report)['complaint'], 'NC')

    def test_primary_also_checks_registered_target(self):
        self.pin['qms_source'] = 'main'
        self.bind_pin()
        with self.assertRaisesRegex(ValueError, 'registered lot authority'):
            self.invoke('report', **self.report)
        self.sites['east']['qms_source'] = 'main'
        with self.assertRaisesRegex(ValueError, 'registered lot authority'):
            self.invoke('report', **self.report)
        self.registered = 'main'
        self.assertEqual(self.invoke('report', **self.report)['qms_source'], 'main')

    def test_legacy_remarks_case_and_receipt_recovery(self):
        self.batch = 'LOT'
        self.qms['east'] = Site('LOT', 'LOT')
        del self.pin['qms_source'], self.pin['qms_batch']
        self.bind_pin('remarks')
        with self.assertRaisesRegex(ValueError, 'another QMS source'):
            self.invoke('report', **self.report, qms_source='main')
        self.invoke('report', **self.report)
        legacy = self.qms['east'].records[CASE]['NC']
        for field in ('custom_delivery_batch', 'custom_delivery_qms_source', 'custom_delivery_inspection'):
            del legacy[field]
        self.invoke('report', **self.report)
        self.assertNotIn('custom_delivery_inspection', self.qms['east'].records[CASE]['NC'])
        self.invoke('contain', complaint='NC', release='QI', qms_source='east')
        self.invoke('authorize-withdrawal', complaint='NC', release='QI', reason='Collect', qms_source='east')
        self.assertEqual(self.qms['east'].records[CASE]['NC']['custom_delivery_inspection'], 'QI')
        receipt = self.invoke('receive-return', **self.receipt)
        # Model a return written by the prior release with identical receipt intent.
        saved = self.root / 'state.json'
        state = json.loads(saved.read_text())
        entry, = state['company_erp']['customer_returns'].values()
        del entry['receipt']['qms_batch']
        saved.write_text(json.dumps(state))
        doc = self.erp.notes[receipt['return_note']]
        doc['custom_norvale_quality_releases'] = json.dumps({'customer_return': entry['receipt']})
        self.assertEqual(self.invoke('receive-return', **self.receipt)['return_note'], receipt['return_note'])
        self.note['remarks'] = json.dumps([dict(batch='LOT', disposition=self.pin)] * 2)
        with self.assertRaisesRegex(ValueError, 'one released allocation'):
            self.invoke('report', **self.report)
