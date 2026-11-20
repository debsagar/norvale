"""Exercise the ordinary disposition check against submitted native-format measurement fields."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from integrations.qms.rework import ReworkQuality
from native.company_rework import Rework


class ReworkDisposition(unittest.TestCase):
    def test_released_retest_requires_matching_original_measurements(self):
        scope = dict(batch_no='BATCH-A', item_code='FG-1', custom_spec_revision='R1',
                     custom_lot_uom='Nos', docstatus=1, report_date='2027-01-03')
        assay = dict(numeric=1, specification='pH', min_value=5, max_value=6)
        parent = dict(scope, name='QI-1', modified='v1', modified_by='qa-manager',
                      workflow_state='Rework Authorized', status='Rejected',
                      readings=[dict(assay, status='Rejected', reading_1='6.5')])
        saved = dict(scope, name='QI-2', modified='v3', creation='v2', modified_by='qa-manager',
                     inspected_by='lab-analyst', custom_retest_of=parent['name'],
                     workflow_state='Revoked', status='Accepted',
                     readings=[dict(assay, status='Accepted', reading_1='5.5')])
        authority = dict(inspection=parent['name'], version=parent['modified'],
                         approver=parent['modified_by'], batch=parent['batch_no'])
        reader = Mock()
        reader.list.return_value = [{'name': saved['name'], 'creation': saved['creation']}]
        reader.get.side_effect = lambda entity, name: child if name == saved['name'] else parent
        quality = ReworkQuality(reader, None, saved['inspected_by'], parent['modified_by'])
        rework = Rework(None, quality, {}, qms_source='lab')
        key = 'repair'
        flow = SimpleNamespace(state={'rework': {key: {'authority': authority, 'retest': saved['name'],
                                                        'execution': 'mes:mrp.production:2'}}},
                               e=SimpleNamespace(date=lambda: saved['report_date']))
        lot = dict(batch='SALVAGE', rework_key=key, item=saved['item_code'], qty=7,
                   uom=saved['custom_lot_uom'], revision={'inspection_spec': saved['custom_spec_revision']})
        child = dict(saved, custom_lot_quantity=7, custom_rework_execution='mes:mrp.production:2',
                     custom_rework_output_batch='SALVAGE')
        self.assertIsNone(rework.disposition(flow, lot))
        approved = dict(child, workflow_state='Released', modified_by=quality.approver_user)
        child = approved
        self.assertEqual(rework.disposition(flow, lot)['qms_source'], 'lab')
        for changes in ({'batch_no': 'another'}, {'inspected_by': 'another'}, {'readings': []},
                        {'readings': [dict(approved['readings'][0], reading_1='7.2')]},
                        {'readings': [dict(approved['readings'][0], max_value=7)]}):
            with self.subTest(fields=list(changes)):
                child = {**approved, **changes}
                self.assertIsNone(rework.disposition(flow, lot))
        child = approved
        parent = dict(parent)
        parent['modified'] = 'later version'
        self.assertIsNone(rework.disposition(flow, lot))
