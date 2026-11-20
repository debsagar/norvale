"""Ordinary clock handoffs and independent native sample-link ingestion."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import unquote

from integrations.lake.adapter import Lake
from integrations.lake.boundary import Frappe, crosslinks
from integrations.qms.adapter import INSPECTION, QMS
from integrations.qms.sample_reference import ENTITY, INCOMING, KEY, SCOPE
from native.company import save
from native.company_erp import CompanyERP, Pending
from .lake import scan
from .operating import Operating
from .test_operating import bind_legacy
from .test_registered_lab import NativeLab
from .timing import handle


class PrimarySampleTest(TestCase):
    def test_failed_lot_survives_lost_post_and_reopen_and_is_ingested_without_transfer(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            lot = dict(batch='B', item='RAW', qty=12, uom='Nos', kind='incoming',
                       source_type='Purchase Receipt', source_id='PR')
            company = SimpleNamespace(run=run, config=dict(company_operating=True,
                company_timing={'lab_minutes': 90}), date=lambda: '2027-01-01', persist=Mock(),
                day=0, log=Mock(), state={})
            fixture = SimpleNamespace(e=company, cfg=dict(lab=dict(analyst_user='analyst',
                procedure='PROCEDURE', parameter='INDEX'), qms_approver='approver',
                qms_source='qms-1', erp_source='erp-1'))
            bind_legacy(fixture, lot)
            cfg = fixture.cfg
            cfg['lab']['references'].clear()
            for role in ('analyst', 'approver'):
                cfg['lab'][role] = dict(cfg['qms_reader'], api_key=role)
            cfg['qms_reader']['api_key'] = 'reader'
            marker = 'Norvale Sample Site site-1'
            writer = dict(connection=dict(cfg['qms_reader'], api_key='reference_writer'),
                          user='reference_writer', instance=marker, target=['https://qms.test', 'qms.localhost'],
                          qms_source='qms-1', erp_source='erp-1', analyst_user='analyst', approver_user='approver',
                          erp_instance='12345678123442348123123456781234')
            path = run / 'env/company-sources.json'
            save(path, cfg)
            clock = SimpleNamespace(has_event=lambda _: False, schedule=Mock(), now=550, state={})
            state = dict(lots={'B': lot}, pending={})
            flow = SimpleNamespace(e=company, state=state, sync=Mock())
            flow.attempt = CompanyERP.attempt.__get__(flow)
            company.company_erp = flow
            native = NativeLab('unused')
            native.fail_reference = True
            for name in (INCOMING, marker):
                native.docs['Quality Inspection Parameter', name] = {'name': name}
            event = {'role': 'LAB', 'at': clock.now}
            wrong = set()
            calls = []

            class Client(QMS):
                def __init__(self, url, api_key, **options):
                    self.user = api_key

                def request(self, method, path, data=None):
                    calls.append((self.user, method, path))
                    if path == 'method/frappe.auth.get_logged_user' and self.user in wrong:
                        return {'message': 'wrong'}
                    if method == 'POST' and path == self.resource('Item'):
                        assert self.user == 'reference_writer'
                        native.docs['Item', data['item_code']] = dict(data, name=data['item_code'],
                                                                     has_serial_no=0, disabled=0)
                        return {'data': native.docs['Item', data['item_code']]}
                    return native.respond(self.user, method, path, data, event)

            def dispatch(payload, role='LAB'):
                event['role'] = role
                handle(company, clock, json.loads(json.dumps(payload)))
                return clock.schedule.call_args.kwargs['payload']

            initial = dict(job=['lab', 'B', None], edge='sample', attempt=0, rows=None)
            with (patch('integrations.company_run.operating.QMS', Client),
                  patch.object(Operating, 'measurement', return_value=6.4),
                  patch('subprocess.check_output', side_effect=AssertionError('No bench')),
                  patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No network'))):
                retry = dispatch(initial)
                self.assertIn('opt-in', state['pending']['operating-sample:B']['last_error'])
                self.assertFalse(calls)
                cfg['lab']['sample_writer'] = writer
                save(path, cfg)
                wrong.add('reference_writer')
                retry = dispatch(retry)
                self.assertIn('commissioned lab identity', state['pending']['operating-sample:B']['last_error'])
                wrong.clear()
                incubate = dispatch(retry)
                self.assertEqual(incubate['edge'], 'incubate')
                ref = incubate['rows']['reference']
                self.assertEqual(sum(kind == 'Stock Entry' for kind, _ in native.calls), 1)
                self.assertEqual(Operating(company).sample('B'), incubate['rows'])
                result = dispatch(incubate)
                for field, value in (('source_row', 'other-row'), ('batch', 'other-lot'), ('writer', 'other-user'),
                                     ('erp_instance', 'other-ERP'), ('qms_instance', 'other-QMS')):
                    qms_ref = native.docs['Stock Entry', ref]
                    original = qms_ref[SCOPE]
                    qms_ref[SCOPE] = json.dumps(dict(json.loads(original), **{field: value}))
                    with self.assertRaises(Pending):
                        Operating(company).record('B', result['rows'])
                    qms_ref[SCOPE] = original
                actor = Operating(company)
                actor.cfg['lab']['analyst']['site'] = 'other.site'
                with self.assertRaisesRegex(Pending, 'different QMS site'):
                    actor.record('B', result['rows'])
                erp_row = company.api['BUYER'].get.return_value['items'][0]
                erp_row['name'] = 'not-the-row'
                with self.assertRaisesRegex(Pending, 'submitted ERP row'):
                    Operating(company).record('B', result['rows'])
                erp_row['name'] = lot['source_row']
                review = dispatch(result)
                self.assertEqual(review['edge'], 'review')
                wrong.add('approver')
                retry_review = dispatch(review, 'QA')
                self.assertIn('commissioned lab identity', state['pending']['operating-review:B']['last_error'])
                wrong.clear()
                dispatch(retry_review, 'QA')
                dispatch(retry_review, 'QA')
                self.assertEqual(company.state['operating_lot_rejections']['B'], review['rows']['inspection'])
                self.assertEqual([action for action, *_ in native.transitions], ['Record', 'Reject Lot'])

            qms_ref = native.docs['Stock Entry', ref]
            qms_ref['remarks'] = 'private narrative'
            qms_ref['items'][0]['private_state'] = 'hidden'
            dart = deepcopy(native.docs[INSPECTION, review['rows']['inspection']])
            dart['modified'] = '2027-01-01T12:00:00'

            dart.update(workflow_state='Rework Authorized', custom_rework_input_batch='B',
                        custom_rework_salvage_quantity=11)
            retest = dict(dart, name='RETEST', status='Accepted', workflow_state='Released',
                          custom_retest_of=dart['name'], custom_rework_execution='mes:mrp.production:2',
                          custom_rework_output_batch='SALVAGE', custom_lot_quantity=11)
            def names(_, entity):
                return ['RETEST', dart['name']] if entity == INSPECTION else []

            def get(_, url):
                kind = unquote(url.split('/')[-2])
                if kind == INSPECTION:
                    return retest if url.endswith('/RETEST') else dart
                return qms_ref

            archive = root / 'lake'
            with (patch.object(Frappe, '__init__', return_value=None),
                  patch.object(Frappe, 'names', names), patch.object(Frappe, 'get', get)):
                client = Frappe('unused', 'r', 's')
                for field, wrong_value in [('custom_lot_quantity', 12), ('custom_rework_output_batch', 'B'),
                                           ('child_row_reference', 'other-row')]:
                    with patch.dict(retest, {field: wrong_value}), self.assertRaisesRegex(ValueError, 'Salvage retest'):
                        list(client.documents(ENTITY))
                scan('qms', dict(source='qms-1', reader=dict(url='http://qms.test', api_key='r',
                          api_secret='s'), entities=[INSPECTION, ENTITY], sample_scope=dict(
                              qms_instance=marker, writer=writer['user'], erp_source=writer['erp_source'],
                              erp_instance=writer['erp_instance'])), archive)
            lake = Lake(archive / 'delta', {})
            archived = [json.loads(row['payload']) for row in lake._rows() if row['entity'] == ENTITY]
            expected_fields = {'name', 'creation', 'erp_source', 'source_type', 'source_id', 'source_row',
                               'batch', 'item', 'qty', 'uom', 'qms_row'}
            self.assertEqual(set(archived[0]), expected_fields)
            erp = company.api['BUYER'].get.return_value
            links = crosslinks({'qms-1': {ENTITY: archived, INSPECTION: [dart]},
                                'erp-1': {'Purchase Receipt': [erp]}})
            self.assertIn((('qms-1', INSPECTION, dart['name']), 'sample_reference',
                           ('qms-1', ENTITY, ref)), links['edges'])
            self.assertIn((('qms-1', ENTITY, ref), 'sample_erp_row',
                           ('erp-1', 'Purchase Receipt', 'PR')), links['edges'])
            self.assertNotIn(KEY, archived[0])
