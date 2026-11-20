import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from adapter import Lake
from boundary import trace
from integrations.plm import PLM
from plm_reader import scan_publications


class ChangeHistoryTests(TestCase):
    def test_publication_scan_preserves_only_observed_receipt_fields(self):
        client = Mock()
        marker = "a" * 64
        approval = dict(actor="engineer", evidence="approved", on="2027-01-01",
                        effective="2027-01-01", sha256="version")
        client.export.return_value = [dict(native_id=1, source_version="version",
            definition=dict(pk=1, name="S", revision="A", revision_of=None, lines=[]),
            approval=approval, review=approval,
            acknowledgments={"mes": dict(native_id="1", plm_native_id=1, plm_source_version="version",
                                         on="2027-01-01", receiver_marker=marker, private="omit")})]
        client.change_events.return_value = []
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(scan_publications(root, "plm", client, [1], "2027-01-01"), 1)
            row, = Lake(root / "delta", {})._rows()
            receipt = json.loads(row["payload"])["acknowledgments"]["mes"]
            self.assertEqual(receipt["receiver_marker"], marker)
            self.assertNotIn("private", receipt)


    def test_rejection_survives_reopen_without_creating_a_publication(self):
        client = PLM('http://localhost', 'fixture', publisher='planner')
        client._family = Mock(return_value=[{'pk': 9, 'revision': 'C'}])
        client.export = Mock(return_value=[])
        client._meta = Mock(return_value={
            'norvale_proposal': dict(doctype='BOM', id='BOM-1', version='v3', on='2027-01-01',
                                     base_id=4, base_version='v2'),
            'norvale_review': dict(actor='engineer', evidence='review', on='2027-01-02', sha256='hash'),
            'norvale_rejection': dict(actor='approver', evidence='test failed', on='2027-01-03', sha256='hash'),
            'internal': 'hidden'})
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for date, count in [('2027-01-01', 1), ('2027-01-02', 1), ('2027-01-03', 1), ('2027-01-03', 0)]:
                self.assertEqual(scan_publications(root, 'plm', client, iter([4]), date), count)
            lake = Lake(root/'delta', {'engineer': ['InvenTree Change', 'InvenTree Publication']})
            rows = lake.read('engineer', 'InvenTree Change')
            self.assertEqual({r['effective_at'] for r in rows}, {'2027-01-01', '2027-01-02', '2027-01-03'})
            docs = [json.loads(r['payload']) for r in rows]
            self.assertEqual({d['stage'] for d in docs}, {'proposal', 'review', 'rejection'})
            self.assertTrue(all(d['proposal']['version'] == 'v3' and 'internal' not in d for d in docs))
            self.assertIn(('InvenTree Change','9:rejection','InvenTree Part','9'),
                          map(tuple, trace({'InvenTree Change': docs})))
            self.assertEqual(lake.read('engineer', 'InvenTree Publication'), [])
