"""Offline acceptance test for extending an existing incremental run."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import generate
from test_engine_goals import Native


class IncrementalExtension(unittest.TestCase):
    def test_extension_is_persisted_and_replayed_without_changing_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = root / 'plan.json'
            run = root / 'run'
            extension = root / 'extension.json'
            plan.write_text(json.dumps({'consumer': 'norvale', 'seed': 41,
                                        'config': {}, 'events': []}))
            extension.write_text(json.dumps({
                'id': 'additional-orders', 'systems': {}, 'roles': {}, 'products': {},
                'events': [{'id': 'new-order', 'kind': 'order', 'at': 10, 'role': 'sales',
                            'payload': {'case': 'PO-1', 'quantity': 2, 'rate': 3}}]}))

            def factory(namespace, plan):
                return Native(root / (namespace + '.json'))

            def call(*args):
                output = io.StringIO()
                with redirect_stdout(output):
                    generate.main(['incremental', '--run', str(run), '--until', '10', *args],
                                  transport_factory=factory)
                return json.loads(output.getvalue())

            call('--plan', str(plan))
            manifest = (run / 'incremental.json').read_bytes()
            first = call('--extend', str(extension))
            self.assertEqual(len(first['records']), 1)
            self.assertEqual(first, call('--extend', str(extension)))
            self.assertEqual((run / 'incremental.json').read_bytes(), manifest)
