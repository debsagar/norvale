import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from integrations.company_run.interrupted_acceptance import verified_interrupted
from native.company_erp import digest


class InterruptedAcceptance(unittest.TestCase):
    def setUp(self):
        self.reader = Mock(source_id='east')
        ref = 'east:mrp.production:1'
        self.batch = dict(mo=1, completion_ref=ref, batch='NV-WIP-' + digest(ref)[:24],
                          work_order='WO', item='SKU', uom='Nos', qty=2)
        self.evidence = dict(id='QI', version='v1', approver='qa', case='NC', batch=self.batch['batch'])
        self.fault = dict(norvale_interrupted_id=[9, 'wo'], workcenter_id=[4, 'center'],
                          norvale_disposition='qms:' + digest(self.evidence))
        self.mo = dict(id=1, state='cancel', origin='WO', product_qty=2, product_uom_id=[1, 'Unit'],
                       product_id=[3, 'SKU'], bom_id=[7, 'bom'], norvale_revision='rev',
                       move_raw_ids=[20], move_finished_ids=[21])
        self.reader.read.side_effect = lambda model, key: {
            'mrp.workcenter.productivity': self.fault, 'mrp.workorder': dict(production_id=[1, 'mo'], workcenter_id=[4, 'c']),
            'mrp.production': self.mo, 'product.product': dict(default_code='SKU')}[model]
        self.reader._rows.return_value = [dict(state='cancel')]
        self.reader._lines.return_value = []
        self.authority = Mock()
        self.authority.operator.return_value = self.reader
        self.authority.native_center.return_value = 4
        self.authority.interrupted_settings.return_value = {'units': {'Unit': 'Nos'}, 'qms_reader': {}}
        self.qms_patch = patch('integrations.company_run.interrupted_acceptance.QMS')
        self.qms = self.qms_patch.start().return_value
        self.addCleanup(self.qms_patch.stop)
        self.qms.get.return_value = dict(name='NC', custom_qms_batch=self.batch['batch'], custom_failed_inspection='QI')
        self.authority.interrupted_disposition.return_value = self.evidence
        self.revision = dict(mes_source='east', mes_ack_id='7', erp_bom='BOM')
        self.work = dict(request={'work_order': 'WO'}, equipment='east:4', revision=self.revision,
                         dispatch=dict(id='1', version='rev', source='east'), qty=5, rejected_remainder=2)
        self.case = dict(center='east:4', qa_as_of='2027-01-10', fault=dict(source_id='east', native_id=8, norvale_interrupted_id=[9, 'wo']), interrupted=self.batch,
                         disposition=dict(source_id='east', native_id=1, system='mes', entity='mrp.production'), qa_evidence=self.evidence)
        equipment = SimpleNamespace(cases={'case': self.case}, scoped_authority=lambda center: self.authority)
        self.erp = Mock()
        self.order = dict(name='WO', docstatus=1, production_item='SKU', stock_uom='Nos', bom_no='BOM', qty=5, produced_qty=0, company='C', custom_norvale_definition=json.dumps(self.revision))
        self.erp.get.side_effect = lambda typ, name: self.order if typ == 'Work Order' else self.post
        self.erp.list.return_value = []
        self.engine = SimpleNamespace(company_erp=SimpleNamespace(equipment=equipment, state={'work': {'a': self.work}}),
                                      api={'PLANNER': self.erp}, company='C')

    def test_disposition_and_native_failures(self):
        self.assertTrue(verified_interrupted(self.engine))
        for obj, key, value in [(self.case['disposition'], 'source_id', 'west'), (self.batch, 'completion_ref', 'west:mrp.production:1'),
                                (self.mo, 'state', 'done'), (self.fault, 'norvale_disposition', 'wrong'),
                                (self.order, 'bom_no', 'wrong'), (self.mo, 'norvale_revision', 'wrong')]:
            old = obj[key]; obj[key] = value
            self.assertFalse(verified_interrupted(self.engine), key)
            obj[key] = old
        self.authority.interrupted_disposition.return_value = {**self.evidence, 'version': 'v2'}
        self.assertFalse(verified_interrupted(self.engine))
        self.authority.interrupted_disposition.return_value = self.evidence
        self.qms.get.return_value['custom_failed_inspection'] = 'other'
        self.assertFalse(verified_interrupted(self.engine))
        self.qms.get.return_value['custom_failed_inspection'] = 'QI'
        self.reader._lines.return_value = [dict(state='done')]
        self.assertFalse(verified_interrupted(self.engine))
        self.reader._lines.return_value = []
        self.case.pop('disposition')
        self.assertFalse(verified_interrupted(self.engine))
        self.engine.company_erp.equipment.cases.clear()
        self.assertFalse(verified_interrupted(self.engine))

    def test_posted_sibling_and_attributed_output(self):
        pin = dict(completion_id='east:mrp.production:2', mes_source='east', version='t', dispatch_id='1',
                   revision=self.revision, output_lot_id='east:stock.lot:5')
        self.post = dict(name='P', docstatus=1, stock_entry_type='Manufacture', work_order='WO', bom_no='BOM',
                         remarks=json.dumps(pin), items=[dict(is_finished_item=1, item_code='SKU', qty=3)], fg_completed_qty=3)
        self.order['produced_qty'] = 3
        self.erp.list.return_value = [dict(name='P')]
        self.reader.completion.return_value = dict(completion_ref=pin['completion_id'], erp_work_ref='WO',
            recorded_at_utc='t', revision='rev', bom={'native_id': 7}, output={'lot': {'native_id': 5}, 'quantity': 3})
        self.assertTrue(verified_interrupted(self.engine))
        self.order['produced_qty'] = 4
        self.assertFalse(verified_interrupted(self.engine))
        self.order['produced_qty'] = 3
        self.erp.list.return_value *= 2
        self.assertFalse(verified_interrupted(self.engine))
        self.erp.list.return_value = [dict(name='P')]
        for key, value in [('completion_id', self.batch['completion_ref']), ('mes_source', 'west'),
                           ('version', 'wrong'), ('dispatch_id', '9')]:
            changed = copy.deepcopy(pin); changed[key] = value; self.post['remarks'] = json.dumps(changed)
            self.assertFalse(verified_interrupted(self.engine), key)
        self.post['remarks'] = json.dumps(pin)
        self.reader.completion.return_value['erp_work_ref'] = 'OTHER'
        self.assertFalse(verified_interrupted(self.engine))


if __name__ == '__main__':
    unittest.main()
