"""Offline QMS transaction and scheduled archive fixtures."""
import io
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit

from integrations.company_run.lake import scan_cycle
from integrations.lake.adapter import Lake
from integrations.lake.boundary import Frappe, scan_key
from integrations.qms.disposition_history import ENTITY, RECORD


INSPECTION = 'Quality Inspection'
RELEASED = '2027-02-10 12:00:00.123456'
REVOKED = '2027-02-10 12:01:00.654321'


def native_events():
    """Execute the installed script body against saved native rows."""
    rows = {}
    frappe = Mock()
    doc = Mock(name='inspection document')
    doc.name = 'QI-1'
    saved = SimpleNamespace(docstatus=1, status='Accepted', item_code='SKU', batch_no='ORIGINAL',
                            custom_rework_output_batch='SALVAGE', custom_spec_revision='SPEC-1',
                            custom_lot_quantity=Decimal('8.0'), custom_lot_uom='Nos')
    frappe.db.get_value.return_value = saved
    frappe.throw.side_effect = ValueError
    for state, version, actor, prior in (
        ('Recorded', RELEASED, 'analyst', 'Draft'),
        ('Released', RELEASED, 'approver', 'Recorded'),
        ('Revoked', REVOKED, 'reviewer', 'Released'),
        ('Revoked', '2027-02-10 12:02:00', 'reviewer', 'Revoked'),
    ):
        doc.workflow_state = saved.workflow_state = state
        doc.get_doc_before_save.return_value = SimpleNamespace(workflow_state=prior)
        saved.modified, saved.modified_by = version, actor
        exec(RECORD, dict(doc=doc, frappe=frappe))
    for number, call in enumerate(frappe.get_doc.call_args_list, start=1):
        event = call.args[0]
        assert event.pop('doctype') == ENTITY
        event['lot_quantity'] = float(event['lot_quantity'])
        rows[str(number)] = dict(event, name=str(number), modified=REVOKED)
    assert frappe.get_doc.return_value.insert.call_count == 2
    frappe.get_doc.return_value.insert.assert_called_with(ignore_permissions=True)
    return rows


class DispositionScanTests(TestCase):
    def test_scoped_scheduler_retries_without_losing_history_or_duplicating_events(self):
        native = native_events()
        sites = {'east': {}, 'west': {}, 'legacy': None}
        outage = set()

        def response(reader, path):
            site = reader.headers['X-Frappe-Site-Name']
            self.assertEqual(reader.headers['Authorization'], f'token {site}-reader:secret')
            entity, *identity = unquote(urlsplit(path).path).split('/')[3:]
            if entity == INSPECTION:
                if not identity:
                    return {'data': [{'name': 'QI-1'}] if sites[site] else []}
                return {'data': dict(name='QI-1', workflow_state='Revoked', modified=REVOKED)}
            if entity == "Norvale Dispatch Lease":
                return {"data": []}
            self.assertEqual(entity, ENTITY)
            if sites[site] is None:
                body = dict(exc_type='DoesNotExistError',
                            _server_messages=json.dumps([json.dumps(dict(message=f'DocType {ENTITY} not found'))]))
                raise HTTPError(path, 404, 'Not Found', {}, io.BytesIO(json.dumps(body).encode()))
            if not identity:
                return {'data': [{'name': key} for key in sites[site]]}
            if identity == ['2'] and site in outage:
                raise OSError('source interrupted mid-scan')
            return {'data': sites[site][identity[0]]}

        def config(site):
            return dict(source=site, entities=[INSPECTION],
                        reader=dict(url='http://localhost', site=site, api_key=f'{site}-reader', api_secret='secret'))

        with TemporaryDirectory() as tmp, patch.object(Frappe, 'response', response):
            root = Path(tmp)
            self.assertEqual(scan_cycle({'qms': config('east')}, root), ({'east'}, []))
            sites['east'] = native
            sites['west'] = {key: dict(event, spec_revision='OTHER', lot_quantity=9)
                            for key, event in native.items()}
            outage.add('east')
            self.assertEqual(scan_cycle({'qms': config('east')}, root), ({'east'}, [('east', 'ExceptionGroup')]))
            scan_cycle({'qms': config('west')}, root)
            self.assertEqual(scan_cycle({'qms': config('legacy')}, root), ({'legacy'}, []))
            state = json.loads((root/'state.json').read_text())
            key = scan_key('east', ENTITY)
            self.assertEqual(state[key]['records'], 0)
            self.assertEqual(state[key]['error'], 'OSError')
            outage.clear()
            # Fresh scheduler, reader and table handles use only the retained archive.
            self.assertEqual(scan_cycle({'qms': config('east')}, root), ({'east'}, []))
            for site in ('east', 'west'):
                self.assertEqual(scan_cycle({'qms': config(site)}, root), ({site}, []))
            archive = Lake(root/'delta', {})._rows()
            rows = {(row['source'], row['native_id']): json.loads(row['payload'])
                    for row in archive if row['entity'] == ENTITY}
            self.assertEqual(len([row for row in archive if row['entity'] == ENTITY]), 4)
            self.assertEqual(rows['east', '1']['inspection_version'], RELEASED)
            self.assertEqual(rows['east', '2']['inspection_version'], REVOKED)
            self.assertEqual(rows['east', '1']['approver'], 'approver')
            self.assertEqual(rows['east', '1']['output_batch'], 'SALVAGE')
            self.assertEqual(rows['east', '1']['lot_quantity'], 8)
            self.assertEqual(rows['west', '1']['spec_revision'], 'OTHER')
            self.assertTrue(all(json.loads(row['payload'])['workflow_state'] == 'Revoked'
                                for row in archive if row['entity'] == INSPECTION))
            from integrations.company_run.verify import ordinary_revocations
            from integrations.lake.genealogy import ShipmentHistory
            pin = dict(batch='SALVAGE', qty=3, disposition=dict(id='QI-1', status='released', item='SKU',
                       qms_source='east', qms_batch='ORIGINAL', batch='SALVAGE', version=RELEASED,
                       qty=8, uom='Nos', approver='approver'))
            engine = Mock()
            flow = engine.company_erp
            flow.state = dict(shipments={'attempt': dict(rows=[pin], allocation_change=dict(delivery='DN'))},
                              lots={'SALVAGE': {}})
            flow.authority.quality_route.return_value = (dict(qms_source='east', qms_reader={}), {})
            warehouse = Mock()
            engine.api = {'WAREHOUSE': warehouse}
            warehouse.get.return_value = dict(name='DN', docstatus=0, remarks=json.dumps([pin]),
                items=[dict(batch_no='SALVAGE', item_code='SKU', qty=3)])
            warehouse.list.return_value = []
            with patch('integrations.qms.adapter.QMS') as client:
                client.return_value.get.return_value = dict(name='QI-1', workflow_state='Revoked',
                    modified=REVOKED, docstatus=1, batch_no='ORIGINAL', item_code='SKU')
                result, = ordinary_revocations(engine, config('east'), ShipmentHistory(archive, 'erp'), [])
            self.assertTrue(result['verified'])
            self.assertEqual(result['disposition_evidence'], [['east', ENTITY, '1'], ['east', ENTITY, '2']])

            sites['east'] = {'2': native['2']}
            self.assertEqual(scan_cycle({'qms': config('east')}, root), ({'east'}, [('east', 'ExceptionGroup')]))
            self.assertEqual(Lake(root/'delta', {})._rows(), archive)
