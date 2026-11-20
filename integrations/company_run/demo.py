"""Commission and operate separate demo records using the engine's observed lots and orders."""
import json
import os
from pathlib import Path
import subprocess
from xmlrpc.client import Fault
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction
from functools import cached_property

from native.company import save
from native.company_erp import Pending
from integrations.mes.client import Mes
from integrations.plm import PLM
from .input_staging import stage
from .quality import inspect


class Demo:
    quality_method = 'DEMO-QC-1'
    incoming_parameter = ''

    def __init__(self, engine):
        self.e = engine
        self.path = engine.run / 'env/company-sources.json'
        self.cfg = json.loads(self.path.read_text())

    @cached_property
    def mes(self):
        return Mes(**self.cfg['mes'])

    def unique(self, model, domain, values):
        rows = self.mes.call(model, 'search', domain, limit=2)
        if len(rows) > 1:
            raise ValueError('Ambiguous native ' + model)
        return rows[0] if rows else self.mes.call(model, 'create', values)

    def product(self, item, unit):
        cfg = self.cfg
        return self.unique('product.product', [['default_code', '=', item]], dict(
            name=item, default_code=item, type='consu', is_storable=True, tracking='lot',
            company_id=cfg['company_id'], uom_id=cfg['uoms'][unit], uom_po_id=cfg['uoms'][unit]))

    def component_specification(self, code):
        return 'Acceptance specification for ' + code

    def publish(self, item):
        if item in self.cfg['products']:
            product = self.cfg['products'][item]
            if not product.get('published'):
                _, _, bridge, _ = self.e.company_erp.authority.clients(item)
                bridge.publish(product['publication'], on=self.e.date())
                product['published'] = True
                save(self.path, self.cfg)
            return
        cfg = self.cfg
        bom = self.e.api['PLANNER'].get('BOM', self.e.docs['bom:' + item]['name'])
        mes_out = self.product(item, 'Nos')
        erp = dict(output=dict(item=item, unit='Nos', qty='1000'), components={},
                   settings=dict(company=self.e.company))
        mes = dict(output=dict(item=mes_out, unit=cfg['uoms']['Nos'], qty='1000'), components={},
                   settings=dict(company_id=cfg['company_id'], operation=dict(
                       name='Fill and pack', workcenter_id=cfg.get('equipment_by_item', {}).get(item, cfg['center']), minutes='1')))
        lines = []
        for row in bom['items']:
            code, unit = row['item_code'], row['stock_uom']
            kind = 'formulation' if unit == 'Kg' else 'packaging'
            source_unit = {'Kg':'kg', 'Nos':'ea'}[unit]
            entry = dict(source_unit=source_unit, kind=kind, factor='1')
            erp['components'][code] = dict(entry, item=code, unit=unit)
            mes['components'][code] = dict(entry, item=self.product(code, unit), unit=cfg['uoms'][unit])
            lines.append(dict(source_id=code, unit=source_unit, kind=kind,
                              quantity=str(Decimal(str(row['qty'])) * 1000),
                              specification=self.component_specification(code)))
        plm = PLM(**cfg['plm'])
        roots = [p for p in plm._list('/api/part/', name=item) if p['name'] == item
                 and p['revision_of'] is None and (p['IPN'] or '').startswith('NV-PLM-')]
        if len(roots) > 1:
            raise ValueError('Ambiguous PLM product family')
        base = roots[0]['pk'] if roots and roots[0]['revision'] != 'A-1' else None
        pk = plm.draft(item, 'A-1', lines, base_id=base)
        date = self.e.date()
        self.commission(plm, pk, bom['name'], date)
        cfg['products'][item] = dict(quality_method=self.quality_method, family=base or pk, publication=pk, mapping={item: dict(erp=erp, mes=mes)},
                                     hold_location_id=cfg['hold'])
        save(self.path, cfg)
        _, _, bridge, _ = self.e.company_erp.authority.clients(item)
        bridge.publish(pk, on=date)
        cfg['products'][item]['published'] = True
        save(self.path, cfg)

    def commission(self, plm, pk, bom, date):
        plm.review(pk, on=date, evidence='Commissioning ERP BOM ' + bom)
        plm.approve(pk, on=date, effective=date, evidence='Commissioning ERP BOM ' + bom)

    def local_work(self, work):
        orders = self.e.state['ops']['orders']
        revision = work.get('revision', {})
        return not (orders.get(work.get('order'), {}).get('production_profile')
                    or (revision.get('material_kind') == 'intermediate' and revision.get('mes_source')))

    def eligible_samples(self, *, include_manual=True):
        flow = self.e.company_erp
        state = flow.state
        physical = {request['batch'] for request in flow.rework_requests()} if include_manual else set()
        physical.update(op['request']['batch'] for op in state.get('rework', {}).values())
        external = {w['request']['work_order'] for w in state['work'].values()
                    if w.get('request') and not self.local_work(w)}
        wanted = set()
        for order in self.e.state['ops']['orders'].values():
            if order.get('production_profile'):
                continue
            item = order['item']
            recipe = flow.attempt('sample-definition:' + item, 'PLANNER', lambda: flow.definition(item))
            if recipe:
                revision, components = recipe
                wanted.update(components)
                for code in revision.get('intermediate_inputs', []):
                    bulk = flow.attempt('sample-definition:' + code, 'PLANNER', lambda: flow.definition(code))
                    if bulk:
                        wanted.update(bulk[1])
            elif 'bom:' + item in self.e.docs:
                wanted.update(row['item_code'] for row in self.e.get('bom:' + item, 'PLANNER')['items'])
        return {key: lot for key, lot in state['lots'].items()
                if key not in physical and not lot.get('rework_key')
                and lot.get('work_order') not in external
                and (lot['kind'] in ('finished', 'intermediate') or lot['item'] in wanted)}

    def prepare_samples(self, lots):
        cfg = self.cfg
        pending = [lot for lot in lots if lot['batch'] not in cfg['lab']['references']]
        if pending:
            script = Path(__file__).with_name('lab_reference.py')
            command = [cfg['qms_script'], 'compose', cfg['qms_runtime'], 'exec', '-T',
                       '-e', 'COMPANY_LOTS']
            environment = dict(os.environ, COMPANY_LOTS=json.dumps(pending))
            if self.incoming_parameter:
                command.extend(['-e', 'COMPANY_INCOMING_PARAMETER'])
                environment['COMPANY_INCOMING_PARAMETER'] = self.incoming_parameter
            command.extend(['qms', 'bash', '-c', 'cd sites && ../env/bin/python -'])
            output = subprocess.check_output(command, input=script.read_bytes(), env=environment)
            cfg['lab']['references'].update(json.loads(output.splitlines()[-1]))
            for lot in pending:
                if lot['kind'] != 'finished':
                    cfg['specifications'].setdefault(lot['item'], self.quality_method)
            save(self.path, cfg)
    def samples(self, batch=None):
        cfg = self.cfg
        lots = self.eligible_samples()
        if batch is not None:
            if batch not in lots:
                return
            lots = {batch: lots[batch]}
        self.prepare_samples(lots.values())
        assays = self.e.state.setdefault('acceptance_assays', {})
        for lot in lots.values():
            batch = lot['batch']
            prior = assays.get(batch)
            if prior and (prior['state'] != 'Rework Authorized' or prior['day'] >= self.e.day):
                continue
            fail = lot['kind'] == 'finished' and not prior and not any(
                x['state'] == 'Rework Authorized' or x.get('retested') for x in assays.values())
            doc = inspect(cfg, lot, self.e.date(), failed=fail, prior=prior['id'] if prior else None)
            assays[batch] = dict(id=doc['name'], state=doc['workflow_state'], day=self.e.day,
                                 retested=bool(prior))
            self.e.persist()
            if (self.e.run / 'interrupt-after-release').exists() and doc['workflow_state'] == 'Released':
                save(self.e.run / 'release-interruption.json', dict(batch=batch, inspection=doc['name'],
                     as_of=self.e.date(), confirmed_moves=[m['target_id'] for m in
                     self.e.company_erp.state['moves'].values() if m['batch']==batch and m.get('done')]))
                (self.e.run / 'interrupt-after-release').unlink()
                raise InterruptedError('Acceptance interruption after native QMS release, before ERP sync')

    def receive_mes_lot(self, work, inp, lot, product, native):
        stage(self, work, inp, lot, product, native)

    def operator(self, work):
        operator = Mes(**self.cfg['operator'])
        if work['dispatch'].get('source', operator.source_id) != operator.source_id:
            raise Pending('MES operator source differs from dispatch')
        if 'production_target' in work:
            mo = operator.read('mrp.production', int(work['dispatch']['id']))
            stock = mo['location_src_id'][0]
            if (stock != self.cfg['stock'] or work.get('mes_stock_location', stock) != stock
                    or mo['origin'] != work['request']['work_order']
                    or mo['norvale_revision'] != work['dispatch']['version']
                    or str(mo['bom_id'][0]) != str(work['revision']['mes_ack_id'])
                    or ('native_group' in work and mo['procurement_group_id'][0] != work['native_group'])):
                raise Pending('Native production differs from the pinned work, revision, stock or group')
            if 'mes_stock_location' not in work:
                work['mes_stock_location'] = stock
                self.e.persist()
        return operator

    def start(self, work, *, scheduled=False, intent=None):
        cfg = self.cfg
        operator = self.operator(work)
        flow = self.e.company_erp
        mo = int(work['dispatch']['id'])
        state = operator.read('mrp.production', mo)['state']
        if state == 'done' and 'production_target' in work:
            raise Pending('Native completion requires reconciliation of its scheduled operation')
        if state in ('done', 'cancel'):
            return []
        flow.equipment.guard_execution(f'{operator.source_id}:mrp.production:{mo}')
        flow.equipment.available(work['equipment'], 'PRODUCTION', running=state in ('progress', 'to_close'))
        rows = []
        pinned = work.get('mes_start') if intent is not None else None
        for index, inp in enumerate(work['inputs']):
            lot = flow.state['lots'][inp['batch']]
            if flow.disposition(lot, 'PRODUCTION') != inp['disposition']:
                raise Pending('Pinned input disposition changed before MES start')
            if inp.get('mes_lot_id'):
                source, identifier = inp['mes_lot_id'].rsplit(':stock.lot:', 1)
                if source != operator.source_id:
                    raise Pending('Intermediate is from a different native MES')
                native = int(identifier)
            else:
                prod = self.product(inp['item'], inp['uom'])
                native = self.unique('stock.lot', [['name', '=', inp['batch']], ['product_id', '=', prod]],
                    dict(name=inp['batch'], product_id=prod, company_id=cfg['company_id']))
                if pinned is not None and native != pinned['inputs'][index]['lot_id']:
                    raise Pending('Native input lot changed after start was pinned')
                self.receive_mes_lot(work, inp, lot, prod, native)
            rows.append(dict(lot_id=native, quantity=inp['qty']))
        released = self.released_inputs(work, rows)
        if scheduled:
            self.start_execution(operator, mo, rows, released, work=work, intent=intent)
        else:
            flow.equipment.native(self.start_execution, operator, mo, rows, released)
        return rows

    def start_execution(self, operator, mo, rows, released, *, work=None, intent=None):
        production = operator.read('mrp.production', mo)
        operation, = production['workorder_ids']
        if operator.read('mrp.workorder', operation)['state'] == 'waiting':
            # Receipts posted after MO confirmation do not refresh Odoo's material readiness.
            try:
                operator.call('mrp.production', 'action_assign', [mo])
            except Fault as exc:
                if 'cannot marshal None' not in exc.faultString:
                    raise
        if intent is not None:
            # Lot staging above has its own reconciliation and cannot start the work order.
            pinned = dict(intent, operation=operation, inputs=rows)
            previous = work.get('mes_start')
            if previous is None:
                work['mes_start'] = pinned
                self.e.persist()
            elif previous != pinned:
                raise Pending('Native start identity or inputs changed during reconciliation')
        # The native start revalidates and reserves only the exact authorized lots.
        operator.start(mo, rows, released)

    def released_inputs(self, work, rows):
        flow = self.e.company_erp
        released = [row['lot_id'] for row, inp in zip(rows, work['inputs'], strict=True)
                    if flow.disposition(flow.state['lots'][inp['batch']], 'PRODUCTION') == inp['disposition']]
        if len(released) != len(rows):
            raise Pending('Pinned input disposition changed before MES execution')
        return released

    def output_quantity(self, work):
        # The qualification fixture proves partial execution and the native backorder.
        revision = work['revision']
        if revision.get('material_kind') == 'intermediate' or revision.get('intermediate_inputs'):
            return work['qty']
        half = work['qty'] // 2
        if not half:
            return work['qty']
        totals = defaultdict(Fraction)
        for row in work['inputs']:
            totals[row['item'], row['uom']] += Fraction(str(row['qty']))
        for (_, unit), total in totals.items():
            partial = total * half / Fraction(str(work['qty']))
            if partial.denominator != 1 and self.e.api['PRODUCTION'].get('UOM', unit)['must_be_whole_number']:
                return work['qty']
        return half

    @staticmethod
    def allocate_consumption(work, rows, qty):
        # Consume from pinned lots in reservation order, respecting component totals.
        if qty == work['qty']:
            return rows
        allocations = list(zip(work['inputs'], rows, strict=True))
        remaining = defaultdict(Fraction)
        fraction = Fraction(str(qty)) / Fraction(str(work['qty']))
        for pin, row in allocations:
            remaining[pin['item']] += Fraction(str(row['quantity'])) * fraction
        consumed = []
        for pin, row in allocations:
            item = pin['item']
            used = min(remaining[item], Fraction(str(row['quantity'])))
            if used:
                consumed.append(dict(lot_id=row['lot_id'], quantity=float(used)))
                remaining[item] -= used
        return consumed

    def complete(self, work, rows, *, labor=None):
        cfg = self.cfg
        flow = self.e.company_erp
        operator = self.operator(work)
        mo = int(work['dispatch']['id'])
        production = operator.read('mrp.production', mo)
        state = production['state']
        started = work.get('mes_start')
        if started and (started['source'] != operator.source_id
                        or started['dispatch'] != work['dispatch']['id']
                        or production['workorder_ids'] != [started['operation']]):
            raise Pending('Native operation changed after start')
        if state == 'cancel':
            return False
        if state != 'done':
            flow.equipment.guard_execution(f'{operator.source_id}:mrp.production:{mo}')
            flow.equipment.available(work['equipment'], 'PRODUCTION', running=True)
            released = self.released_inputs(work, rows)
        else:
            # Native completion verifies replay against its posted lot and movements.
            released = []
        intent = work.get('mes_consumption')
        output = intent.get('output') if labor is not None and intent else None
        if output is None:
            item = self.product(work['item'], work['uom'])
            name = ('NV-BULK-' if work['revision'].get('material_kind') == 'intermediate' else 'NV-FG-') + str(mo)
            output = self.unique('stock.lot', [['name', '=', name], ['product_id', '=', item]],
                dict(name=name, product_id=item, company_id=cfg['company_id']))
        if intent is None:
            if state == 'done':
                # Checkpoints created before lot allocation was journaled used proportional draws.
                qty = (work['qty'] // 2 if labor is None and
                       not (work['revision'].get('material_kind') == 'intermediate' or
                            work['revision'].get('intermediate_inputs')) else work['qty'])
                actual = [dict(lot_id=r['lot_id'], quantity=r['quantity'] * qty / work['qty']) for r in rows]
            else:
                qty = self.output_quantity(work)
                actual = self.allocate_consumption(work, rows, qty)
                intent = work['mes_consumption'] = dict(qty=qty, inputs=actual)
                if labor is None:
                    self.e.persist()
        else:
            qty, actual = intent['qty'], intent['inputs']
        if labor is None:
            operator.complete(mo, output, qty, actual, released)
        else:
            if intent is None or 'output' not in intent:
                work['mes_consumption'] = dict(qty=qty, inputs=actual, output=output)
                self.e.persist()
            operator.complete(mo, output, qty, actual, released, labor=labor)
        return True

    def execute(self):
        """Resume qualification checkpoints created before minute scheduling."""
        flow = self.e.company_erp
        for work in flow.state['work'].values():
            if not self.local_work(work) or not work.get('dispatch') or work.get('rejected_remainder'):
                continue
            rows = flow.attempt('mes-start:' + work['key'], 'PRODUCTION', lambda: self.start(work))
            if rows:
                self.complete(work, rows)
