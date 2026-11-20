import tempfile
import unittest
from pathlib import Path

from integrations.company_run.authority import Authority
from integrations.company_run.__main__ import AcceptanceEngine
from native.company_erp import Pending


class AcceptanceTests(unittest.TestCase):
    def test_missing_sources_leave_hold(self):
        with tempfile.TemporaryDirectory() as temp:
            authority = Authority(Path(temp))
            lot = dict(item='sample')
            self.assertIsNone(authority.disposition(lot, as_of='2027-01-01', role='QA'))
            self.assertIsNone(authority.disposition(lot, as_of='2027-01-01', role='PRODUCTION'))
            with self.assertRaises(Pending):
                authority.disposition(lot, as_of='2027-01-01', role='SALES')
            with self.assertRaises(Pending):
                authority.definition('sample', as_of='2027-01-01', role='PLANNER')

    def test_finished_lot_keeps_its_inspection_spec_across_changes(self):
        from .quality import finished_specification, lot_specification
        publication = dict(native_id=7,source_version='A')
        first = finished_specification('lotion','assay-1','plm-east',publication)
        for item,method,source,revision in [
            ('lotion','assay-1','plm-east',{**publication,'source_version':'B'}),
            ('lotion','assay-2','plm-east',publication),
            ('lotion','assay-1','plm-west',publication)]:
            self.assertNotEqual(first,finished_specification(item,method,source,revision))
        self.assertEqual(lot_specification({'specifications':{'lotion':'method-changed'}},
                         {'item':'lotion','kind':'finished','revision':{'inspection_spec':first}}),first)
        self.assertIsNone(lot_specification({'specifications':{'lotion':'method-changed'}},
                          {'item':'lotion','kind':'finished','revision':{}}))

    def test_future_inspection_cannot_authorize_current_dispatch(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            authority = Authority(Path(temp))
            authority.settings = lambda: dict(qms_reader={},qms_approver='qa')
            with patch('integrations.company_run.authority.QMS') as qms:
                qms.return_value.release.return_value = dict(name='QI-1',modified='v2')
                qms.return_value.get.return_value = dict(report_date='2027-02-01',modified='v2',
                                                       modified_by='qa',item_code='lotion',batch_no='B',
                                                       custom_spec_revision='specific',custom_lot_quantity=3,
                                                       custom_lot_uom='ea',docstatus=1,status='Accepted',
                                                       workflow_state='Released')
                lot=dict(item='lotion',kind='finished',batch='B',qty=3,uom='ea',
                         revision={'inspection_spec':'specific'})
                self.assertIsNone(authority.disposition(lot,as_of='2027-01-31',role='WAREHOUSE'))
                self.assertEqual(authority.disposition(lot,as_of='2027-02-01',role='WAREHOUSE')['id'],'QI-1')
                qms.return_value.release.assert_called_with('B','specific',3,'ea')

    def test_dispatch_lease_maps_pinned_spec_and_requires_same_qms_site(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            authority = Authority(Path(temp))
            reader = {'url': 'http://qms.test', 'api_key': 'read'}
            dispatcher = {**reader, 'api_key': 'dispatch'}
            cfg = dict(qms_reader=reader, qms_dispatcher=dispatcher, qms_source='quality')
            authority.settings = lambda: cfg
            lot = dict(batch='B', item='lotion', uom='Nos', kind='finished',
                       revision={'version': 'plm', 'inspection_spec': 'pinned-assay'})
            decision = dict(id='QI-1', version='v2', approver='QA', qty=10, qms_source='quality')
            request = dict(id='L', delivery='DN',
                           scopes=[dict(lot=lot, disposition=decision, dispatch_qty=2)])
            with patch('integrations.company_run.authority.QMS') as qms:
                authority.dispatch_lease(request, outcome='active', role='WAREHOUSE')
                qms.assert_called_once_with(**dispatcher)
                qms.return_value.dispatch_lease.assert_called_once_with(
                    dict(id='L', delivery='DN', scopes=[dict(batch='B', item='lotion', uom='Nos',
                         revision='pinned-assay', qty=10, dispatch_qty=2,
                         inspection='QI-1', version='v2', approver='QA')]), outcome='active')
                with self.assertRaises(Pending):
                    authority.dispatch_lease(request, outcome='active', role='QA')
                dispatcher['site'] = 'other.localhost'
                with self.assertRaises(Pending):
                    authority.dispatch_lease(request, outcome='active', role='WAREHOUSE')
                self.assertEqual(qms.call_count, 1)

    def test_unknown_local_decision_stops(self):
        engine = object.__new__(AcceptanceEngine)
        engine.state = {'decisions': {}}
        with self.assertRaisesRegex(ValueError, 'No acceptance input'):
            engine.choose('po-review:1', 'BUYER', {}, '')

    def test_scoped_purchase_input_survives_restart(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            engine = object.__new__(AcceptanceEngine)
            engine.run, engine.day = Path(temp), 0
            engine.state = {'decisions': {}}
            evidence = dict(supplier='A', grand_total=11,
                            supplier_quote=dict(supplier='A', grand_total=11, valid_till='2027-01-01'))
            first = engine.choose('po-review:1', 'SCM_MGR', evidence, '')
            self.assertEqual(first['action'], 'approve')
            engine.state = json.loads((engine.run / 'state.json').read_text())
            self.assertEqual(engine.choose('po-review:1', 'SCM_MGR', evidence, ''), first)
            evidence['supplier_quote']['grand_total'] = 10
            self.assertEqual(engine.choose('po-review:2', 'SCM_MGR', evidence, '')['action'], 'defer')

    def test_ordinary_model_failure_remains_visible(self):
        from unittest.mock import patch
        from native.company import Engine
        engine = object.__new__(Engine)
        engine.day, engine.state = 0, {'decisions': {}}
        with patch.dict('os.environ', {'NORVALE_OFFLINE': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'no approval fallback'):
                engine.decision('po-review:1', 'SCM_MGR', {}, '')
        self.assertFalse(engine.state['decisions'])
