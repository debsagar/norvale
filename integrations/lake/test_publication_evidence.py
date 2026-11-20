"""Regression checks against the retained, read-only shipment archive."""
import copy
import json
import os
import unittest
from pathlib import Path
from adapter import Lake
from genealogy import ShipmentHistory

PATH = os.environ.get('NORVALE_ORDINARY_DELTA')
MANIFEST = os.environ.get('NORVALE_SHIPMENT_PROOF')
DELIVERY = 'MAT-DN-2027-00001'

@unittest.skipUnless(PATH or MANIFEST, 'retained Delta archive unavailable')
class PublicationEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if MANIFEST:
            proof = json.loads(Path(MANIFEST).read_text())
            cls.erp, = {row['shipment'][0] for row in proof['shipments']}
            cls.rows = []
            selected = set()
            for path, archive in proof['archives'].items():
                sources = set(archive['sources'])
                if sources & selected:
                    raise ValueError('Overlapping source archives')
                selected.update(sources)
                cls.rows.extend(row for row in Lake(path, {})._rows(archive['delta_version'])
                                if row['source'] in sources)
        else:
            cls.erp = 'company-ordinary.zlZ3K6-erp'
            cls.rows = Lake(PATH, {})._rows()

    def setUp(self):
        self.rows = copy.deepcopy(self.rows)
        self.trace = lambda: ShipmentHistory(self.rows, self.erp).trace(DELIVERY)
        self.pub = next(r for r in self.rows if r['entity'] == 'InvenTree Publication' and r['native_id'] == '1')
        self.work = next(r for r in self.rows if r['entity'] == 'Work Order' and r['native_id'] == 'MFG-WO-2027-00001')

    def change(self, row, edit):
        value = json.loads(row['payload']); edit(value); row['payload'] = json.dumps(value)

    def unresolved(self):
        result = self.trace()
        self.assertTrue(any(reason == 'PLM revision' for _, reason in result['unresolved']))
        self.assertNotIn('publication', result['lots'][0])
        self.assertTrue(result['stock_ledger'])

    def test_actual_match_and_ledger(self):
        result = self.trace()
        self.assertEqual(result['unresolved'], [])
        self.assertEqual(result['lots'][0]['publication'][2], '1')
        self.assertTrue(result['stock_ledger'])

    def test_mismatched_receiver(self):
        self.change(self.pub, lambda p: p['acknowledgments']['mes'].update(native_id='wrong'))
        self.unresolved()

    def test_late_activation(self):
        self.change(self.pub, lambda p: p.update(effective_on='2027-02-01'))
        self.unresolved()

    def test_superseded_future_revision(self):
        self.change(self.work, lambda w: w['definition'].update(id='10', version=next(json.loads(r['payload'])['version'] for r in self.rows if r['entity']=='InvenTree Publication' and r['native_id']=='10')))
        self.unresolved()

    def test_early_pre_ack_observation_does_not_disqualify(self):
        early = copy.deepcopy(self.pub)
        self.change(early, lambda p: p.update(acknowledgments={}))
        early['ingested_at'] = '0000'
        self.rows.append(early)
        self.assertEqual(self.trace()['unresolved'], [])

    def test_late_acknowledgment(self):
        self.change(self.pub, lambda p: p['acknowledgments']['erp'].update(on='2027-02-01'))
        self.unresolved()

if __name__ == '__main__':
    unittest.main()
