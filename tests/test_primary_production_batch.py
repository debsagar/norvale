"""Qualify the primary-site production clock from a commissioned dispatch."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from test_production_profiles import ERP, OperatingMES, company, generate
from integrations.company_run.operating import Operating
from integrations.mes.client import EVENT, MO, WO, Mes
from native.company_erp import CompanyERP
from native.production_profiles import FACTORY


class PrimaryMES(OperatingMES):
    def __init__(self):
        super().__init__('primary', bounded=True)
        self.work.add('WO')
        self.lose_start = self.lose_finish = True
        self.labor_rows = {}
        self.damage = None
        self.starts = []
        self.orders[1]['stock'] = 8

    def call_as(self, login, model, method, *args, **kwargs):
        if (model, method) == ('product.product', 'search'):
            assert login == 'planner'
            return [{'PRIMARY': 2, 'BULK': 3, 'PACK': 4}[args[0][0][2]]]
        if (model, method) == ('stock.lot', 'search'):
            name, product = [condition[2] for condition in args[0]]
            return [key for key, lot in self.lots.items()
                    if (lot['name'], lot['product_id'], lot['company_id']) == (name, product, 1)]
        return super().call_as(login, model, method, *args, **kwargs)

    def endpoint(self, **cfg):
        client = self.client(cfg['login'])
        client.url, client.db, client.uid = cfg['url'], cfg['db'], 8
        native_read = client.read
        native_start, native_call = client.start, client.call

        def read(model, identifier):
            row = native_read(model, identifier)
            if model == MO:
                row['location_src_id'] = [self.orders[identifier].get('stock', 8)]
                row['move_raw_ids'] = [identifier]
            if model == WO and identifier in self.reservations:
                row['state'] = self.orders[identifier]['state']
            return row

        def start(mo, rows, released):
            if mo in self.reservations:
                assert self.reservations[mo] == rows
            native_start(mo, rows, released)
            self.starts.append(mo)
            if self.lose_start:
                self.lose_start = False
                raise TimeoutError('lost native start acknowledgement')

        def complete(mo, lot, qty, rows, released, *, labor):
            assert client.uid == 8 and cfg['login'] == 'operator'
            assert labor['center'] == 4 and labor['dispatch'] == str(mo)
            assert labor['source'] == 'primary' and labor['work_order'] == 'WO'
            order = self.orders[mo]
            assert self.lots[lot]['name'] == f'NV-FG-{mo}'
            if order['state'] == 'done':
                assert not released and self.posted[mo] == (lot, qty, rows, labor)
                self.calls.append(('replay', mo))
                return mo
            assert order['state'] == 'progress' and released == [1, 2]
            assert rows == [{'lot_id': item, 'quantity': qty} for item in released]
            if qty < order['qty']:
                self.orders[mo + 1] = dict(state='confirmed', qty=order['qty'] - qty)
            order.update(state='done', qty=qty)
            self.posted[mo] = deepcopy((lot, qty, rows, labor))
            self.labor_rows[mo] = deepcopy(labor)
            self.calls.append(('post', mo))
            if self.lose_finish:
                self.lose_finish = False
                raise TimeoutError('lost native completion acknowledgement')
            return mo

        def call(model, method, *args, **kwargs):
            if (model, method) == (MO, 'norvale_start'):
                [mo], rows, released = args
                start(mo, rows, released)
                return mo
            if (model, method) == (MO, 'norvale_complete_scheduled'):
                [mo], lot, qty, rows, released, labor = args
                return complete(mo, lot, qty, rows, released, labor=labor)
            if (model, method) == (EVENT, 'read'):
                [mo], = args
                return [dict(id=mo, date_end='2027-01-01T12:01:00', user_id=[8],
                             workorder_id=[mo], norvale_scheduled_labor=self.labor_rows[mo])]
            return native_call(model, method, *args, **kwargs)

        def lines(move_ids):
            mo, = move_ids
            return [dict(lot_id=[row['lot_id']], quantity=row['quantity'])
                    for row in self.reservations[mo]]

        client.source_id = cfg['source_id']
        client.call, client.read, client._lines = call, read, lines
        client._matches_inputs = Mes._matches_inputs
        client._ref = lambda model, identifier: dict(system='mes', source_id=client.source_id,
                                                      entity=model, native_id=identifier)
        client.start = Mes.start.__get__(client)
        client.complete = Mes.complete.__get__(client)
        return client

    def completion(self, identifier):
        report = super().completion(identifier)
        report['work_orders'] = [dict(report['work'], entity=WO)]
        if self.damage == 'lot':
            report['inputs'][0]['lot']['native_id'] = 99
        if self.damage == 'source':
            report['output']['lot']['source_id'] = 'other'
        return report


class PrimaryERP(ERP):
    def list(self, doctype, filters, fields=None, **kwargs):
        return super().list(doctype, filters, **kwargs)

    def update(self, doctype, name, values):
        draft = self.docs[doctype, name]['docstatus'] == 0
        row = super().update(doctype, name, values)
        if doctype == 'Stock Entry' and draft and row['docstatus'] == 1:
            for item in row['items']:
                detail = item['name']
                warehouse = item.get('s_warehouse') or item['t_warehouse']
                outward = bool(item.get('s_warehouse'))
                qty = -item['qty'] if outward else item['qty']
                self.docs['Serial and Batch Bundle', detail] = dict(
                    docstatus=1, voucher_no=name, voucher_detail_no=detail, warehouse=warehouse,
                    item_code=item['item_code'], type_of_transaction='Outward' if outward else 'Inward',
                    entries=[dict(batch_no=item['batch_no'], qty=qty)])
                self.docs['Stock Ledger Entry', detail] = dict(
                    voucher_type=doctype, voucher_no=name, voucher_detail_no=detail,
                    warehouse=warehouse, is_cancelled=0, actual_qty=qty, serial_and_batch_bundle=detail)
        return row


class PrimaryProduction(TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.run = run = Path(stack.enter_context(TemporaryDirectory()))
        (run / 'env').mkdir()
        (run / 'env/controller-token.json').write_text('{"token":"offline"}')
        erp = self.erp = PrimaryERP()
        erp.login = lambda *args: None
        mes = self.mes = PrimaryMES()
        cfg = dict(company.DEFAULT_CONFIG, days=7, company_operating=True, company_authority=FACTORY,
                   company_timing=dict(shifts={'PRODUCTION': [[720, 722], [750, 760]], 'LAB': [[540, 600]]}, lab_minutes=30))
        company.save(run / 'config.json', cfg)
        self.settings = dict(source='primary-plm', products={'PRIMARY': {'hold_location_id': 9}},
                             mes=dict(url='http://mes.test', db='mes', login='planner', source_id='primary'),
                             stock=8, company_id=1, center=4, uoms={'Nos': 1}, units={'Units': 'Nos'})
        self.settings['operator'] = dict(self.settings['mes'], login='operator')
        self.write_settings()

        erp.docs.clear()
        erp.docs['Sales Order', 'SO'] = dict(name='SO', docstatus=1, items=[
            dict(name='SO-line', item_code='PRIMARY', qty=2, delivered_qty=0)])
        erp.docs['Work Order', 'WO'] = dict(name='WO', docstatus=1, qty=2, produced_qty=0,
                                           production_item='PRIMARY', stock_uom='Nos', bom_no='BOM-PRIMARY')
        docs = {'order': dict(doctype='Sales Order', name='SO')}
        lots = {batch: dict(batch=batch, item=item, qty=20, uom='Nos', kind='opening')
                for batch, item in [('bulk-lot', 'BULK'), ('pack-lot', 'PACK')]}
        revision = dict(erp_bom='BOM-PRIMARY', mes_ack_id='7')
        inputs = [dict(batch=lot['batch'], item=lot['item'], uom='Nos', qty=2,
                       disposition={'id': 'released', 'qty': 20}) for lot in lots.values()]
        work = dict(key='work', item='PRIMARY', qty=2, uom='Nos', equipment=4, revision=revision,
                    order='order', sales_order='SO', sales_order_item='SO-line', inputs=inputs,
                    request=dict(key='dispatch', item='PRIMARY', work_order='WO', qty=2, uom='Nos'),
                    dispatch=dict(id='1', source='primary', version='fingerprint'))
        state = dict(day=0, completed=['seed', 'recurring-setup'], docs=docs, physical={},
                     ops=dict(orders={'order': {'item': 'PRIMARY'}}, maintenance={}))
        seed = company.Engine.__new__(company.Engine)
        seed.run, seed.state = run, state
        seed.company_erp = CompanyERP(seed, None)
        seed.company_erp.state['work']['work'] = work
        seed.company_erp.state['lots'].update(lots)
        seed.company_erp.state['synced'] = {row['batch']: row['disposition'] for row in inputs}
        company.save(run / 'state.json', state)

        self.controllers = []
        original_init = company.Engine.__init__

        def fresh(engine, *args, **kwargs):
            original_init(engine, *args, **kwargs)
            self.controllers.append(engine)
            engine.wh.update(rm='RM', hold='Hold')
            engine.api = {role: erp for role in ('PLANNER', 'PRODUCTION', 'IT_ADMIN')}
            flow = engine.company_erp
            flow.disposition = lambda *args: {'id': 'released', 'qty': 20}
            flow.balance = lambda *args: 20

        def noop(*args):
            pass

        noops = dict.fromkeys(('advance_clock', 'service', 'arrivals', 'inspections', 'demand',
                              'fulfillment', 'replenishment', 'approvals', 'bank_statements',
                              'settlements', 'management'), noop)
        stack.enter_context(patch.multiple(company.Engine, __init__=fresh, **noops))
        stack.enter_context(patch('company.Stack', lambda _: SimpleNamespace(
            endpoints=lambda: {'erp': '', 'site': ''})))
        stack.enter_context(patch('company.ErpClient', return_value=erp))
        for module in ('authority', 'demo', 'maintenance'):
            stack.enter_context(patch(f'integrations.company_run.{module}.Mes', side_effect=mes.endpoint))
        stack.enter_context(patch.object(Operating, 'definitions', noop))
        stack.enter_context(patch('integrations.company_run.timing.queue_samples', noop))
        stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('network')))
        stack.enter_context(patch('time.sleep', side_effect=AssertionError('global retry')))
        stack.enter_context(redirect_stdout(StringIO()))
        old_mask = os.umask(0o077)
        stack.callback(os.umask, old_mask)

    def write_settings(self):
        company.save(self.run / 'env/company-sources.json', self.settings)

    def invoke(self):
        generate.main(['--run', str(self.run)])

    def state(self):
        return json.loads((self.run / 'state.json').read_text())['company_erp']

    def work(self):
        return self.state()['work']['work']

    def stock_entries(self):
        return self.erp.list('Stock Entry', {})

    def test_primary_without_policy_runs_one_full_operation(self):
        self.invoke()
        work = self.work()
        self.assertIs(type(Operating(self.controllers[-1]).scheduled(work)), Operating)
        self.assertNotIn('production_operations', work)
        self.assertEqual(work['produced'], 2)
        self.assertEqual(work['scheduled_labor']['intervals'], [[720, 722]])
        self.assertEqual(self.mes.calls, [('post', 1), ('replay', 1)])
        self.assertEqual(len(self.stock_entries()), 1)

    def test_primary_validates_backorder_before_continuing_in_process(self):
        self.settings['production_batch'] = dict(target=1, backorder_not_before=1)
        self.write_settings()
        production = company.Engine.production

        def damage_native_readback(engine):
            day = engine.day
            self.mes.damage = {1: 'lot', 2: 'source'}.get(day)
            if day >= 1:
                child = self.mes.orders[2]
                child['origin'] = 'wrong-work' if day == 3 else 'WO'
                child['stock'] = 88 if day == 4 else 8
            production(engine)
            if 1 <= day <= 4:
                self.assertEqual(len(self.stock_entries()), 1)
                self.assertNotIn(2, self.mes.reservations)
                self.assertEqual(len(self.mes.labor_rows), 1)

        with patch.object(company.Engine, 'production', damage_native_readback):
            self.invoke()
        self.assertEqual(len(self.controllers), 1)
        self.assertEqual(self.work()['produced'], 2)
        self.assertEqual([self.work()['production_operations'][str(i)]['completed_day'] for i in (1, 2)],
                         [0, 5])
        trace = [json.loads(row) for row in (self.run / 'trace.jsonl').read_text().splitlines()]
        for day, reason in [(1, 'different reserved lot'), (2, 'outside the pinned instance'),
                            (3, 'does not match the posted original ERP work'),
                            (4, 'differs from the pinned work, revision, stock or group')]:
            self.assertTrue(any(row['day'] == day and row['kind'] == 'company_erp_pending'
                                and reason in row['error'] for row in trace), (day, reason))
        self.assertEqual(self.mes.starts, [1, 1, 2])
        self.assertEqual(self.mes.calls, [('post', 1), ('replay', 1), ('post', 2)])
        self.assertEqual(len(self.stock_entries()), 2)

    def test_primary_split_reopens_after_lost_posting_reply(self):
        self.settings['production_batch'] = dict(target=1, backorder_not_before=1)
        self.write_settings()
        update, get = self.erp.update, self.erp.get
        posting_lost = False

        def lose_posting(doctype, name, values):
            nonlocal posting_lost
            row = update(doctype, name, values)
            if doctype == 'Stock Entry' and not posting_lost:
                posting_lost = True
                raise TimeoutError('lost ERP Manufacture acknowledgement')
            return row

        def offline_read(doctype, name):
            if posting_lost and doctype == 'Stock Entry':
                raise TimeoutError('ERP readback temporarily unavailable')
            return get(doctype, name)

        production = company.Engine.production

        def cut(engine):
            production(engine)
            if engine.day == 1:
                raise InterruptedError('reopen after native split')

        with patch.object(self.erp, 'update', side_effect=lose_posting), \
             patch.object(self.erp, 'get', side_effect=offline_read), \
             patch.object(company.Engine, 'production', cut):
            with self.assertRaisesRegex(InterruptedError, 'reopen after native split'):
                self.invoke()
        first = self.work()['production_operations']['1']
        self.assertEqual(self.mes.starts, [1, 1])
        self.assertEqual(self.mes.calls, [('post', 1), ('replay', 1)])
        self.assertEqual(first['scheduled_labor'], self.mes.labor_rows[1])
        self.assertEqual(self.erp.get('Work Order', 'WO')['produced_qty'], 1)
        self.assertEqual(self.work().get('produced', 0), 0)
        self.assertEqual(len(self.stock_entries()), 1)

        self.invoke()
        self.assertIsNot(self.controllers[0], self.controllers[1])
        work = self.work()
        self.assertEqual((work['produced'], work['used']), (2, {'bulk-lot': 2, 'pack-lot': 2}))
        self.assertEqual(self.mes.reservations[2], [{'lot_id': 1, 'quantity': 1}, {'lot_id': 2, 'quantity': 1}])
        for identifier, operation in work['production_operations'].items():
            self.assertEqual(operation['scheduled_labor'], self.mes.labor_rows[int(identifier)])
            self.assertEqual(operation['scheduled_labor']['minutes'], 1)
        self.assertEqual(len({row['event'] for row in self.mes.labor_rows.values()}), 2)
        self.assertEqual(self.mes.calls, [('post', 1), ('replay', 1), ('post', 2)])
        self.assertEqual({json.loads(row['remarks'])['completion_id'] for row in self.stock_entries()},
                         {'primary:mrp.production:1', 'primary:mrp.production:2'})
        self.assertTrue(all((row['docstatus'], row['work_order'], row['fg_completed_qty']) == (1, 'WO', 1)
                            for row in self.stock_entries()))
        self.assertEqual([item['t_warehouse'] for row in self.stock_entries()
                          for item in row['items'] if item.get('is_finished_item')], ['Hold', 'Hold'])
        self.assertEqual(len(self.stock_entries()), 2)
        self.assertEqual(len(self.state()['completions']), 2)
        self.assertEqual(len(self.mes.lots), 4)
        self.assertEqual(len(self.mes.receipts), 2)
        self.assertEqual(len(self.erp.list('Stock Ledger Entry', {})), 6)
        self.assertEqual(len(self.erp.list('Serial and Batch Bundle', {})), 6)
        self.assertEqual(len(self.erp.list('Work Order', {})), 1)
        self.assertEqual(len(self.erp.list('Sales Order', {})), 1)
        before = deepcopy((self.erp.docs, self.mes.posted, self.mes.labor_rows, self.work()))
        self.invoke()
        self.assertEqual((self.erp.docs, self.mes.posted, self.mes.labor_rows, self.work()), before)
