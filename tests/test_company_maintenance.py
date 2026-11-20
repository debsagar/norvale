import json
import unittest
from unittest.mock import Mock, patch

from native.company import Engine
from native.company_erp import CompanyERP, Pending


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.authority = Mock()
        self.e = object.__new__(Engine)
        self.e.state = {'ops': {'maintenance': {}, 'production': {}, 'orders': {}, 'shortage_days': {}}}
        self.e.day, self.e.event = 2, 'd0002:production'
        self.e.config = {'maintenance_units': 12, 'capacity_units': 12}
        self.saved = {}
        self.e.persist = lambda: self.saved.update(state=json.dumps(self.e.state))
        self.e.log = Mock()
        self.e.company_erp = self.flow = CompanyERP(self.e, self.authority)
        self.eq = self.flow.equipment
        self.authority.work_center.side_effect = lambda rev, **_: rev['center']
        self.authority.legacy_equipment.return_value = 1
        self.authority.observed_faults.return_value = []
        self.authority.equipment_fault.return_value = {'native_id': 91}
        self.authority.interrupted_batch.return_value = {
            'batch': 'WIP', 'work_order': 'WO-A', 'completion_ref': 'MES:MO:1', 'qty': 12,
        }
        self.authority.maintenance_ticket.return_value = {'number': 7, 'html_url': '/issues/7', 'state': 'closed'}
        self.authority.staffed_restart.return_value = None
        self.authority.restart_evidence.return_value = None
        self.authority.interrupted_disposition.return_value = None

    def restart_controller(self):
        self.e.state = json.loads(self.saved['state'])
        self.e.company_erp = self.flow = CompanyERP(self.e, self.authority)
        self.eq = self.flow.equipment

    def test_recurring_service_retries_native_side_effect_and_requires_independent_evidence(self):
        self.flow.state['work']['a'] = {'equipment': 1, 'produced': 12, 'qty': 24,
                                         'request': {'work_order': 'WO-A'}}
        self.authority.equipment_fault.side_effect = [TimeoutError('lost response'), {'native_id': 91}]
        self.e.service()
        self.restart_controller()
        self.e.service()
        args = self.authority.equipment_fault.call_args_list
        self.assertEqual(args[0], args[1])
        self.authority.equipment_restart.assert_not_called()
        with self.assertRaises(Pending):
            self.eq.available(1, 'PLANNER')
        self.assertEqual(self.eq.available(2, 'PLANNER'), 2)

        self.authority.restart_evidence.return_value = {'id': 8, 'ref': 'verified'}
        self.authority.equipment_restart.return_value = {'native_id': 91, 'date_end': 'now'}
        self.e.service()
        self.assertEqual(self.eq.available(1, 'PLANNER'), 1)
        with self.assertRaises(Pending):
            self.eq.guard_execution('MES:MO:1')
        self.eq.guard_execution('MES:MO:2')
        self.authority.reject_interrupted.assert_not_called()
        self.assertNotIn('rejected_remainder', self.flow.state['work']['a'])
        self.authority.interrupted_disposition.return_value = {'id': 'QI-10'}
        self.authority.reject_interrupted.return_value = {'native_id': 1}
        self.e.service()
        self.restart_controller()
        self.e.service()
        self.assertEqual(self.flow.state['work']['a']['rejected_remainder'], 12)
        self.authority.reject_interrupted.assert_called_once()
        self.assertFalse(self.flow.state['lots'])

    def test_interrupted_cancellation_uses_work_revision_and_business_date(self):
        from integrations.company_run.maintenance import MaintenanceAuthority
        from integrations.qms.adapter import INSPECTION, OP
        self.authority.interrupted_disposition.side_effect = (
            MaintenanceAuthority.interrupted_disposition.__get__(self.authority))
        self.authority.interrupted_settings.return_value = dict(
            qms_reader={'url': 'https://qms'}, qms_approver='qa', specifications={'FG':'new-method'})
        batch = dict(work_order='WO-A', item='FG', uom='Nos', qty=4, batch='WIP', completion_ref='mes:mrp.production:5')
        case = dict(center=1, interrupted=batch)
        work = dict(request=dict(work_order='WO-A', released_on='2027-01-02'), item='FG', uom='Nos',
                    equipment=1, qty=4, revision=dict(inspection_spec='original-spec'))
        self.flow.state['work']['a'] = work
        doc = dict(name='QI', modified='v1', docstatus=1, status='Rejected', workflow_state='Lot Rejected',
                   modified_by='qa', batch_no='WIP', item_code='FG', custom_lot_quantity=4, custom_lot_uom='Nos',
                   custom_decision_rationale='Interrupted ' + batch['completion_ref'], **{OP:'WIP:interrupted'},
                   readings=[dict(specification='pH', numeric=1, min_value=5, max_value=6, reading_1='7.2', status='Rejected')])
        def list_rows(entity, *args, **kwargs):
            return [{'name':'QI', 'modified':'v1', 'creation':'t'}] if entity == INSPECTION else [{'name':'NC'}]
        with patch('integrations.company_run.maintenance.QMS') as qms:
            qms.return_value.get.return_value = doc
            qms.return_value.list.side_effect = list_rows
            for spec, day in [('new-method','2027-01-03'), ('original-spec','2027-01-04'),
                              ('original-spec','2027-01-01'), ('original-spec',None)]:
                doc.update(custom_spec_revision=spec, report_date=day)
                with self.assertRaisesRegex(Pending, 'awaits authorized'):
                    self.eq.dispose(case)
                self.authority.reject_interrupted.assert_not_called()
            doc.update(report_date='2027-01-03')
            self.authority.reject_interrupted.side_effect = [TimeoutError('lost reply'), {'native_id':5}]
            with self.assertRaises(TimeoutError):
                self.eq.dispose(case)
            self.e.day += 1
            self.eq.dispose(case)
        self.assertEqual(case['qa_as_of'], '2027-01-03')
        self.assertEqual(work['rejected_remainder'], 4)
        self.assertFalse(self.flow.state['lots'])

    def test_native_fault_is_adopted_and_usage_fault_is_not_duplicated(self):
        self.flow.state['work']['a'] = {'equipment': 1, 'produced': 12, 'qty': 12}
        fault = dict(native_id=92, description='Fault observed-by-maintenance', workcenter_id=[1, 'A'])
        self.authority.observed_faults.return_value = [fault]
        self.authority.interrupted_batch.return_value = None
        self.e.service()
        self.e.service()
        case, = self.eq.cases.values()
        self.assertEqual(case['fault'], fault)
        self.authority.equipment_fault.assert_not_called()

    def test_real_production_planner_skips_blocked_route_and_preserves_per_equipment_capacity(self):
        self.e.state['ops']['maintenance']['legacy'] = {'closed': None}
        self.eq.observe(1, 'vibration-1', 'Vibration', 0)
        self.e.state['ops']['orders'] = {k: {'item': k} for k in 'ABC'}
        self.e.wh = {'rm': 'RM'}
        self.flow.state['lots']['raw'] = dict(batch='raw', item='material', uom='kg', kind='opening', qty=36)
        self.flow.available = lambda lot, role: (lot['qty'], 'released')
        self.e.get = lambda *args: {'items': [{'qty': 12}]}
        self.flow.definition = lambda item: ({'center': 'ABC'.index(item) + 1}, {'material': ('kg', 1)})
        self.flow.production = Mock()
        self.e.production()
        plan = self.flow.production.call_args.args[0]
        self.assertEqual([(w['item'], w['equipment']) for w in plan['work']], [('B', 2), ('C', 3)])

    def test_partial_work_reservations_and_held_stock_limit_new_orders(self):
        self.e.state['ops']['orders'] = {'new': {'item': 'FG'}}
        self.e.config['company_operating'] = True
        self.e.get = lambda *args: {'items': [{'qty': 12}]}
        self.flow.definition = lambda item: ({'center': 1}, {'raw': ('kg', 1)})
        self.e.inventory = lambda role: {('raw', 'RM'): 100}
        self.flow.state['lots'] = {batch: dict(batch=batch, item='raw', kind='opening', uom='kg')
                                   for batch in ('released', 'held')}
        self.flow.available = lambda lot, role: (10 if lot['batch'] == 'released' else 0, {})
        self.flow.state['work']['older'] = dict(qty=10, produced=4, item='other',
            inputs=[dict(batch='released', qty=10)], used={'released': 4})
        plan = self.flow.build_plan()
        self.assertEqual([w['qty'] for w in plan['work']], [4])
        self.flow.state['work']['older']['rejected_remainder'] = 6
        self.assertEqual([w['qty'] for w in self.flow.build_plan()['work']], [10])

    def test_verified_ticket_restart_admits_previously_blocked_work_once(self):
        self.e.state['ops']['orders'] = {'order': {'item': 'FG'}}
        self.e.wh = {'rm': 'RM'}
        self.flow.state['lots']['raw'] = dict(batch='raw', item='material', uom='kg', kind='opening', qty=3)
        self.flow.available = lambda lot, role: (lot['qty'], 'released')
        self.e.get = lambda *args: {'items': [{'qty': 3}]}
        self.e.config['company_operating'] = True
        key = self.eq.observe(1, 'fault', 'Stopped center', 0)
        self.authority.interrupted_batch.return_value = None

        def plan():
            self.flow.definition = lambda item: ({'center': 1}, {'material': ('kg', 1)})
            return self.flow.build_plan()['work']

        self.e.service()
        self.assertEqual(plan(), [])
        self.assertIn('verified restart', self.flow.state['pending']['equipment:FG']['last_error'])

        self.restart_controller()
        self.flow.available = lambda lot, role: (lot['qty'], 'released')
        self.authority.restart_evidence.return_value = {'id': 8, 'ref': 'verified'}
        self.authority.equipment_restart.return_value = {'native_id': 91, 'date_end': 'now'}
        self.e.day += 1
        self.e.service()
        self.e.service()
        work, = plan()
        self.assertEqual((work['order'], work['qty'], work['equipment']), ('order', 3, 1))
        self.assertEqual(self.eq.cases[key]['ticket']['number'], 7)
        self.authority.close_maintenance_ticket.assert_called_once_with(
            key, self.eq.cases[key], role='MAINTENANCE')
        self.authority.equipment_restart.assert_called_once()


if __name__ == '__main__':
    unittest.main()
