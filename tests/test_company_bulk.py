"""Exercise recurring planning, release and posting through the native API boundaries."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from integrations.company_run.authority import Authority
from integrations.company_run.quality import lot_specification
from native.company import Engine
from native.company_erp import CompanyERP


class BulkFlowTests(unittest.TestCase):
    def test_bulk_quarantine_packaging_shortage_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            self.exercise(Path(directory))

    def test_planner_replaces_rejected_or_withdrawn_bulk_after_restart(self):
        for outcome in ('rejected', 'withdrawn'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                self.exercise(Path(directory), outcome)

    def exercise(self, run, outcome=None):
        e = object.__new__(Engine)
        e.run, e.day, e.event = run, 0, 'production:0'
        e.company = 'Norvale'
        e.wh = dict(rm='RM', fg='FG', hold='Hold', wip='WIP')
        e.config = dict(capacity_units=12)
        e.state = dict(ops=dict(orders={'sale': {'item': 'PACK'}}, maintenance={},
                               shortage_days={}, changes={}), physical={})
        e.log = Mock()
        sales = dict(name='SO-1', docstatus=1, items=[dict(name='SO-row', item_code='PACK', qty=3, stock_uom='Nos')])
        e.observed_usage = Mock(return_value={})
        e.mail = Mock()
        api = Mock()
        e.api = {role: api for role in ('IT_ADMIN', 'PLANNER', 'QA', 'PRODUCTION')}
        authority = Authority(run)
        e.company_erp = flow = CompanyERP(e, authority)
        cfg = dict(source='plm', plm_source='plm', mes={'source_id': 'mes', 'url': 'http://mes', 'db': 'test'},
                   plm={'url': 'http://plm'}, units={'kg': 'Kg', 'Units': 'Nos'},
                   qms_reader={}, qms_approver='qa', products={
                       'BULK': dict(material_kind='intermediate', stock_step=.01, family=1,
                                    quality_method='bulk-assay', hold_location_id=20,
                                    mapping={'BULK': {'erp': {'output': {'unit': 'Kg'}}}}),
                       'PACK': dict(family=2, quality_method='pack-assay', hold_location_id=20,
                                    mapping={'PACK': {'erp': {'components': {
                                        'bulk': {'item': 'BULK'}, 'bottle': {'item': 'BOTTLE'}}}}})},
                   specifications={'RAW': 'raw-spec', 'BOTTLE': 'bottle-spec'})
        authority.settings = lambda: {k: v for k, v in cfg.items() if k != 'products'}
        profiles = {'production-' + item: {**authority.settings(), **product}
                    for item, product in cfg['products'].items()}
        def registry(path, item=None):
            return {key: profile for key, profile in profiles.items()
                    if item is None or key == 'production-' + item}
        self.addCleanup(patch.stopall)
        patch('integrations.company_run.authority.registered', side_effect=registry).start()
        patch('integrations.company_run.authority.profile', side_effect=lambda path, key, spec: ('hash', spec)).start()
        for item, profile in cfg['products'].items():
            mapping = profile['mapping'][item]
            if item == 'BULK':
                mapping['erp']['components'] = {'raw': {'item': 'RAW'}}
            mapping['mes'] = {'output': {'unit': 1 if item == 'BULK' else 2},
                              'components': {key: {'unit': 2 if key == 'bottle' else 1}
                                             for key in mapping['erp']['components']}}
        boms = {
            'BULK': dict(quantity=.75, items=[dict(item_code='RAW', stock_uom='Kg', stock_qty=1)]),
            'PACK': dict(quantity=1, items=[dict(item_code='BULK', stock_uom='Kg', stock_qty=.25),
                                            dict(item_code='BOTTLE', stock_uom='Nos', stock_qty=1)]),
        }
        plm, bridge, mes = Mock(), Mock(), Mock(source_id='mes')
        reports, dispatches, documents, balances, batches, released = {}, {}, {}, {}, {}, set()
        def pin(family, *args, **kwargs):
            item = 'BULK' if family == 1 else 'PACK'
            return dict(native_id=family, work_id=item, source_version='v1',
                        receiver_ids={'erp': item, 'mes': family})
        plm.pin.side_effect = pin
        def resolve(row, **kwargs):
            item = row['work_id']
            profile = cfg['products'][item]
            definition = dict(material_kind=profile.get('material_kind', 'finished'),
                              lines=[dict(source_id=key) for key in profile['mapping'][item]['erp']['components']])
            return dict(native_id=row['native_id'], source_version='v1', definition=definition,
                        approval=dict(effective=e.dated(0), evidence='approved'))
        plm.resolve_pin.side_effect = resolve
        bridge.installed.side_effect = lambda row, publication: ({'erp': {'output': {'item': row['work_id']}}}, None)
        bridge.release.side_effect = lambda family, *a, **kw: {'pin': pin(family)}
        def dispatch(work, **kwargs):
            result = dict(id=len(dispatches) + 1, norvale_revision='native-v1',
                          workorder_ids=[len(dispatches) + 1], location_src_id=[10, 'Warehouse'])
            return dispatches.setdefault(work, result)
        bridge.dispatch.side_effect = dispatch
        authority.family_clients = lambda *args: (plm, bridge, mes)
        authority.execution_reader = lambda *args: mes
        authority.work_center = lambda revision, **kw: int(revision['mes_ack_id'])
        authority.require_equipment = Mock()
        def mes_call(model, method, *args, **kwargs):
            if model == 'mrp.production' and method == 'search':
                match = dispatches.get(args[0][0][2])
                return [match['id']] if match else []
            return [{'id': 1, 'rounding': .01}, {'id': 2, 'rounding': 1}]
        mes.call.side_effect = mes_call

        def mes_read(model, identifier):
            if model == 'mrp.workorder':
                return {'workcenter_id': [identifier]}
            return {'state': 'done' if identifier in reports else 'confirmed'}
        mes.read.side_effect = mes_read
        mes.completion.side_effect = reports.__getitem__

        def erp_get(dt, name):
            if dt == 'BOM':
                return dict(boms[name], item=name, docstatus=1, is_active=1)
            if dt == 'Item':
                return {'stock_uom': 'Kg', 'has_batch_no': 1}
            if dt == 'UOM':
                return {'must_be_whole_number': int(name == 'Nos')}
            return batches[name]
        api.get.side_effect = erp_get
        api.list.side_effect = lambda dt, filters: [batches[filters['name']]] if filters['name'] in batches else []
        api.create.side_effect = lambda dt, data: batches.setdefault(data['batch_id'], data)
        api.call.side_effect = lambda endpoint, args: balances.get((args['batch_no'], args['warehouse']), 0)

        def write(key, dt, fields, role, submit=False, before_submit=None):
            if key in documents:
                return documents[key]
            draft = dict(copy.deepcopy(fields), name=key, doctype=dt, docstatus=0)
            if before_submit:
                before_submit(draft)
            if dt == 'Stock Entry':
                for row in draft['items']:
                    for field, sign in [('s_warehouse', -1), ('t_warehouse', 1)]:
                        if row.get(field):
                            lot_key = row['batch_no'], row[field]
                            balances[lot_key] = balances.get(lot_key, 0) + sign * row['qty']
                            self.assertGreaterEqual(balances[lot_key], 0)
            draft['docstatus'] = int(submit)
            documents[key] = draft
            return draft
        e.get = lambda key, *args: sales if key == 'sale' else documents[key]
        e.write = write
        e.recover_company_doc = lambda key, *args: documents.get(key)
        flow.verify_row = Mock()
        may_release = {}
        self.assertAlmostEqual(flow.forecast_components('PACK')['RAW'], 1 / 3)

        def receive(batch, item, uom, qty):
            flow.register(batch, item, qty, uom, 'incoming', dict(doctype='Purchase Receipt', name='PR-' + batch))
            balances[batch, 'Hold'] = qty
            may_release[batch] = item
            released.add(batch)
        def qms_release(batch, spec, qty, uom):
            if batch in released:
                return dict(name=batch, modified='signed', docstatus=1, status='Accepted',
                            workflow_state='Released', batch_no=batch, custom_spec_revision=spec,
                            custom_lot_quantity=qty, custom_lot_uom=uom)
        def qms_read(dt, batch):
            lot = e.company_erp.state['lots'][batch]
            return dict(qms_release(batch, lot_specification(cfg, lot), lot['qty'], lot['uom']),
                        report_date=e.dated(0), item_code=may_release[batch], modified_by='qa')

        def day(number):
            e.day, e.event = number, f'production:{number}'
        def ref(entity, identifier):
            return dict(system='mes', source_id='mes', entity=entity, native_id=identifier)

        def move(item, qty, lot_id, name, uom, native_move):
            return dict(item_code=item, quantity=qty, lot=ref('stock.lot', lot_id), lot_name=name,
                        uom_name=uom, record=ref('stock.move.line', native_move))
        def report(native_id, output, inputs):
            work, = [key for key, row in dispatches.items() if row['id'] == native_id]
            reports[native_id] = dict(revision='native-v1', bom=ref('mrp.bom', native_id),
                                      erp_work_ref=work, work=ref('mrp.production', native_id),
                                      output_hold_location=ref('stock.location', 20),
                                      completion_ref=f'mes:mrp.production:{native_id}', recorded_at_utc='now',
                                      output=output, inputs=inputs, backorder=None)

        with patch('integrations.company_run.authority.QMS') as qms:
            qms.return_value.release.side_effect = qms_release
            qms.return_value.get.side_effect = qms_read
            receive('raw-lot', 'RAW', 'Kg', 1)
            flow.inspections()
            e.production()
            self.assertEqual(e.state['plans'][e.event]['shortages'], ['BOTTLE'])
            bulk_work, = flow.state['work'].values()
            self.assertEqual((bulk_work['order'], bulk_work['qty'], bulk_work['uom']), (None, .75, 'Kg'))
            self.assertIsNone(documents[bulk_work['key']]['sales_order'])
            day(1)
            e.production()
            self.assertEqual(len(dispatches), 1)
            raw = move('RAW', 1, 100, 'raw-lot', 'kg', 10)
            bulk_out = move('BULK', .75, 101, 'Original MES bulk', 'kg', 11)
            report(1, bulk_out, [{**raw, 'quantity': .75}])
            day(2)
            # Exercise rejection before admitting the separate valid execution to the journal.
            malformed, = authority.completions(bulk_work['dispatch'], as_of=e.date(), role='PRODUCTION')
            flow.attempt('negative-yield', 'PRODUCTION', lambda: flow.post_completion(
                'negative-yield', {'work': bulk_work['key'], 'report': malformed}))
            self.assertFalse(flow.state['lots'].keys() - {'raw-lot'})
            self.assertTrue(any('pinned BOM yield' in row['last_error']
                                for row in flow.state['pending'].values()))
            reports[1]['inputs'] = [raw]
            day(3)
            e.production()
            bulk, = [lot for lot in flow.state['lots'].values() if lot['kind'] == 'intermediate']
            self.assertEqual(balances[bulk['batch'], 'Hold'], .75)
            self.assertEqual(balances['raw-lot', 'RM'], 0)
            may_release[bulk['batch']] = 'BULK'
            flow.inspections()
            if outcome:
                receive('replacement-raw', 'RAW', 'Kg', 1)
                if outcome == 'withdrawn':
                    released.add(bulk['batch'])
                    flow.inspections()
                    released.remove(bulk['batch'])
                else:
                    e.state.setdefault('operating_lot_rejections', {})[bulk['batch']] = 'QA-rejection'
                flow.inspections()
                e.state = json.loads((run / 'state.json').read_text())
                e.company_erp = flow = CompanyERP(e, authority)
                flow.verify_row = Mock()
                day(4)
                e.production()
                replacement, = e.state['plans'][e.event]['work']
                self.assertEqual((replacement['item'], replacement['qty']), ('BULK', .75))
                self.assertEqual(balances[bulk['batch'], 'Hold'], .75)
                e.production()
                self.assertEqual(len(flow.state['work']), 2)
                return
            receive('reserve-raw', 'RAW', 'Kg', 1)
            flow.inspections()
            e.state = json.loads((run / 'state.json').read_text())
            e.company_erp = flow = CompanyERP(e, authority)
            flow.verify_row = Mock()
            day(4)
            e.production()
            self.assertEqual(len(dispatches), 1)
            released.add(bulk['batch'])
            flow.inspections()
            self.assertEqual(balances[bulk['batch'], 'RM'], .75)
            day(5)
            e.production()
            self.assertEqual(len(dispatches), 1)
            self.assertEqual(e.state['plans'][e.event]['shortages'], ['BOTTLE'])
            receive('bottles', 'BOTTLE', 'Nos', 3)
            flow.inspections()
            day(6)
            mes.release_intermediate.side_effect = [TimeoutError('Lost transfer acknowledgement'), None]
            e.production()
            self.assertEqual(len(dispatches), 2)
            e.state = json.loads((run / 'state.json').read_text())
            e.company_erp = flow = CompanyERP(e, authority)
            flow.verify_row = Mock()
            e.production()
            packing, = [work for work in flow.state['work'].values() if work['order']]
            self.assertEqual(packing['qty'], 3)
            self.assertEqual(documents[packing['key']]['sales_order'], 'SO-1')
            self.assertEqual(mes.release_intermediate.call_count, 2)
            self.assertEqual(*mes.release_intermediate.call_args_list)
            mes.release_intermediate.assert_called_with(
                101, 'BULK', .75, 10, origin='QMS bulk release ' + bulk['batch'])
            pack_out = move('PACK', 3, 102, 'Packed lot', 'Units', 20)
            bottle = move('BOTTLE', 3, 103, 'bottles', 'Units', 22)
            consumed = {**bulk_out, 'record': ref('stock.move.line', 21)}
            report(2, pack_out, [{**consumed, 'lot': ref('stock.lot', 999)}, bottle])
            day(7)
            e.production()
            self.assertEqual(balances[bulk['batch'], 'RM'], .75)
            self.assertTrue(any('different intermediate lot' in row['last_error']
                                for row in flow.state['pending'].values()))
            report(2, pack_out, [consumed, bottle])
            e.production()
            self.assertEqual(balances[bulk['batch'], 'RM'], 0)
            self.assertEqual(balances['bottles', 'RM'], 0)
            manufacture = [v for v in documents.values() if v.get('stock_entry_type') == 'Manufacture']
            first, second = manufacture
            self.assertEqual(first['items'][-1]['batch_no'], bulk['batch'])
            self.assertEqual({r['batch_no'] for r in second['items'] if r.get('s_warehouse')}, {bulk['batch'], 'bottles'})
            self.assertEqual(json.loads(second['remarks'])['intermediate_inputs'], [
                dict(batch=bulk['batch'], mes_lot_id='mes:stock.lot:101', mes_move=ref('stock.move.line', 21),
                     erp_manufacture=first['name'], qms_release=bulk['batch'], qms_version='signed', qms_source=None)])

            before = copy.deepcopy(documents), copy.deepcopy(balances)
            e.state = json.loads((run / 'state.json').read_text())
            e.company_erp = CompanyERP(e, authority)
            e.company_erp.verify_row = Mock()
            e.production()
            self.assertEqual((documents, balances), before)
            self.assertEqual(len(dispatches), 2)
