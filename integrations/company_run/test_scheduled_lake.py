"""Exercise the ordinary generator's analytical scan and completed-horizon retry."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

import generate
from company import Engine
from integrations.lake.adapter import Lake
from integrations.lake.boundary import Frappe, DISPOSITION_ENTITY


class ScheduledLake(TestCase):
    def test_business_cutoffs_independent_failure_and_resume(self):
        with TemporaryDirectory() as temp:
            run = Path(temp)
            env = run/'env'
            env.mkdir()
            archive = run/'lake'
            settings = env/'analytics.json'
            reader = dict(url='http://local', api_key='reader', api_secret='test')
            settings.write_text(json.dumps({
                'erp': dict(source='operations', entities=['Work Order'], reader=reader),
                'qms': dict(source='quality', entities=['Quality Inspection'], reader=reader),
                'plm': dict(source='definitions', reader=reader, parts=[])}))
            (env/'company-sources.json').write_text(json.dumps({
                'plm_source': 'definitions', 'plm':reader, 'products':{}}))
            (env/'planning-report.json').write_text(json.dumps({
                'settings':str(settings), 'archive':str(archive)}))

            def controller():
                e = Engine.__new__(Engine)
                e.run, e.day = run, 0
                e.config = dict(days=1, company_operating=True, company_timing={}, report_every=7)
                e.state = dict(day=0, docs={}, completed=[])
                e.docs = e.state['docs']
                e.log, e.advance_clock, e.run_additions = Mock(), Mock(), Mock()
                return e

            e = controller()
            e.state['completed'] = [key for _, key, _ in e.events()
                                    if key not in ('d0000:management', 'd0000:native-clock')]
            e.persist()
            recover = False
            def documents(entity):
                if entity in (DISPOSITION_ENTITY, 'Norvale Dispatch Lease'):
                    return []
                if entity == 'Quality Inspection' and not recover:
                    raise TimeoutError()
                return [dict(name=entity+'-1', modified='v1')]

            with patch.object(Frappe, 'documents', side_effect=documents), patch.object(
                    Frappe, '__init__', return_value=None), patch('integrations.plm.PLM'), patch(
                    'integrations.company_run.publications.collect') as publications:
                with patch('company.Engine', return_value=e):
                    generate.main(['--run', temp])
                self.assertEqual(e.state['lake_scans']['last_success_minute']['operations'], 1439)
                self.assertNotIn('quality', e.state['lake_scans']['last_success_minute'])
                self.assertIn(('quality', 'ExceptionGroup'), e.state['lake_scans']['errors'])
                recover = True
                resumed = controller()
                with patch('company.Engine', return_value=resumed):
                    generate.main(['--run', temp])
                    count = publications.call_count
                    generate.main(['--run', temp])
                self.assertEqual(publications.call_count, count)
                self.assertEqual(resumed.state['lake_scans']['errors'], [])
                self.assertEqual(resumed.state['lake_scans']['last_success_minute']['quality'], 1439)
                rows = Lake(archive/'delta', {})._rows()
                self.assertCountEqual([r['source'] for r in rows], ['operations', 'quality'])
                self.assertTrue(all(c.kwargs['on'] == '2027-01-01' for c in publications.call_args_list))
