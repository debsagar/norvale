from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from integrations.lake.erp_scope import ERP_ANALYTICS_ROLES
from native.production_profiles import ADAPTER, profile
from .configure import configure
from .lake import registered_mes_scan, scan, scan_cycle


INSTANCE = 'a' * 64
EXECUTION = dict(url='https://odoo.test', db='mes', source_id='plant',
                 login='planner', password='planner-secret')
READER = dict(EXECUTION, login='analytics', password='separate-reader-secret')
PRIMARY = dict(reader=READER, instance=INSTANCE, reader_company=1)


class MesAnalyticsTests(TestCase):
    def test_three_collectors_share_the_attested_account_and_fail_before_ingesting(self):
        with (patch('integrations.mes.client.Mes') as mes,
              patch('integrations.lake.mes_reader.scan') as completion,
              patch('integrations.lake.mes_reader.scan_labor') as labor,
              patch('integrations.lake.mes_reader.scan_staging') as staging):
            client = mes.return_value
            client.call.return_value = 'b' * 64
            with self.assertRaises(PermissionError):
                scan('mes', PRIMARY, 'archive')
            mes.assert_called_once_with(**READER)
            client.call.assert_called_once_with('mrp.production', 'norvale_analytics_identity', 1)
            for collect in (completion, labor, staging):
                collect.assert_not_called()

            client.call.return_value = INSTANCE
            scoped = dict(PRIMARY, product=2, company=1)
            scan('mes', scoped, 'archive')
            for collect in (completion, labor, staging):
                collect.assert_called_once_with('archive', client, product=2, company=1)

    def test_registered_selection_keeps_legacy_and_rejects_commissioned_drift(self):
        self.assertEqual(registered_mes_scan({'mes': EXECUTION}, 2, 1, None)['reader'], EXECUTION)
        self.assertEqual(registered_mes_scan({'mes': EXECUTION}, 2, 1, PRIMARY)['reader'], READER)
        cfg = dict(mes=EXECUTION, mes_reader=READER, mes_reader_instance=INSTANCE)
        self.assertEqual(registered_mes_scan(cfg, 2, 1, None), dict(PRIMARY, product=2, company=1))
        for bad in (dict(cfg, mes_reader_instance='b' * 64),
                    dict(cfg, mes=dict(EXECUTION, url='https://wrong.test'))):
            with self.assertRaises(ValueError):
                registered_mes_scan(bad, 2, 1, PRIMARY)
        with self.assertRaises(ValueError):
            registered_mes_scan(cfg, 2, 2, PRIMARY)

    def test_primary_binding_cannot_be_downgraded_to_legacy_reader(self):
        config = dict(mes={'reader': EXECUTION}, plm={'source': 'plm', 'reader': {}},
                      qms={'source': 'qms', 'reader': {'url': 'http://qms'}, 'entities': []})
        with (TemporaryDirectory() as tmp, patch('integrations.company_run.lake.scan') as collect,
              patch('integrations.company_run.publications.collect'),
              patch('integrations.company_run.publications.publication_source')):
            run = Path(tmp)
            (run/'env').mkdir()
            sources = run/'env/company-sources.json'
            sources.write_text(json.dumps(dict(mes_analytics=PRIMARY, products={}, mes=EXECUTION, company_id=1)))
            _, errors = scan_cycle(config, run/'archive', run, business_day=1)
            self.assertIn(('plant', 'Commissioned MES analytics binding changed'), errors)
            self.assertEqual({call.args[0] for call in collect.call_args_list}, {'qms', 'plm'})

            config['mes'] = PRIMARY
            _, errors = scan_cycle(config, run/'archive', run, business_day=1)
            self.assertEqual(errors, [])
            collect.assert_any_call('mes', PRIMARY, run/'archive')
            collect.reset_mock()
            sources.write_text(json.dumps(dict(mes_analytics=PRIMARY, products={}, company_id=1,
                                               mes=dict(EXECUTION, source_id='wrong-plant'))))
            _, errors = scan_cycle(config, run/'archive', run, business_day=1)
            self.assertIn(('plant', 'Commissioned MES analytics binding changed'), errors)
            self.assertEqual({call.args[0] for call in collect.call_args_list}, {'qms', 'plm'})

    def test_registry_pins_opt_in_and_requires_separate_secret(self):
        writer = {k: value for k, value in EXECUTION.items() if k != 'password'}
        cfg = dict(adapter=ADAPTER, item='bulk', family=1, mapping={'bulk': {}}, source='plant',
                   hold_location_id=1, plm=dict(url='http://plm', publisher='publisher', token_env='PLM'),
                   erp=dict(base='http://erp', site='erp', token_env='ERP'),
                   mes=dict(writer, password_env='WRITER'),
                   qms_reader=dict(url='http://qms', site='qms', api_key_env='KEY', api_secret_env='QMS'),
                   qms_approver='approver', specifications={'bulk': 'spec'}, units={'kg': 'kg'},
                   material_kind='intermediate', stock_step=.01)
        spec = dict(connection='plant')
        with (TemporaryDirectory() as tmp, patch('native.production_profiles.host_profile', return_value=cfg),
              patch.dict(os.environ, dict(PLM='plm', ERP='erp', WRITER=EXECUTION['password'],
                                         KEY='key', QMS='secret', READER=READER['password']), clear=True)):
            run = Path(tmp)
            (run/'env').mkdir()
            (run/'env/endpoints.json').write_text(json.dumps({'erp': 'http://erp', 'site': 'erp'}))
            _, old = profile(run, 'production-bulk', spec)
            self.assertNotIn('mes_reader', old)
            cfg['mes_reader'] = dict(login='analytics', password_env='READER', instance=INSTANCE)
            spec['target_hash'] = sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
            _, commissioned = profile(run, 'production-bulk', spec)
            self.assertEqual(commissioned['mes_reader'], READER)
            self.assertEqual(commissioned['mes_reader_instance'], INSTANCE)
            with patch.dict(os.environ, READER=EXECUTION['password']):
                with self.assertRaisesRegex(ValueError, 'separate'):
                    profile(run, 'production-bulk', spec)
            del os.environ['READER']
            with self.assertRaisesRegex(ValueError, 'available'):
                profile(run, 'production-bulk', spec)
            cfg.pop('mes_reader')
            with self.assertRaisesRegex(ValueError, 'changed'):
                profile(run, 'production-bulk', spec)

    def test_fresh_configuration_verifies_reader_before_other_native_calls(self):
        with (TemporaryDirectory() as tmp, patch('integrations.company_run.configure.Engine') as engine,
              patch('integrations.company_run.configure.Mes') as mes,
              patch('integrations.company_run.configure.ERPNext') as erp):
            root = Path(tmp)
            engine.return_value = SimpleNamespace(run=root/'business', admin=object())
            (root/'services.json').write_text(json.dumps(dict(mes_url=READER['url'], mes_project='plant')))
            fixture = root/'mes-fixture.json'
            fixture.write_text('{}')
            (root/'mes-password').write_text(EXECUTION['password'])
            with self.assertRaisesRegex(ValueError, 'Fresh'):
                configure(root)
            fixture.write_text(json.dumps(dict(analytics_instance=INSTANCE, analytics_company=1)))
            (root/'mes-analytics-password').write_text(READER['password'])
            mes.return_value.call.return_value = 'b' * 64
            with self.assertRaises(PermissionError):
                configure(root)
            mes.assert_called_once_with(**dict(READER, db='norvale_mes'))
            erp.assert_not_called()
            self.assertFalse((root/'analytics.json').exists())

    def test_configure_persists_fresh_binding_and_reopens_old_settings(self):
        with (TemporaryDirectory() as tmp, patch('integrations.company_run.configure.Engine') as engine,
              patch('integrations.company_run.configure.Mes') as mes,
              patch('integrations.company_run.configure.ERPNext'),
              patch('integrations.company_run.configure.PLM') as plm):
            root = Path(tmp)
            run = root/'business'
            (run/'env').mkdir(parents=True)
            engine.return_value = SimpleNamespace(run=run, admin=object(), config={},
                                                  eps={'erp': 'http://erp', 'site': 'erp'},
                                                  tokens=dict.fromkeys(('QA', 'PLANNER', *ERP_ANALYTICS_ROLES.values()),
                                                                       'key:secret'))
            (root/'services.json').write_text(json.dumps(dict(mes_url=READER['url'], mes_project='plant',
                                                              plm_url='http://plm')))
            (root/'mes-password').write_text(EXECUTION['password'])
            for role in ('publisher', 'reader'):
                (root/role).write_text(role)
            (root/'qms').mkdir()
            (root/'qms/url').write_text('http://qms')
            (root/'qms/users.json').write_text(json.dumps(dict.fromkeys(
                ('reader', 'dispatcher', 'approver', 'analyst'),
                dict(api_key='key', api_secret='secret', user='person'))))
            (root/'qms/fixture.json').write_text(json.dumps(dict(procedure='test', parameter='check')))
            plm.return_value.request.return_value = {'token': 'plm'}
            def reply(model, method, *args, **kwargs):
                return {
                    'norvale_analytics_identity': INSTANCE,
                    'search_read': [dict(uom_id=[1], company_id=[1])],
                    'read': [dict(rounding=.000001)], 'precision_get': 8,
                }[method]
            mes.return_value.call.side_effect = reply
            settings = root/'analytics.json'
            sources = run/'env/company-sources.json'
            fixture = root/'mes-fixture.json'
            legacy_fixture = dict(stock=1, centers=[1], hold=2)
            fixture.write_text(json.dumps(legacy_fixture))
            legacy_mes = dict(reader=dict(EXECUTION, db='norvale_mes', login='operator'))
            settings.write_text(json.dumps(dict(mes=legacy_mes)))
            configure(root)
            self.assertEqual(json.loads(settings.read_text())['mes'], legacy_mes)
            self.assertNotIn('mes_analytics', json.loads(sources.read_text()))
            mes.assert_called_once_with(**dict(EXECUTION, db='norvale_mes'))

            settings.unlink()
            sources.unlink()
            fixture.write_text(json.dumps(dict(legacy_fixture, analytics_instance=INSTANCE,
                                               analytics_company=1)))
            (root/'mes-analytics-password').write_text(READER['password'])
            configure(root)
            expected = dict(PRIMARY, reader=dict(READER, db='norvale_mes'))
            self.assertEqual(json.loads(settings.read_text())['mes'], expected)
            self.assertEqual(json.loads(sources.read_text())['mes_analytics'], expected)
            configure(root)
            self.assertEqual(json.loads(settings.read_text())['mes'], expected)
