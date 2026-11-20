import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapter import Lake


class LakeTest(unittest.TestCase):
    def test_failed_append_leaves_retry_cursor_at_last_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake = Lake(Path(tmp) / 'table', {})
            cursor = {}
            docs = [{'name': 'D1'}, {'name': 'D1'}]
            with patch('adapter.write_deltalake', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    lake.ingest('erp', 'Delivery Note', docs, latest=cursor)
            self.assertEqual(cursor, {})
            self.assertEqual(lake.ingest('erp', 'Delivery Note', docs, latest=cursor), 1)
            self.assertEqual(lake.ingest('erp', 'Delivery Note', docs, latest=cursor), 0)
            self.assertEqual(cursor, lake.latest_hashes('erp', 'Delivery Note'))

    def test_scoped_read_filters_shared_ids_at_requested_version(self):
        entity = "Planner's Note"
        with tempfile.TemporaryDirectory() as tmp:
            lake = Lake(Path(tmp) / 'table', {'planner': [entity]})
            lake.ingest('erp', 'Work Order', [{'name': 'one'}])
            lake.ingest('erp', entity, [{'name': 'one'}])
            self.assertEqual(lake.read('planner', entity, version=0), [])
            self.assertEqual([row['entity'] for row in lake.read('planner', entity)], [entity])

    def test_real_delta_storage(self):
        release = Path(os.environ['NORVALE_RELEASE'])
        records = json.loads((release / 'data/observable/native-records.json').read_text())
        entity = 'Delivery Note'
        docs = list(records[entity].values())[:2]
        self.assertTrue(docs)
        with tempfile.TemporaryDirectory(prefix='norvale-lake-') as tmp:
            lake = Lake(Path(tmp) / 'table', {'warehouse': [entity]})
            self.assertEqual(lake.ingest('erpnext', entity, docs, 'posting_date'), len(docs))
            self.assertEqual(lake.ingest('erpnext', entity, docs, 'posting_date'), 0)
            corrected = dict(docs[0], docstatus=2, modified='2099-01-01 00:00:00')
            self.assertEqual(lake.ingest('erpnext', entity, [corrected], 'posting_date'), 1)
            reopened = Lake(Path(tmp) / 'table', {'warehouse': [entity]})
            self.assertEqual(len(reopened.read('warehouse', entity, version=0)), len(docs))
            self.assertEqual(len(reopened.read('warehouse', entity)), len(docs) + 1)
            self.assertEqual(len(reopened.history()), 2)
            self.assertEqual(reopened.ingest('erpnext', entity, [docs[0]], 'posting_date'), 1)
            self.assertEqual(reopened.ingest('erpnext', entity, [docs[0]], 'posting_date'), 0)
            self.assertEqual(len(reopened.history()), 3)
            with self.assertRaises(PermissionError):
                reopened.read('sales', entity)
            self.assertTrue(list((Path(tmp) / 'table' / '_delta_log').glob('*.json')))


if __name__ == '__main__':
    unittest.main()
