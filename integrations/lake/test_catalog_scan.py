from types import SimpleNamespace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from integrations.company_run.lake import scan, scan_cycle, scheduled_scan


class CatalogScanTests(TestCase):
    def test_failed_family_lookup_marks_bom_scan_stale(self):
        with TemporaryDirectory() as tmp, patch('integrations.plm.PLM') as client:
            root = Path(tmp)
            cfg = dict(source='plant-plm', reader={}, parts=[17])
            client.return_value._family.return_value = []
            scan('plm', cfg, root)
            def status():
                return next(iter(json.loads((root/'state.json').read_text()).values()))
            success = status()['last_success']
            client.return_value._family.side_effect = PermissionError()
            with self.assertRaises(PermissionError):
                scan('plm', cfg, root)
            self.assertEqual(status()['error'], 'PermissionError')
            self.assertEqual(status()['last_success'], success)
            client.return_value._family.assert_called_with(17)

    def test_new_family_is_visible_on_next_pass_and_other_sources_survive_a_failure(self):
        catalog = {}
        settings = {
            'plm': {'source': 'plm-A', 'reader': {'url': 'http://primary'}},
            'qms': {'source': 'lab-A', 'reader': {'url': 'http://lab-a'}, 'entities': ['Quality Inspection']},
        }
        family = dict(item='SKU', family=42, plm_source='plm-B', qms_source='lab-B',
                      plm={'url':'http://secondary'}, qms_reader={'url': 'http://lab-b'}, mes={'source_id':'plant-B'},
                      mapping={'PLM family name': {'mes': {'settings': {'company_id': 2}, 'output': {'item': 7}},
                                                   'erp': {'output': {'item': 'SKU'}}}})
        observed = []
        def read(kind, cfg, root):
            observed.append((kind, cfg))
            if kind == 'mes':
                raise OSError('offline')
        with TemporaryDirectory() as tmp, patch('native.production_profiles.registered', return_value=catalog), \
                patch('native.production_profiles.profile', return_value=('digest',family)), \
                patch('integrations.company_run.publications.collect'), patch('integrations.company_run.lake.scan', read):
            run = Path(tmp)
            (run/'env').mkdir()
            bindings = run/'env/company-sources.json'
            cfg = {'plm_source':'plm-A', 'plm':{'url':'http://primary'}, 'products':{'X':{'family':9}}}
            bindings.write_text(json.dumps(cfg))
            (run/'state.json').write_text('{"day":5}')
            self.assertEqual(scan_cycle(settings,run,run), ({'plm-A','lab-A'}, []))
            self.assertEqual(observed[0][1]['parts'], [9])
            cfg['products']['Y'] = {'family':11}
            bindings.write_text(json.dumps(cfg))
            catalog['production-SKU'] = {}
            quality_sites = set()
            sources, errors = scan_cycle(settings,run,run, qms_sources=quality_sites)
            self.assertEqual(quality_sites, {'lab-A', 'lab-B'})
            self.assertEqual([entry['parts'] for kind,entry in observed if kind=='plm'], [[9],[9,11],[42]])
            family['plm_source'] = 'plm-A'
            self.assertIn(('plm-A', 'PLM source and endpoint disagree with the primary reader'),
                          scan_cycle(settings, run, run)[1])
            config_file = run/'analytics.json'
            config_file.write_text(json.dumps(settings))
            (run/'env/planning-report.json').write_text(json.dumps(
                dict(settings=str(config_file), archive=str(run/'archive'))))
            engine = SimpleNamespace(run=run, state={'lake_scans': {'last_success_minute': {'plm-A': 1}}},
                                     persist=lambda: None, log=lambda *a, **kw: None)
            scheduled_scan(engine, 10)
            last = engine.state['lake_scans']['last_success_minute']
            self.assertEqual(last['plm-A'], 1)
            self.assertEqual(last['lab-A'], 10)
            with patch('native.production_profiles.profile', side_effect=ValueError('missing profile')):
                scheduled_scan(engine, 20)
            self.assertEqual(last['lab-A'], 10)
            self.assertEqual(engine.state['lake_scans']['unresolved_profiles'], ['production-SKU'])
            family['plm_source'] = 'plm-B'
            settings['plm']['reader']['url'] = 'http://wrong-site'
            self.assertIn(('plm-A','ValueError'), scan_cycle(settings,run,run)[1])
            # Two families on one commissioned lab share a scan, each with its own sample scope.
            settings['plm']['reader']['url'] = 'http://primary'
            settings['erp'] = dict(source='erp', reader=dict(url='http://erp', site='erp-site'))
            family.update(erp=dict(base='http://erp', site='erp-site'),
                          recurring_lab=dict(reference_user='writer', analyst_user='analyst',
                                             warehouse='Hold', company='Co', assays={'SKU': {}}))
            family['qms_reader']['site'] = 'lab-site'
            other = dict(family, item='BULK', recurring_lab=dict(family['recurring_lab'], assays={'BULK': {}}),
                         mapping={'BULK': {'erp': {'output': {'item': 'BULK'}},
                                           'mes': {'settings': {'company_id': 2}, 'output': {'item': 8}}}})
            catalog['production-BULK'] = {}
            offset = len(observed)
            with patch('native.production_profiles.profile',
                       side_effect=lambda run, name, spec: ('digest', other if name == 'production-BULK' else family)):
                _, both_errors = scan_cycle(settings, run, run)
            self.assertFalse(any(source == 'lab-B' for source, _ in both_errors))
            lab_scan, = [entry for kind, entry in observed[offset:] if kind == 'qms' and entry['source'] == 'lab-B']
            self.assertCountEqual([scope['item'] for scope in lab_scan['registered_samples']], ['SKU', 'BULK'])
        self.assertEqual(sources, {'plm-A','lab-A','plant-B','plm-B','lab-B'})
        self.assertEqual(errors, [('plant-B','OSError')])
        self.assertIn(('mes', {'reader':family['mes'],'product':7,'company':2}), observed)
        self.assertIn(('plm', {'reader':family['plm'],'parts':[42],'registered_family':42,'source':'plm-B'}), observed)
        self.assertIn(('publication', {'reader':family['plm'],'families':[42],'registered_family':42,'on':'2027-01-06','source':'plm-B'}), observed)
