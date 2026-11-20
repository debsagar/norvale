"""Regression coverage of ordinary generator selection and the quality handoffs."""
import os
from pathlib import Path
from unittest.mock import Mock
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import generate
from integrations.company_run.operating import Operating, proposed_salvage
from integrations.company_run.timing import operating_quality
from native.company_erp import Pending


def bind_legacy(actor, lot):
    lot.setdefault('source_type', 'Stock Entry')
    lot.setdefault('source_id', 'ERP-1')
    lot['source_row'] = 'ERP-row'
    actor.e.company = 'ERP Co'
    connection = dict(base='https://erp.test', site='erp.test')
    erp = SimpleNamespace(**connection)
    erp.metadata = Mock(return_value={'fields': [dict(fieldname='custom_norvale_receiver_marker',
                                                       read_only=1, default='12345678123442348123123456781234')]})
    erp.get = Mock(return_value=dict(doctype=lot['source_type'], name=lot['source_id'],
                   company=actor.e.company, docstatus=1, items=[
                       dict(name=lot['source_row'], batch_no=lot['batch'], item_code=lot['item'],
                            qty=lot['qty'], stock_uom=lot['uom'], t_warehouse='ERP Hold')]))
    actor.e.api = {role: erp for role in ('BUYER', 'WAREHOUSE', 'PLANNER')}
    cfg = actor.cfg
    cfg.update(erp=connection, qms_reader=dict(url='https://qms.test'))
    cfg['lab'].update(analyst=cfg['qms_reader'], approver=cfg['qms_reader'], references={lot['batch']: 'QREF'})
    return dict(name='QREF', company='Norvale QMS', stock_entry_type='Material Receipt', docstatus=0,
                owner='Administrator', remarks=f"ERP {lot['source_type']} {lot['source_id']} batch {lot['batch']}",
                items=[dict(name='ROW', batch_no=lot['batch'], item_code=lot['item'], qty=lot['qty'],
                            stock_uom=lot['uom'], t_warehouse='QMS Quarantine - NQ')])


class OperatingTest(TestCase):
    def test_normal_entry_opts_in_at_provisioning(self):
        original_mask = os.umask(0o077)
        try:
            with TemporaryDirectory() as tmp, patch('company.initialize') as initialize:
                generate.main(['--run', tmp, '--provision', '--company-operating', '--days', '8'])
        finally:
            os.umask(original_mask)
        path, config = initialize.call_args.args
        self.assertEqual(path, Path(tmp))
        self.assertTrue(config['company_operating'])
        self.assertTrue(config['supplier_quality'])
        self.assertIn('LAB', config['company_timing']['shifts'])

    def test_repeatable_measurement_uses_confirmed_lot_genealogy_and_rework(self):
        actor = Operating.__new__(Operating)
        raw = dict(batch='INPUT', item='SURFACTANT', kind='incoming', source_type='Stock Entry', source_id='S')
        source = dict(batch='OUTPUT', item='SHAMPOO', kind='finished', completion_id='mes:mrp.production:4')
        output = dict(source, batch='REWORKED', rework_key='r', completion_id='mes:mrp.production:5')
        report = dict(id=source['completion_id'], inputs=[dict(batch='INPUT', qty=.2, uom='Kg')])
        rework = dict(output_batch='REWORKED', request={'batch': 'OUTPUT'}, execution=output['completion_id'])
        physical = dict(lots={'INPUT':raw, 'OUTPUT':source}, rework={'r':rework},
                        completions={'c':dict(done=True, work='work-0', report=report)})
        actor.e = SimpleNamespace(config={'seed':42}, company_erp=SimpleNamespace(state=physical),
                                  docs={'opening':dict(doctype='Stock Entry', name='S')})
        value = actor.measurement(source)
        self.assertEqual(actor.measurement(source), value)
        self.assertEqual(actor.measurement(dict(source, batch='OTHER-DEPLOYMENT-ID')), value)
        report['inputs'][0]['qty'] = .4
        self.assertNotEqual(actor.measurement(source), value)
        with self.assertRaises(Pending):
            actor.measurement(output)
        rework.update(receipt='STE-2', retest='QMS-2')
        self.assertLess(actor.measurement(output), actor.measurement(source))
        self.assertEqual(proposed_salvage(dict(source, qty=12, uom='Nos'), 6.35), 11)
        self.assertFalse(proposed_salvage(dict(output, qty=11, uom='Nos'), 6.35))
        self.assertFalse(proposed_salvage(dict(source, qty=12, uom='Nos'), 6.8))
        actor.e.config['seed'] = 72
        report['inputs'][0]['qty'] = .2
        self.assertEqual(proposed_salvage(dict(source, qty=12, uom='Nos'), actor.measurement(source)), 11)
        self.assertGreater(actor.measurement(output), 6)
        self.assertFalse(proposed_salvage(dict(output, qty=11, uom='Nos'), actor.measurement(output)))

    def test_inbound_measurement_can_reject_a_supplier_receipt(self):
        lot = dict(batch='B', kind='incoming', item='NACL', qty=12, uom='Kg', supplier='Supplier A',
                   received_on='2027-01-12', receipt_index=0, source_type='Purchase Receipt', source_id='PR')
        actor = Operating.__new__(Operating)
        actor.e = SimpleNamespace(config=dict(seed=14, supplier_quality=True), date=lambda: '2027-01-13',
            dated=lambda day: '2027-01-01', docs={'receipt-1':dict(doctype='Purchase Receipt', name='PR')},
            company_erp=SimpleNamespace(state={'lots':{'B':lot}}), state={}, persist=lambda: None)
        actor.cfg = dict(specifications={'NACL':'spec'}, lab=dict(analyst_user='analyst', analyst={},
                         approver={}, references={'B':'QREF'}, procedure='SOP'), qms_approver='qa')
        receipt = bind_legacy(actor, lot)
        # Pre-commissioning sites have no receiver marker in their BOM schema.
        actor.e.api['PLANNER'].metadata.side_effect = AssertionError('No historical site marker')
        value = actor.measurement(lot)
        self.assertGreater(value, 6)
        self.assertEqual(actor.measurement(dict(lot, batch='fresh-native-id')), value)
        with patch('integrations.company_run.operating.QMS') as client:
            qa = client.return_value
            doc = {}
            def create(entity, key, **fields):
                if entity == 'Quality Inspection':
                    doc.update(fields, name='QI', owner='analyst', custom_qms_operation=key,
                               docstatus=1, status='Rejected')
                    doc['readings'][0]['status'] = 'Rejected'
                    return doc
                return {'name':entity}
            qa.create.side_effect = create
            qa.get.side_effect = lambda kind, name: receipt if kind == 'Stock Entry' else doc
            qa.list.side_effect = lambda kind, *args, **kw: (
                ([dict(name='QI', custom_qms_operation='B:operating-initial')] if doc else [])
                if kind == 'Quality Inspection' else [dict(name='NC')])
            def transition(*args):
                doc.update(modified_by='qa', workflow_state='Lot Rejected')
                return doc
            qa.transition.side_effect = transition
            qa.request.return_value = {'message':'qa'}
            with self.assertRaisesRegex(Pending, 'commissioned lab identity'):
                actor.record('B', ['2027-01-13', value])
            qa.create.assert_not_called()
            qa.request.return_value = {'message':'analyst'}
            with self.assertRaisesRegex(Pending, 'dated'):
                actor.record('B', ['2027-02-01', value])
            qa.create.assert_not_called()
            recorded = actor.record('B', ['2027-01-13',value])
            self.assertEqual(recorded, dict(inspection='QI', sample={'date': '2027-01-13', 'value': value}))
            self.assertEqual([c.args[0] for c in qa.create.call_args_list],
                             ['Quality Inspection','Non Conformance','Quality Action'])
            with self.assertRaisesRegex(Pending, 'commissioned lab identity'):
                actor.review('B','QI')
            qa.transition.assert_not_called()
            qa.request.return_value = {'message':'qa'}
            for changed in ({'date': '2027-01-12'}, {'value': 5.5}):
                stale = dict(recorded, sample={**recorded['sample'], **changed})
                with self.assertRaisesRegex(Pending, 'scheduled result'):
                    actor.review('B', stale)
                qa.transition.assert_not_called()
            self.assertEqual(actor.review('B',recorded), 'Lot Rejected')
            qa.transition.assert_called_once_with('QI','Reject Lot')
        actor.e.config.pop('supplier_quality')
        self.assertLess(actor.measurement(lot), 6)

    def test_bulk_sample_follows_its_own_source_and_version(self):
        actor = Operating.__new__(Operating)
        actor.cfg = {'lab': {'parameter': 'pH'}}
        bulk = dict(batch='B', item='BULK', kind='intermediate', completion_id='1',
                    completion_version='v1', revision={'mes_source':'east'})
        raw = dict(batch='R', item='RAW', kind='incoming', source_type='Purchase Receipt', source_id='PR')
        report = dict(id='1', source='east', version='v1', inputs=[dict(batch='R', qty=.75, uom='Kg')])
        state = dict(lots={'R':raw}, completions={
            'west':dict(done=True, work='west-work', report=dict(report, source='west')),
            'east':dict(done=True, work='east-work', report=report)})
        actor.e = SimpleNamespace(config={'seed':42}, company_erp=SimpleNamespace(state=state),
                                  docs={'receipt':dict(doctype='Purchase Receipt', name='PR')})
        self.assertEqual(actor.parameter(bulk), 'pH')
        self.assertEqual(actor.identity(bulk), ['east-work', 'BULK'])
        primary = dict(bulk, revision={}, mes_lot_id='east:stock.lot:7')
        self.assertEqual(actor.identity(primary), ['east-work', 'BULK'])
        first = actor.measurement(bulk)
        report['inputs'][0]['qty'] = .45
        self.assertNotEqual(first, actor.measurement(bulk))
        with self.assertRaises(Pending):
            actor.measurement(dict(bulk, completion_version='unknown'))

    def test_qa_checks_submitted_failure_and_full_scope_before_authorizing(self):
        actor = Operating.__new__(Operating)
        lot = dict(batch='B', kind='finished', item='SHAMPOO', qty=12, uom='Nos',
                   revision={'inspection_spec':'REV'}, completion_id='mes:mrp.production:4')
        actor.e = SimpleNamespace(date=lambda: '2027-01-02',
                                  company_erp=SimpleNamespace(state={'lots':{'B':lot}}))
        actor.cfg = dict(lab=dict(approver={}, analyst_user='lab', parameter='pH'),
                         rework_mes={'source_id':'mes'}, qms_approver='qa')
        receipt = bind_legacy(actor, lot)
        failure = dict(name='QI', owner='lab', custom_qms_operation='B:operating-initial',
                       child_row_reference='ROW', reference_type='Stock Entry', reference_name='QREF', docstatus=1, status='Rejected', batch_no='B',
                       item_code='SHAMPOO', custom_spec_revision='REV', custom_lot_quantity=12,
                       custom_lot_uom='Nos', report_date='2027-01-02', inspected_by='lab',
                       custom_rework_input_batch='B', custom_rework_salvage_quantity=11,
                       readings=[dict(specification='pH', numeric=1, min_value=5, max_value=6,
                                      reading_1='6.35', status='Rejected')])
        with patch('integrations.company_run.operating.QMS') as client:
            qa = client.return_value
            qa.request.return_value = {'message':'qa'}
            qa.get.side_effect = lambda kind, name: receipt if kind == 'Stock Entry' else failure
            def transition(*args):
                failure.update(qa.transition.return_value)
                return failure
            qa.transition.side_effect = transition
            qa.list.side_effect = lambda doctype, *args, **kwargs: [
                dict(name='QI' if doctype == 'Quality Inspection' else 'NC', creation='now')]
            qa.transition.return_value = dict(failure, workflow_state='Rework Authorized', modified_by='qa')
            failure['custom_rework_salvage_quantity'] = 12
            with self.assertRaises(Pending):
                actor.review('B', 'QI')
            qa.transition.assert_not_called()
            failure['custom_rework_salvage_quantity'] = 11
            self.assertEqual(actor.review('B', 'QI'), 'Rework Authorized')
            qa.transition.assert_called_once_with('QI', 'Authorize Rework')
            failure.update(status='Accepted', readings=[dict(failure['readings'][0], reading_1='5.5')])
            qa.transition.reset_mock()
            with self.assertRaisesRegex(Pending, 'reading status'):
                actor.review('B', 'QI')
            qa.transition.assert_not_called()
            failure['readings'][0]['status'] = 'Accepted'
            qa.transition.return_value = dict(failure, workflow_state='Released', modified_by='other')
            with self.assertRaisesRegex(Pending, 'approver'):
                actor.review('B', 'QI')
            qa.transition.return_value = dict(failure, workflow_state='Released', modified_by='qa')
            self.assertEqual(actor.review('B', 'QI'), 'Released')

    def test_retest_review_retry_keeps_the_recorded_sample_and_salvage_identity(self):
        actor = Operating.__new__(Operating)
        lot = dict(batch='SALVAGE', kind='finished', item='S', qty=3, uom='Nos',
                   revision={'inspection_spec': 'R'}, rework_key='work')
        work = dict(retest='RT', execution='MES-1', authority=dict(batch='ORIGINAL', inspection='FAIL'))
        actor.e = SimpleNamespace(date=lambda: '2027-01-04', company_erp=SimpleNamespace(
            state={'lots': {'SALVAGE': lot}, 'rework': {'work': work}}))
        actor.cfg = dict(qms_approver='qa', qms_reader={'url':'https://primary'},
                         lab=dict(analyst_user='lab', parameter='pH', approver={'url':'https://other'}))
        doc = dict(name='RT', docstatus=1, status='Accepted', inspected_by='lab', report_date='2027-01-02',
                   custom_retest_of='FAIL', custom_rework_execution='MES-1', custom_rework_output_batch='SALVAGE',
                   batch_no='ORIGINAL', item_code='S', custom_lot_quantity=3, custom_lot_uom='Nos',
                   custom_spec_revision='R', readings=[dict(specification='pH', numeric=1, min_value=5,
                   max_value=6, status='Accepted', reading_1='5.80')])
        result = dict(inspection='RT', sample=dict(date='2027-01-02', value=5.6))
        with patch('integrations.company_run.operating.QMS') as client:
            qa = client.return_value
            qa.request.return_value = {'message': 'qa'}
            qa.get.return_value = doc
            qa.list.return_value = [dict(name='RT', creation='t')]
            with self.assertRaisesRegex(Pending, 'original QMS site'):
                actor.review('SALVAGE', result)
            qa.get.assert_not_called()
            actor.cfg['lab']['approver']['url'] = 'https://primary'
            with self.assertRaisesRegex(Pending, 'scheduled result'):
                actor.review('SALVAGE', result)
            with self.assertRaisesRegex(Pending, 'linked to the physical rework'):
                actor.review('SALVAGE', dict(result, inspection='OTHER'))
            qa.transition.assert_not_called()
            doc['readings'][0]['reading_1'] = '5.60'
            qa.transition.return_value = dict(doc, modified_by='qa', workflow_state='Released')
            self.assertEqual(actor.review('SALVAGE', result), 'Released')

    def test_timed_out_sample_defers_only_that_lot(self):
        from native.company_erp import CompanyERP
        from unittest.mock import Mock
        company = SimpleNamespace(config={'company_timing':{'lab_minutes':90}}, state={}, day=1,
                                  persist=Mock(), log=Mock())
        company.company_erp = CompanyERP(company, None)
        clock = SimpleNamespace(now=1800)
        actor = Mock()
        actor.sample.side_effect = [TimeoutError('laboratory unavailable'), ['2027-01-02',5.5]]
        jobs = [['lab',lot,None] for lot in ('A','B')]
        with patch('integrations.company_run.timing.schedule') as schedule:
            for job in jobs:
                operating_quality(company,clock,actor,job,'sample',0,None)
        first, second = schedule.call_args_list
        self.assertEqual((first.args[1:3],first.kwargs['attempt']), ((jobs[0],'sample'),1))
        self.assertEqual(second.args[1:3], (jobs[1],'incubate'))
        self.assertIn('operating-sample:A',company.company_erp.state['pending'])

    def test_review_deadline_creates_planner_task_only_for_late_lot(self):
        from unittest.mock import Mock
        job = ['lab', 'LOT', None]
        flow = SimpleNamespace(state={'lots':{'LOT':dict(source_type='Stock Entry',source_id='STE')}},
                               attempt=lambda key,role,fn:fn())
        clock = SimpleNamespace(now=3000,state={'quality_reviewed':{'LOT':2000}})
        planner = Mock()
        planner.get.return_value = {'name':'STE','docstatus':1}
        company = SimpleNamespace(company_erp=flow,config={'company_timing':{}}, dated=lambda day: '2027-01-02',
                                  api={'PLANNER':planner},write=Mock(side_effect=lambda k,t,d,r:dict(name='TASK',**d)))
        operating_quality(company,clock,None,job,'deadline',0,2000)
        planner.get.assert_not_called()
        clock.state['quality_reviewed']['LOT'] = 2001
        operating_quality(company,clock,None,job,'deadline',1,2000)
        planner.get.assert_called_once_with('Stock Entry','STE')
        key, kind, fields, role = company.write.call_args.args
        self.assertEqual((key,kind,role),('overdue-quality:LOT','ToDo','PLANNER'))
        self.assertEqual((fields['reference_name'],fields['allocated_to']),('STE','planner@norvale.test'))
        self.assertIn('2000',fields['description'])

    def test_mes_refreshes_material_readiness_before_pinned_start(self):
        actor = Operating.__new__(Operating)
        calls = []
        class Operator:
            def read(self, model, native_id):
                return {'state': 'waiting'} if model == 'mrp.workorder' else {'workorder_ids': [10]}
            def call(self, model, action, ids):
                calls.append((model, action, ids))
            def start(self, mo, rows, released):
                calls.append((mo, rows, released))
        rows = [{'lot_id':4, 'quantity':12}]
        actor.start_execution(Operator(), 9, rows, [4])
        self.assertEqual(calls, [('mrp.production', 'action_assign', [9]), (9, rows, [4])])

    def test_timed_retest_waits_for_physical_readback(self):
        edges = []
        clock = SimpleNamespace(now=700, state={})
        def attempt(key, role, action, **options):
            try:
                return action()
            except Pending:
                return None
        def schedule(clock, job, edge, at, **options):
            edges.append((job, edge, options))
        company = SimpleNamespace(config={'company_operating':True}, company_erp=SimpleNamespace(
            attempt=attempt, sync=lambda lot: True, state={'lots':{'held-batch':{}}}))
        actor = SimpleNamespace(review=lambda batch, inspection: 'Rework Authorized', rework_center=lambda _: 7,
                                rework=lambda batch, inspection: 'reworked-batch')
        job = ['lab', 'held-batch', None]
        with patch('integrations.company_run.timing.schedule', schedule):
            operating_quality(company, clock, actor, job, 'review', 0, 'failure')
            self.assertEqual(edges.pop(), (job, 'rework', dict(role='PRODUCTION', duration=30, rows='failure')))
            def missing(*args):
                raise Pending('No native receipt')
            with patch.object(actor, 'rework', missing):
                operating_quality(company, clock, actor, job, 'rework', 0, 'failure')
            self.assertEqual(edges.pop(), (job, 'rework', dict(role='PRODUCTION', attempt=1, rows='failure', duration=30)))
            operating_quality(company, clock, actor, job, 'rework', 1, 'failure')
            self.assertEqual(edges, [(['lab', 'reworked-batch', None], 'sample', dict(role='LAB',duration=0))])
