"""Offline fault injection for recurring ERP lot handoffs."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from native.company_erp import CompanyERP, Pending, digest


def decision(batch, qty, version='release', authorized=True):
    return {'batch': batch, 'item': 'FG', 'uom': 'Nos', 'qty': qty, 'id': 'QMS-1', 'version': version,
            'status': 'released' if qty else 'hold', 'authorized': authorized,
            'approver': 'QA-1', 'evidence': 'inspection-1'}


class RestartTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'state.json'
        self.authority = Mock()
        self.native = {}
        self.leases = {}
        self.authority.dispatch_lease.side_effect = self.lease
        self.engine, self.flow = self.load()
        self.lot = {'batch': 'B', 'item': 'FG', 'kind': 'finished', 'qty': 12, 'uom': 'Nos'}
        self.flow.state['lots']['B'] = self.lot
        self.engine.persist()

    def test_pooled_output_covers_both_sales_orders_after_recovery(self):
        flow, engine = self.flow, self.engine
        engine.config = {}
        engine.state['ops']['orders'] = {key: {'item':'FG'} for key in ('first','second')}
        engine.get = lambda key, role: {'items':[{'qty':72 if key == 'first' else 60,
                                                  'delivered_qty':66 if key == 'first' else 0}]}
        state = flow.state
        state['lots'] = {}
        for order, made in [('first',36), ('second',30)]:
            state['work'][order] = dict(order=order, item='FG', qty=made*2, produced=made,
                                        request={'work_order':order}, inputs=[])
            state['lots'][order] = dict(batch=order,item='FG',kind='finished',qty=made)
        state['shipments']['delivered'] = dict(order='first',target_id='DN-1',
            rows=[{'batch':'first','qty':36},{'batch':'second','qty':30}])
        phantom = dict(order='second',item='FG',qty=24,inputs=[])
        state['work']['excess'] = phantom
        state['pending'] = {'mes-dispatch:excess':{'last_error':'HTTP 417 OverProductionError'}}
        # Uncertain external work must survive recovery.
        self.native['excess'] = {'name':'WO','docstatus':1}
        flow.retire_overplanned_work()
        self.assertIn('excess',state['work'])
        del self.native['excess']
        flow.retire_overplanned_work()
        self.assertEqual(state['withdrawn_work']['excess']['proposal'],phantom)
        self.assertEqual(flow.finished_supply(), {'FG':66})
        self.assertEqual(flow.build_plan(), {'work':[], 'shortages':[]})
        self.authority.definition.assert_not_called()
        _, reopened = self.load()
        self.assertNotIn('excess',reopened.state['work'])
        self.assertNotIn('mes-dispatch:excess',reopened.state['pending'])

    def test_withdrawn_finished_lot_no_longer_covers_customer_demand(self):
        flow = self.flow
        # Before its first decision the new lot covers demand provisionally.
        self.assertEqual(flow.finished_supply()['FG'], 12)
        flow.state['shipments']['first'] = dict(target_id='DN-1', rows=[dict(batch='B', qty=3)])
        flow.state['moves']['release'] = dict(done=True, batch='B', source='Hold', target='FG')
        flow.state['synced'] = {'B': None}
        self.engine.persist()
        _, reopened = self.load()
        self.assertEqual(reopened.finished_supply().get('FG', 0), 0)
        reopened.state['synced']['B'] = decision('B', 12, 'next-version')
        self.assertEqual(reopened.finished_supply()['FG'], 9)

    def test_replacement_after_scrap_keeps_demand_reference(self):
        flow, engine = self.flow, self.engine
        engine.config = {'capacity_units': 100}
        engine.wh['wip'] = 'WIP'
        engine.get = lambda *args: {'name': 'SO', 'docstatus': 1, 'items': [
            {'name': 'SO-line', 'item_code': 'FG', 'qty': 60, 'work_order_qty': 60, 'stock_uom': 'Nos'}]}
        flow.state['work']['original'] = dict(order='order', item='FG', qty=60, produced=60,
                                              inputs=[], sales_order_item='SO-line')
        flow.state['lots']['B']['qty'] = 60
        engine.state['operating_lot_rejections'] = {'B': 'rejected'}
        self.assertEqual(flow.finished_supply()['FG'], 0)
        revision = {'erp_bom': 'BOM'}
        flow.definition = lambda item: (revision, {})
        self.authority.work_center.return_value = 'Center'
        flow.equipment.available = Mock()
        def submit(key, kind, body, role, approved, before_submit):
            before_submit(body)
            return dict(body, name='WO-2')
        engine.write = Mock(side_effect=submit)
        self.authority.dispatch.side_effect = lambda req, **kw: dict(id='MES-2', version='1', request=req)
        work = flow.pin_work(dict(key='replace', item='FG', order='order', qty=60,
                                  revision=revision, equipment='Center'))
        # A partial fulfillment between draft creation and submit changes what Sales still needs.
        read_order = engine.get
        def changed(key, kind, body, role, approved, before_submit):
            order = read_order('order', 'PLANNER')
            order['items'][0]['delivered_qty'] = 1
            engine.get = lambda *args: order
            return submit(key, kind, body, role, approved, before_submit)
        engine.write.side_effect = changed
        with self.assertRaisesRegex(Pending, 'draft Work Order'):
            flow.dispatch(work)
        self.authority.dispatch.assert_not_called()
        engine.get = read_order
        engine.write.side_effect = submit
        flow.dispatch(work)
        native = engine.write.call_args.args[2]
        self.assertIsNone(native['sales_order'])
        self.assertIsNone(native['sales_order_item'])
        self.assertEqual(json.loads(native['custom_norvale_definition']),
                         dict(revision, erp_released_on=engine.date(), replenishes_sales_order='SO', replenishes_line='SO-line'))
        self.assertEqual(work['request']['sales_order'], 'SO')
        self.assertEqual(flow.finished_supply()['FG'], 60)

    def test_submitted_work_survives_new_revision_before_first_mes_dispatch(self):
        flow, e = self.flow, self.engine
        e.config = {'capacity_units':10}
        e.wh['wip'] = 'WIP'
        revision = dict(id='1', version='rev-a', erp_bom='BOM-A', effective_from='2027-01-01')
        work = dict(key='wo',item='FG',quantity=1,qty=1,uom='Nos',sales_order='S',sales_order_item='L',
                    equipment='Center',inputs=[], revision=revision)
        self.native['wo'] = dict(name='MFG-1', company=e.company, docstatus=1, production_item='FG',bom_no='BOM-A',
                                 sales_order='S',sales_order_item='L',qty=1,planned_start_date='2027-01-04',
                                 custom_norvale_definition=json.dumps(dict(revision,erp_released_on='2027-01-02'),sort_keys=True))
        flow.state['work']['wo'] = work
        flow.definition = Mock(return_value=(dict(revision,id='2',version='rev-b'),{}))
        self.authority.work_center.return_value = 'Center'
        flow.equipment.available = Mock()
        e.write = Mock()
        self.authority.dispatch.side_effect = lambda req, **kw: dict(id='9',version='1',request=req)
        flow.dispatch(work)
        flow.definition.assert_not_called()
        e.write.assert_not_called()
        self.assertEqual((work['request']['released_on'],work['request']['as_of']),('2027-01-02','2027-01-04'))
        self.assertEqual(work['request']['revision'],revision)
        self.assertEqual(work['request']['work_order'],'MFG-1')

    def test_closed_demand_releases_uncommitted_material_reservations(self):
        flow, e = self.flow, self.engine
        work = dict(key='closed',order='order', sales_order='S',sales_order_item='L',
                    item='FG',qty=5,inputs=[dict(batch='B',qty=2)],revision={})
        flow.state['work']['closed'] = work
        e.get = Mock(return_value=dict(name='S',docstatus=1,status='Closed',items=[]))
        flow.dispatch(work)
        self.assertEqual(flow.reserved_inputs(), {})
        self.assertEqual(flow.state['withdrawn_work']['closed']['proposal'], work)
        self.authority.dispatch.assert_not_called()

    def test_unreleased_superseded_proposal_frees_material_after_reopen(self):
        flow, e = self.flow, self.engine
        original = dict(key='old',item='FG',qty=1,revision={'id':'A'},inputs=[dict(batch='RAW',qty=1)])
        flow.state['work']['old'] = original
        flow.definition = lambda item: ({'id':'B'}, {})
        self.assertEqual(flow.reserved_inputs(),{'RAW':1})
        flow.production({'work':[original]})
        self.assertEqual(flow.reserved_inputs(),{})
        _, reopened = self.load()
        self.assertEqual(reopened.state['withdrawn_work']['old']['proposal'],original)

    def lease(self, request, *, outcome, role):
        self.assertEqual(role, 'WAREHOUSE')
        key = request['id']
        record = self.leases.get(key)
        if record:
            self.assertEqual(record['request'], request)
        submit_permitted = False
        if outcome == 'active':
            record = self.leases.setdefault(key, {'request': request, 'state': 'active'})
        elif outcome == 'withdrawn':
            record = self.leases.setdefault(key, {'request': request, 'state': outcome})
            self.assertIn(record['state'], ('active', 'withdrawn'))
            record['state'] = outcome
        elif outcome == 'armed':
            self.assertIn(record['state'], ('active', 'armed'))
            submit_permitted = record['state'] == 'active'
            record['state'] = outcome
        elif outcome == 'submitted':
            self.assertIn(record['state'], ('armed', 'submitted'))
            record['state'] = outcome
        return {'id': key, 'delivery': request['delivery'],
                'state': record['state'] if record else 'absent', 'submit_permitted': submit_permitted}

    def load(self):
        e = SimpleNamespace(
            state=json.loads(self.path.read_text()) if self.path.exists() else {
                'physical': {}, 'ops': {'meter': 0, 'orders': {'order': {'item': 'FG'}},
                                        'maintenance': {}}},
            day=3, company='NCC', wh={'fg': 'FG', 'hold': 'Hold', 'rm': 'RM'},
            log=Mock(), date=lambda: '2027-01-04', recover_company_doc=lambda key, *args: self.native.get(key),
            invoice_delivery=Mock(),
        )
        e.persist = lambda: self.path.write_text(json.dumps(e.state))
        return e, CompanyERP(e, self.authority)

    def shipping(self, e, flow, *, fail=None, ordered=12):
        if isinstance(ordered, int):
            ordered = {'order': ordered}
        orders = {key: {'name': f'SO-{i}', 'docstatus': 1, 'items': [
            {'name': 'SO-row' if i == 1 else f'SO-row-{i}', 'item_code': 'FG', 'qty': qty}]}
            for i, (key, qty) in enumerate(ordered.items(), 1)}

        def shipped_rows():
            return [row for doc in self.native.values() if doc['docstatus'] == 1 for row in doc['items']]

        def get(key, role):
            order = orders[key]
            line, = order['items']
            line['delivered_qty'] = sum(row['qty'] for row in shipped_rows()
                                        if row['against_sales_order'] == order['name']
                                        and row['so_detail'] == line['name'])
            return order

        def mapped(*args, source_name):
            order = next(order for order in orders.values() if order['name'] == source_name)
            return {'items': [{'item_code': 'FG', 'against_sales_order': source_name,
                               'so_detail': order['items'][0]['name']}]}

        e.get = get
        e.mapped = mapped
        flow.verify_row = Mock()
        flow.balance = Mock(side_effect=lambda lot, wh, role: lot['qty'] - sum(
            row['qty'] for row in shipped_rows() if row['batch_no'] == lot['batch']))

        def write(key, doctype, data, role, submit, before_submit):
            if key not in self.native:
                self.native[key] = {**copy.deepcopy(data), 'name': f'DN-{len(self.native) + 1}',
                                    'docstatus': 0}
            draft = self.native[key]
            if fail == 'revoke':
                self.authority.disposition.return_value = decision('B', 0, 'revoked')
            before_submit(draft)
            self.assertEqual(next(reversed(self.leases.values()))['state'], 'armed')
            if fail == 'in-flight':
                raise TimeoutError('Original submit may still be in flight')
            draft['docstatus'] = 1
            if fail == 'timeout':
                raise TimeoutError('Lost native submit response')
            return draft

        e.write = Mock(side_effect=write)

    def release(self):
        approved = decision('B', 5)
        self.authority.disposition.return_value = approved
        self.flow.state['synced'] = {'B': approved}
        self.engine.persist()

    def test_failed_or_missing_release_blocks_dispatch_even_with_erp_balance(self):
        for inspection in (None, decision('B', 0, 'failed')):
            with self.subTest(inspection=inspection):
                self.authority.disposition.return_value = inspection
                self.shipping(self.engine, self.flow)
                self.flow.fulfillment()
                self.assertIn('QMS release', self.flow.state['pending']['dispatch:order']['last_error'])
                self.engine.write.assert_not_called()
                self.engine.invoice_delivery.assert_not_called()
                self.assertFalse(self.engine.state['physical'])
        self.release()
        self.flow.fulfillment()
        self.assertNotIn('dispatch:order', self.flow.state['pending'])
        shipped, = self.engine.state['physical'].values()
        self.assertEqual(shipped['qty'], 5)

    def test_two_releases_dispatch_partial_quantities_across_restart(self):
        self.lot['qty'] = 3
        first = decision('B', 3)
        self.authority.disposition.return_value = first
        self.flow.state['synced'] = {'B': first}
        self.shipping(self.engine, self.flow, ordered=6)
        self.flow.fulfillment()
        only, = self.native.values()
        self.assertEqual((only['docstatus'], only['items'][0]['qty']), (1, 3))

        engine, flow = self.load()
        second = decision('B2', 3, 'next-release')
        flow.state['lots']['B2'] = {**self.lot, 'batch': 'B2'}
        flow.state['synced']['B2'] = second
        self.authority.disposition.side_effect = lambda lot, **_: flow.state['synced'][lot['batch']]
        self.shipping(engine, flow, ordered=6)
        flow.fulfillment()
        shipments = list(flow.state['shipments'].values())
        self.assertEqual([(row['target_id'], row['rows'][0]['qty']) for row in shipments],
                         [('DN-1', 3), ('DN-2', 3)])
        self.assertEqual([row['rows'][0]['disposition'] for row in shipments], [first, second])
        self.assertEqual(shipments[1]['lease_receipt']['state'], 'submitted')
        self.assertEqual([call.args[0] for call in engine.invoice_delivery.call_args_list],
                         [row['key'] for row in shipments])

    def test_lot_revoked_after_draft_is_never_submitted_even_after_restart(self):
        self.release()
        self.shipping(self.engine, self.flow, fail='revoke')
        self.flow.fulfillment()
        self.assertEqual([doc['docstatus'] for doc in self.native.values()], [0])
        e, flow = self.load()
        self.shipping(e, flow)
        flow.fulfillment()
        e.write.assert_not_called()
        e.invoice_delivery.assert_not_called()
        self.assertFalse(e.state['physical'])

    def test_denied_lease_records_the_change_even_after_a_passing_precheck(self):
        self.engine.config = {'company_operating': True}
        self.release()
        self.shipping(self.engine, self.flow)
        def denied(request, *, outcome, role):
            return dict(id=request['id'], delivery=request['delivery'], state='denied')
        self.authority.dispatch_lease.side_effect = denied
        with self.assertRaisesRegex(Pending, 'lease denied'):
            self.flow.ship('order', {'item': 'FG'})
        _, flow = self.load()
        shipment, = flow.state['shipments'].values()
        self.assertTrue(shipment['abandoned'])
        change = shipment['allocation_change']
        self.assertEqual(change['allocations'], [dict(batch='B', quantity=5, uom='Nos')])
        self.assertEqual(change['delivery'], 'DN-1')

    def test_withdrawn_allocation_reaches_sales_after_mail_timeout(self):
        self.engine.config = {'company_operating':True}
        self.release()
        self.flow.state['lots']['B2'] = {**self.lot, 'batch': 'B2'}
        self.flow.state['synced']['B2'] = decision('B2', 3)
        self.authority.disposition.side_effect = lambda lot, **kw: (
            self.authority.disposition.return_value if lot['batch'] == 'B' else self.flow.state['synced']['B2'])
        self.shipping(self.engine,self.flow,fail='revoke')
        with self.assertRaises(Pending):
            self.flow.ship('order',{'item':'FG'})
        op, = self.flow.state['shipments'].values()
        self.assertEqual(op['allocation_change']['batch'],'B')
        self.assertFalse(op.get('target_id'))
        # Reopen. Warehouse has fenced off the draft; the planner and Sales
        # each read the task under their own credentials.
        e,flow = self.load()
        op, = flow.state['shipments'].values()
        flow.recover_shipments()
        self.assertTrue(op['abandoned'])
        e.config = {'company_operating':True}
        e.dated = lambda d:'2027-01-04'
        e.get = Mock(return_value={'name':'SO-1','items':[dict(name='SO-row',qty=12,delivered_qty=0,
                                                              delivery_date='2027-01-05',stock_uom='Nos')]})
        task = {}
        def create(key,kind,fields,role):
            self.assertEqual((kind,role),('ToDo','PLANNER'))
            return task.setdefault(key,dict(fields,name='task-1'))
        e.write = create
        e.api = {'PLANNER':Mock(),'SALES':Mock()}
        e.api['PLANNER'].get.return_value = self.native[op['key']]
        e.api['SALES'].get.side_effect = lambda *args:next(iter(task.values()))
        from native.company_erp import MailReadbackError
        e.mail=Mock(side_effect=[TimeoutError(), MailReadbackError("delivery differs"), None])
        with self.assertRaises(TimeoutError): flow.report_allocation_change(op)
        key = 'sales-impact:' + op['key']
        flow.attempt(key, 'PLANNER', lambda: flow.report_allocation_change(op))
        self.assertIn('mail reconciliation', flow.state['pending'][key]['last_error'])
        self.assertNotIn('sales_task', op['allocation_change'])
        e.get.return_value['items'][0]['delivered_qty']=12
        flow.report_allocation_change(op)
        self.assertEqual(op['allocation_change']['sales_task'],'task-1')
        self.assertEqual(e.mail.call_args_list[0],e.mail.call_args_list[1])
        message = e.mail.call_args.args[-1]
        self.assertIn('Draft delivery DN-1', message)
        self.assertIn('5 Nos from lot B, 3 Nos from lot B2', message)
        self.assertIn('12 Nos', message)
        self.assertEqual(len(task),1)
        self.assertEqual(next(iter(task.values()))['reference_name'],'SO-1')

    def test_customer_hold_during_lease_acquisition_blocks_submit(self):
        self.release()
        self.shipping(self.engine, self.flow)
        order = self.engine.get('order','WAREHOUSE')
        def lease(request, *, outcome, role):
            receipt = self.lease(request, outcome=outcome, role=role)
            if outcome == 'active':
                order['status'] = 'On Hold'
            return receipt
        self.authority.dispatch_lease.side_effect = lease
        with self.assertRaisesRegex(Pending, 'Sales order no longer covers'):
            self.flow.ship('order', {'item':'FG'})
        op, = self.flow.state['shipments'].values()
        self.assertTrue(op['abandoned'])
        self.assertEqual(op['lease_receipt']['state'], 'withdrawn')
        self.assertEqual(self.native[op['key']]['docstatus'], 0)
        self.assertNotIn('submit_started', op)
        self.assertFalse(self.engine.state['physical'])

    def test_changed_delivery_draft_is_not_submitted(self):
        self.release()
        self.shipping(self.engine, self.flow)
        write = self.engine.write.side_effect
        def changed(key, doctype, data, role, submit, before_submit):
            def guard(draft):
                draft['items'][0]['qty'] += 1
                before_submit(draft)
            return write(key, doctype, data, role, submit, guard)
        self.engine.write.side_effect = changed
        with self.assertRaisesRegex(Pending, 'differs from pinned allocation'):
            self.flow.ship('order', {'item': 'FG'})
        draft, = self.native.values()
        self.assertEqual(draft['docstatus'], 0)
        self.assertFalse(self.engine.state['physical'])

    def test_unknown_dispatch_submit_recovers_before_reallocation_or_billing(self):
        self.release()
        self.shipping(self.engine, self.flow, fail='timeout')
        # Restart before the in-day reconciliation completes.
        with self.assertRaises(TimeoutError):
            self.flow.ship('order', {'item': 'FG'})
        e, flow = self.load()
        self.shipping(e, flow)
        self.authority.disposition.return_value = decision('B', 0, 'revoked')
        flow.fulfillment()
        e.write.assert_not_called()
        ship, = flow.state['shipments'].values()
        self.assertEqual(ship['target_id'], 'DN-1')
        self.assertEqual(ship['lease_receipt']['state'], 'submitted')
        self.assertEqual(e.state['physical'][ship['key']]['qty'], 5)
        e.invoice_delivery.assert_called_once_with(ship['key'], 3)

    def test_ambiguous_draft_keeps_lease_and_never_replays_submit(self):
        self.release()
        self.shipping(self.engine, self.flow, fail='in-flight')
        with self.assertRaises(TimeoutError):
            self.flow.ship('order', {'item': 'FG'})
        e, flow = self.load()
        self.shipping(e, flow)
        flow.fulfillment()
        e.write.assert_not_called()
        self.assertEqual(next(iter(self.leases.values()))['state'], 'armed')
        e.invoice_delivery.assert_not_called()
        next(iter(self.native.values()))['docstatus'] = 1
        flow.fulfillment()
        self.assertEqual(next(iter(self.leases.values()))['state'], 'submitted')
        e.invoice_delivery.assert_called_once()

    def test_recurring_dispatch_reserves_only_the_uncertain_order_line_and_batch(self):
        self.release()
        orders = {'order': 7, 'second': 6}
        self.engine.state['ops']['orders']['second'] = {'item': 'FG'}
        self.shipping(self.engine, self.flow, fail='in-flight', ordered=orders)
        self.flow.fulfillment()
        blocked, = self.flow.state['shipments'].values()
        blocked_key = blocked['key']
        pending_key = 'delivery-reconciliation:' + blocked_key
        self.assertTrue(blocked['submit_started'])
        self.engine.write.assert_called_once()
        self.engine.invoice_delivery.assert_not_called()

        e, flow = self.load()
        # Exercise a checkpoint from before reconciliation was scoped.
        pending = flow.state['pending']
        pending['delivery-reconciliation'] = pending.pop(pending_key, pending.get('delivery-reconciliation'))
        flow.state['lots']['B2'] = {**self.lot, 'batch': 'B2', 'qty': 8}
        flow.state['synced']['B2'] = decision('B2', 8, 'independent-release')
        self.authority.disposition.side_effect = lambda lot, **_: flow.state['synced'][lot['batch']]
        self.shipping(e, flow, ordered=orders)
        self.authority.disposition.reset_mock()
        flow.fulfillment()

        self.assertEqual([(doc['docstatus'], row['so_detail'], row['batch_no'], row['qty'])
                          for doc in self.native.values() for row in doc['items']],
                         [(0, 'SO-row', 'B', 5), (1, 'SO-row', 'B2', 2), (1, 'SO-row-2', 'B2', 6)])
        self.assertEqual(flow.state['shipments'][blocked_key], blocked)
        self.assertEqual(self.leases[blocked['lease_id']]['state'], 'armed')
        self.assertIn(pending_key, flow.state['pending'])
        self.assertNotIn('delivery-reconciliation', flow.state['pending'])
        self.assertEqual([call.args[0]['batch'] for call in flow.balance.call_args_list], ['B2'] * 4)
        self.assertEqual([call.args[0]['batch'] for call in self.authority.disposition.call_args_list], ['B2'] * 4)
        self.assertEqual(e.write.call_count, 2)
        self.assertEqual(e.invoice_delivery.call_count, 2)
        self.assertNotIn(blocked_key, e.state['physical'])

        # Reopening again leaves the original draft and native lease untouched.
        e, flow = self.load()
        self.shipping(e, flow, ordered=orders)
        flow.fulfillment()
        e.write.assert_not_called()
        self.assertEqual(self.leases[blocked['lease_id']]['state'], 'armed')
        self.assertIn(pending_key, flow.state['pending'])

        # Late native commit is finalized and billed on its original day.
        self.native[blocked_key]['docstatus'] = 1
        e.day = 4
        e.invoice_delivery.reset_mock()
        flow.fulfillment()
        e.write.assert_not_called()
        self.assertNotIn(pending_key, flow.state['pending'])
        self.assertEqual(self.leases[blocked['lease_id']]['state'], 'submitted')
        self.assertEqual(e.state['physical'][blocked_key]['qty'], 5)
        e.invoice_delivery.assert_any_call(blocked_key, 3)

    def test_lost_acquire_reply_reuses_the_pinned_lease_before_first_submit(self):
        self.release()
        self.shipping(self.engine, self.flow)
        def lost(request, **kwargs):
            self.lease(request, **kwargs)
            raise TimeoutError('QMS acquired the lease but its reply was lost')
        self.authority.dispatch_lease.side_effect = lost
        with self.assertRaises(TimeoutError):
            self.flow.ship('order', {'item': 'FG'})
        self.assertEqual(next(iter(self.native.values()))['docstatus'], 0)
        self.authority.dispatch_lease.side_effect = self.lease
        e, flow = self.load()
        self.shipping(e, flow)
        with self.assertRaisesRegex(Pending, 'readback'):
            flow.verify_row.side_effect = Pending('readback')
            flow.recover_shipment(next(iter(flow.state['shipments'].values())))
        self.assertEqual(len(self.leases), 1)
        self.assertEqual(next(iter(self.leases.values()))['state'], 'armed')
        flow.verify_row.side_effect = None
        flow.recover_shipments()
        self.assertEqual(next(iter(self.leases.values()))['state'], 'submitted')

    def test_lost_arm_reply_never_mints_a_second_submit_permission(self):
        self.release()
        self.shipping(self.engine, self.flow)
        def lost(request, **kwargs):
            receipt = self.lease(request, **kwargs)
            if kwargs['outcome'] == 'armed':
                raise TimeoutError('QMS arm committed but its reply was lost')
            return receipt
        self.authority.dispatch_lease.side_effect = lost
        with self.assertRaises(TimeoutError):
            self.flow.ship('order', {'item': 'FG'})
        self.assertEqual(next(iter(self.native.values()))['docstatus'], 0)

        self.authority.dispatch_lease.side_effect = self.lease
        e, flow = self.load()
        self.shipping(e, flow)
        flow.fulfillment()
        e.write.assert_not_called()
        e.invoice_delivery.assert_not_called()
        self.assertEqual(next(iter(self.leases.values()))['state'], 'armed')

    def test_pre_arm_withdrawal_fences_late_acquire_after_lost_reply(self):
        self.release()
        self.shipping(self.engine, self.flow)
        def lost(request, **kwargs):
            self.lease(request, **kwargs)
            raise TimeoutError('QMS acquire reply was lost')
        self.authority.dispatch_lease.side_effect = lost
        with self.assertRaises(TimeoutError):
            self.flow.ship('order', {'item': 'FG'})

        self.authority.dispatch_lease.side_effect = self.lease
        e, flow = self.load()
        self.shipping(e, flow)
        flow.balance = lambda *args: 0
        with self.assertRaisesRegex(Pending, 'changed at dispatch'):
            flow.recover_shipment(next(iter(flow.state['shipments'].values())))
        record, = self.leases.values()
        self.assertEqual(self.lease(record['request'], outcome='active', role='WAREHOUSE')['state'],
                         'withdrawn')
        self.assertEqual(next(iter(self.native.values()))['docstatus'], 0)
        self.assertFalse(e.state['physical'])

    def test_lost_finalize_reply_replays_receipt_without_second_submit(self):
        self.release()
        self.shipping(self.engine, self.flow)
        def lost(request, **kwargs):
            receipt = self.lease(request, **kwargs)
            if kwargs['outcome'] == 'submitted':
                raise TimeoutError('QMS finalize reply was lost')
            return receipt
        self.authority.dispatch_lease.side_effect = lost
        with self.assertRaises(TimeoutError):
            self.flow.ship('order', {'item': 'FG'})
        self.assertFalse(self.engine.state['physical'])

        self.authority.dispatch_lease.side_effect = self.lease
        e, flow = self.load()
        self.shipping(e, flow)
        flow.fulfillment()
        e.write.assert_not_called()
        self.assertEqual(next(iter(self.leases.values()))['state'], 'submitted')
        e.invoice_delivery.assert_called_once()

    def test_unauthorized_and_unsynced_release_block_aggregate_stock(self):
        self.release()
        self.shipping(self.engine, self.flow)
        self.authority.disposition.return_value = decision('B', 5, authorized=False)
        self.flow.fulfillment()
        self.engine.write.assert_not_called()
        self.authority.disposition.return_value = decision('B', 5, 'new')
        self.flow.fulfillment()
        self.engine.write.assert_not_called()

    def test_two_meses_reuse_native_ids_across_restart(self):
        def completions(dispatch, **kwargs):
            return [dict(id='1', version='v1', source=dispatch['source'])]
        self.authority.completions.side_effect = completions
        for source in ('east', 'west'):
            work = dict(key=source, dispatch=dict(id='1', version='v1', source=source))
            self.flow.collect(work)
            _, self.flow = self.load()
            self.flow.collect(work)
        self.assertEqual(len(self.flow.state['completions']), 2)
        self.authority.completions.side_effect = lambda *a, **kw: [dict(id='1',version='v1',source='east')]
        with self.assertRaises(Pending):
            self.flow.collect(work)

    def test_rounded_native_stock_transfer_blocks_manufacture_submit(self):
        row = dict(item_code='OIL', qty=0.0675, batch_no='OIL-A', s_warehouse='Raw')
        draft = dict(work_order='WO', fg_completed_qty=54, items=[dict(row, transfer_qty=0.068)])
        with self.assertRaisesRegex(Pending, 'stock-unit transfer'):
            self.flow.check_completion_draft(draft, 'WO', 54, [row])

    def test_partial_mes_post_timeout_replays_once_and_keeps_remaining_work(self):
        e, flow = self.engine, self.flow
        revision = {'id': 'REV', 'version': '1', 'erp_bom': 'BOM-FG'}
        raw = {'batch': 'RAW', 'item': 'RM', 'qty': 10, 'uom': 'Kg', 'kind': 'opening'}
        flow.state['lots']['RAW'] = raw
        flow.state['work']['work'] = {
            'key': 'work', 'qty': 12, 'item': 'FG', 'uom': 'Nos', 'revision': revision,
            'dispatch': {'id': 'MES-WORK', 'version': '1'}, 'request': {'work_order': 'WO'},
            'inputs': [raw],
        }
        report = {'id': 'MES-R', 'version': '1', 'dispatch_id': 'MES-WORK', 'work_order': 'WO',
                  'revision': revision, 'authorized': True,
                  'inputs': [{'batch': 'RAW', 'item': 'RM', 'uom': 'Kg', 'qty': 4}],
                  'output': {'lot_id': 'MES-L', 'item': 'FG', 'uom': 'Nos', 'qty': 5}}
        self.authority.completions.return_value = [report]

        def post(key, doctype, data, role, submit, before_submit):
            draft = {**data, 'name': 'STE-1', 'doctype': doctype, 'docstatus': 0}
            pin = json.loads(draft['remarks'])
            with self.assertRaisesRegex(Pending, 'pinned MES completion'):
                before_submit({**draft, 'remarks': json.dumps({**pin, 'dispatch_id': 'OTHER'})})
            before_submit(draft)
            self.native[key] = {**draft, 'docstatus': 1}
            raise TimeoutError('Lost ERP response')

        e.write = Mock(side_effect=post)
        flow.balance = lambda *args: 10
        flow.batch = lambda *args: 'MES-B'
        flow.poll_work()
        key = 'mes-completion:' + digest('MES-R')
        self.assertIn(key, flow.state['pending'])
        self.assertEqual(len(self.native), 1)
        e, flow = self.load()
        e.write = Mock()
        flow.batch = lambda *args: 'MES-B'
        flow.verify_row = Mock()
        original = self.native[key]['remarks']
        for field in ('completion_id', 'version', 'dispatch_id', 'output_lot_id', 'revision'):
            self.native[key]['remarks'] = json.dumps({**json.loads(original), field: 'WRONG'})
            flow.poll_work()
            self.assertIn(key, flow.state['pending'])
            self.assertFalse(flow.state['completions'][key].get('done'))
            self.assertNotIn('MES-B', flow.state['lots'])
            e.write.assert_not_called()
        self.native[key]['remarks'] = original
        flow.poll_work()
        flow.poll_work()
        self.assertEqual(flow.state['completions'][key]['target_id'], 'STE-1')
        self.assertEqual(flow.state['work']['work']['produced'], 5)
        self.assertEqual(flow.state['work']['work']['used'], {'RAW': 4})
        self.assertEqual(e.state['ops']['meter'], 5)
        self.assertEqual(flow.state['lots']['MES-B']['completion_id'], 'MES-R')
        self.assertEqual(flow.verify_row.call_count, 2)
        e.write.assert_not_called()
        self.authority.completions.return_value = [{**report, 'version': '2'}]
        with self.assertRaises(Pending):
            flow.collect(flow.state['work']['work'])

    def test_exhausted_input_stays_pending(self):
        flow = self.flow
        raw = {'batch': 'RAW', 'item': 'RM', 'qty': 10, 'uom': 'Kg', 'kind': 'opening'}
        flow.state['lots']['RAW'] = raw
        flow.state['work']['w'] = {'key': 'w', 'qty': 12, 'item': 'FG', 'uom': 'Nos',
                                   'revision': {}, 'dispatch': {'id': 'D'},
                                   'request': {'work_order': 'WO'}, 'inputs': [raw]}
        flow.balance = lambda *args: 1
        flow.batch = lambda *args: 'OUT'
        self.engine.write = Mock()
        report = {'dispatch_id': 'D', 'work_order': 'WO', 'revision': {}, 'authorized': True,
                  'inputs': [{**raw, 'qty': 2}], 'output': {'lot_id': 'O', 'item': 'FG', 'qty': 3, 'uom': 'Nos'}}
        with self.assertRaisesRegex(Pending, 'input lot insufficient'):
            flow.post_completion('post', {'work': 'w', 'report': report})
        self.engine.write.assert_not_called()

    def test_release_submit_timeout_recovers_ledger_before_retry(self):
        e, flow = self.engine, self.flow
        approved = decision('B', 5)
        self.authority.disposition.return_value = approved
        quantities = {'FG': 0, 'Hold': 12}
        flow.balance = lambda lot, warehouse, role: quantities[warehouse]

        def post(key, doctype, data, role, submit, before_submit):
            draft = {**data, 'name': 'STE-release', 'doctype': doctype, 'docstatus': 0}
            before_submit(draft)
            self.native[key] = {**draft, 'docstatus': 1}
            quantities.update(FG=5, Hold=7)
            raise TimeoutError('Lost transfer response')

        e.write = Mock(side_effect=post)
        self.assertIsNone(flow.attempt('sync:B', 'QA', lambda: flow.sync(self.lot)))
        self.assertNotIn('B', flow.state.get('synced', {}))
        e, flow = self.load()
        e.write = Mock()
        flow.balance = lambda lot, warehouse, role: quantities[warehouse]
        flow.verify_row = Mock()
        doc, = self.native.values()
        for changed in ({'version':'other-version'}, {'id':'another-inspection'}, {'qms_source':'other-site'}):
            doc['remarks'] = json.dumps({**approved, **changed})
            self.assertIsNone(flow.attempt('sync:B', 'QA', lambda: flow.sync(self.lot)))
            self.assertNotIn('B', flow.state.get('synced', {}))
            self.assertFalse(any(move.get('done') for move in flow.state['moves'].values()))
            flow.verify_row.assert_not_called()
        doc['remarks'] = json.dumps(approved)
        self.assertTrue(flow.attempt('sync:B', 'QA', lambda: flow.sync(self.lot)))
        e.write.assert_not_called()
        self.assertEqual(flow.verify_row.call_count, 2)
        self.assertEqual(flow.available(self.lot, 'WAREHOUSE'), (5, approved))

    def test_mes_dispatch_reconciles_ack_before_reconsidering_revision(self):
        req = {'key': 'work', 'work_order': 'WO'}
        self.flow.state['work']['work'] = {'key': 'work', 'request': req}
        self.engine.persist()
        e, flow = self.load()
        self.authority.find_dispatch.return_value = {'id': 'MES-1', 'version': '1', 'request': req}
        work = flow.state['work']['work']
        flow.dispatch(work)
        self.authority.dispatch.assert_not_called()
        self.authority.definition.assert_not_called()
        self.assertEqual(work['dispatch'], {'id': 'MES-1', 'version': '1'})

    def test_uncertain_submit_with_new_hold_keeps_same_operation_pending(self):
        self.release()
        self.shipping(self.engine, self.flow, fail='timeout')
        with self.assertRaises(TimeoutError):
            self.flow.ship('order', {'item': 'FG'})
        draft, = self.native.values()
        # The original submit is still in flight when reconciliation reads the draft.
        draft['docstatus'] = 0
        self.authority.disposition.return_value = decision('B', 0, 'hold')
        e, flow = self.load()
        self.shipping(e, flow)
        flow.fulfillment()
        ship, = flow.state['shipments'].values()
        self.assertTrue(ship['submit_started'])
        self.assertFalse(ship.get('abandoned'))
        self.assertIn('delivery-reconciliation:' + ship['key'], flow.state['pending'])
        self.assertEqual(len(self.native), 1)
        e.invoice_delivery.assert_not_called()

    def test_future_and_unacknowledged_plm_revision_never_read_erp_bom(self):
        self.engine.api = {'PLANNER': Mock()}
        revision = {'item': 'FG', 'id': 'REV', 'version': 'V', 'erp_bom': 'BOM',
                    'approval_id': 'APP', 'erp_ack_id': 'ERP', 'mes_ack_id': 'MES',
                    'effective_from': '2027-01-05', 'approved': True, 'acknowledged': True}
        self.authority.definition.return_value = revision
        with self.assertRaisesRegex(Pending, 'effective period'):
            self.flow.definition('FG')
        revision.update(effective_from='2027-01-01', acknowledged=False)
        with self.assertRaisesRegex(Pending, 'acknowledged effective'):
            self.flow.definition('FG')
        self.engine.api['PLANNER'].get.assert_not_called()

    def test_multi_lot_readback_matches_batches_instead_of_child_order(self):
        e, flow = self.engine, self.flow
        flow.verify_row = Mock()
        shipment = {'key': 'D', 'day': 3, 'sales_order': 'SO', 'sales_order_item': 'SO-row',
                    'item': 'FG', 'rows': [{'batch': 'B', 'qty': 7}, {'batch': 'C', 'qty': 3}]}
        doc = {'name': 'DN', 'items': [
            {'name': batch, 'batch_no': batch, 'against_sales_order': 'SO',
             'so_detail': 'SO-row', 'item_code': 'FG'} for batch in ('C', 'B')
        ]}
        with self.assertRaisesRegex(Pending, 'release evidence'):
            flow.confirm_shipment(shipment, doc)
        doc['custom_norvale_quality_releases'] = json.dumps(shipment['rows'])
        flow.confirm_shipment(shipment, doc)
        self.assertEqual([call.args[1]['name'] for call in flow.verify_row.call_args_list], ['B', 'C'])
        self.assertEqual(e.state['physical']['D']['qty'], 10)
