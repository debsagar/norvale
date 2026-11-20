"""Offline checks for primary production policy and existing work."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from integrations.company_run.operating import Operating
from integrations.company_run.production_batch import queue_operation
from integrations.company_run.timing import queue_dispatches


class PrimaryBatch(TestCase):
    def test_policy_and_existing_event(self):
        with TemporaryDirectory() as root:
            run = Path(root)
            (run / 'env').mkdir()
            settings = run / 'env/company-sources.json'
            settings.write_text(json.dumps({'production_batch': {'target': 2}}))
            w = dict(key='w', item='item', uom='Nos', qty=4, inputs=[], equipment=1,
                     revision={}, request={'key': 'r'}, dispatch={'id': '1', 'source': 'primary'})
            scheduled = []
            clock = SimpleNamespace(now=0, has_event=lambda key: False)
            flow = SimpleNamespace(state={'work': {'w': w}}, attempt=lambda key, role, f: f())
            e = SimpleNamespace(run=run, additions=clock, company_erp=flow,
                                config={'capacity_units': 4}, persist=lambda: None)
            actor = Operating(e)
            actor.scheduled = lambda work: actor
            with patch('integrations.company_run.timing.schedule', side_effect=lambda *a, **k: scheduled.append(a)):
                queue_dispatches(e, actor)
                self.assertEqual(w['production_operations']['1']['production_target'], 2)
                self.assertEqual(scheduled[0][1][2], '1')
                # Existing operation survives removal of the policy after restart.
                actor.cfg.pop('production_batch')
                self.assertEqual(queue_operation(e, w, actor), w['dispatch'])
                w.pop('production_operations')
                clock.has_event = lambda key: True
                actor.cfg['production_batch'] = {'target': 2}
                queue_dispatches(e, actor)
                self.assertNotIn('production_operations', w)
            for invalid in ({}, {'target': True}, {'target': 0}, {'target': 1, 'backorder_not_before': -1}):
                settings.write_text(json.dumps({'production_batch': invalid}))
                with self.assertRaises(ValueError):
                    Operating(e)
