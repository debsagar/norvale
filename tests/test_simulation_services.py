import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.company_run.services import manage
from norvale.environment.stack import Stack


class RunServices(unittest.TestCase):
    def test_only_owned_containers_start_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            file = base/'release/integrations/mes/compose.yaml'
            file.parent.mkdir(parents=True)
            file.touch()
            (base/'services.json').write_text(json.dumps(dict(mes_project='right', plm_project='plm')))
            candidates = [
                ('db', 'right', str(file), 'False', 'db'),
                ('app', 'right', str(file), 'False', 'odoo'),
                ('wrong-run', 'right', str(file) + '.other', 'False', 'odoo'),
                ('wrong-project', 'other', str(file), 'False', 'odoo'),
                ('bootstrap', 'right', str(file), 'True', 'odoo'),
            ]
            state = 'exited'
            def docker_ps(args, *, text):
                filters = [args[i + 1] for i, x in enumerate(args) if x == '--filter']
                return '\n'.join(f'{i} {state} {p} {s}' for i, p, cfg, oneoff, s in candidates
                                 if all(f in (f'label=com.docker.compose.project={p}',
                                              f'label=com.docker.compose.project.config_files={cfg}',
                                              f'label=com.docker.compose.oneoff={oneoff}') for f in filters))
            with patch('subprocess.check_output', side_effect=docker_ps), patch('subprocess.run') as mutate:
                self.assertEqual([r['id'] for r in manage(base, 'status')], ['db','app'])
                mutate.assert_not_called()
                manage(base, 'start')
                state = 'running'
                manage(base, 'stop')
                self.assertEqual([call.args[0] for call in mutate.call_args_list], [
                    ['docker','start','db'], ['docker','start','app'],
                    ['docker','stop','app'], ['docker','stop','db']])

    def test_commissioned_erp_endpoint_remains_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack = Stack(Path(tmp))
            stack.env.mkdir()
            stack.file.write_text(json.dumps(dict(services=dict(frontend=dict(ports=['127.0.0.1:12345:8080'])))))
            for address in ('172.30.0.3', '172.30.0.9'):
                with patch('norvale.environment.docker.container_ip', return_value=address):
                    self.assertEqual(stack.endpoints()['erp'],'http://127.0.0.1:12345')
