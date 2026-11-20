"""Operating participants for new company runs."""
from datetime import date
from functools import cached_property
from hashlib import sha256
import json
from math import isfinite
from xmlrpc.client import Fault, ProtocolError

from integrations.plm import Blocked, PLM
from integrations.revisions import RevisionMismatch
from integrations.qms.adapter import ACTION, CASE, INSPECTION, OP, QMS, STATES
from integrations.qms.rework import PH_LIMITS, proposed_salvage
from integrations.qms.sample_reference import INCOMING
from native.company import save
from native.company_erp import Pending, digest
from native.production_profiles import ADAPTER, production_batch, registered
from .authority import qms_target
from .definition_change import change_actors, change_scope
from .demo import Demo, Mes
from .primary_sample import PrimarySample
from .quality import lot_specification
from .registered_lab import RegisteredLab, authenticated_actor

LOW, HIGH = PH_LIMITS
ADJUSTMENT = 0.65


def variation(seed, identity, channel):
    key = json.dumps([seed, identity, channel], separators=(',', ':')).encode()
    return int.from_bytes(sha256(key).digest()[:4], 'big') / 2**32


def supplier_reading(seed, lot, identity, epoch):
    """Weekly STOCKTAKE quality transitions and a lot-specific incoming assay."""
    week = (date.fromisoformat(lot['received_on']) - date.fromisoformat(epoch)).days // 7
    if week < 0:
        raise Pending('Supplier receipt predates the operating calendar')
    state, age = 'in_control', 0
    for period in range(week + 1):
        draw = variation(seed, [lot['supplier'], period], 'supplier-quality')
        previous = state
        if state == 'in_control':
            if draw < .04:
                state = 'drifting'
        elif state == 'drifting':
            hazard = min(1, .05 + .04 * age)
            if draw < hazard:
                state = 'out_of_control'
            elif draw < hazard + .20:
                state = 'in_control'
        elif draw < .25:
            state = 'in_control'
        age = age + 1 if state == previous else 0
    accept, marginal = {'in_control': (.90, .09), 'drifting': (.30, .50),
                        'out_of_control': (.05, .25)}[state]
    scope = [lot['supplier'], identity, lot['receipt_index']]
    band = variation(seed, scope, 'incoming-band')
    center = 5.45 if band < accept else 5.95 if band < accept + marginal else 6.25
    return center + (variation(seed, scope, 'incoming-level') - .5) * .12


class Operating(Demo):
    quality_method = 'NORVALE-LOT-QC-1'
    incoming_parameter = INCOMING

    def __init__(self, engine):
        super().__init__(engine)
        if 'production_batch' in self.cfg:
            production_batch(self.cfg['production_batch'])

    def commission(self, plm, pk, bom, date):
        meta = plm._meta(pk)
        if meta.get('norvale_approval'):
            return
        actors = self.cfg.get('plm_commissioning')
        if not actors:
            raise Pending('Configure independently credentialed PLM commissioning')
        review = meta.get('norvale_review')
        evidence = 'Commissioning ERP BOM ' + bom
        if not review:
            PLM(**actors['engineering']).review(pk, on=date, evidence=evidence)
        elif review['actor'] != actors['engineering']['decision_actor']:
            raise Pending('Unapproved legacy draft requires a native engineering review')
        PLM(**actors['approver']).approve(pk, on=date, effective=date, evidence=evidence)

    def definitions(self):
        """Commission new items and reconcile approved changes before planning."""
        items = set(self.cfg['products'])
        items.update(order['item'] for order in self.e.state['ops']['orders'].values()
                     if not order.get('production_profile'))
        flow = self.e.company_erp

        def reconcile(item, *, addon):
            try:
                if addon:
                    scope = change_scope(self.e.run, item)
                    if scope is None:
                        return
                    cfg, product = flow.authority.route(item)
                    change_actors(scope, cfg, product)
                    plm, bridge, _ = flow.authority.family_clients(cfg, product)
                    plm.require_change_guard()
                    key = json.dumps([cfg['source'], product['family'], item])
                    confirmed = flow.state.setdefault('definition_changes', {}).setdefault(key, {})
                else:
                    self.publish(item)
                    product, plm, bridge, _ = flow.authority.clients(item)
                    confirmed = self.cfg['products'][item].setdefault('confirmed_changes', {})
                on = self.e.date()
                for part in plm._family(product['family']):
                    pk = part['pk']
                    if pk == product.get('publication'):
                        continue
                    meta = plm._meta(pk)
                    approval = meta.get('norvale_approval')
                    if not approval or approval['on'] > on:
                        continue
                    if str(pk) in confirmed:
                        continue
                    if addon:
                        if not meta.get('norvale_proposal'):
                            continue
                        definition = plm.definition(pk)
                        if (definition['name'] != item or definition['revision_of'] != product['family']
                                or definition.get('material_kind', 'finished') != product.get('material_kind', 'finished')):
                            raise Blocked('Approved proposal differs from the registered production family')
                    elif meta.get('norvale_proposal'):
                        plm.require_change_guard()
                    bridge.publish(pk, on=on)
                    receipts = plm._receipts(plm.publication(pk), on)
                    source = {'source': cfg['source'], 'family': product['family']} if addon else {}
                    self.e.log('definition_published', item=item, part=pk, receipts=receipts, **source)
                    confirmed[str(pk)] = receipts
                    if addon:
                        self.e.persist()
                    else:
                        save(self.path, self.cfg)
            except (Blocked, RevisionMismatch, Fault, ProtocolError, PermissionError,
                    ValueError, KeyError, TypeError) as exc:
                raise Pending('PLM definition handoff pending: ' + str(exc)) from exc

        addons = {system.removeprefix('production-') for system in registered(self.e.run)}
        for item in sorted(items | addons):
            flow.attempt('definition-publication:' + item, 'PLANNER',
                         lambda: reconcile(item, addon=item in addons))

    def parameter(self, lot):
        return self.cfg['lab']['parameter'] if lot['kind'] in ('finished', 'intermediate') else self.incoming_parameter

    def component_specification(self, code):
        return self.quality_method + ' / ' + code

    @staticmethod
    def output_quantity(work):
        return work.get('production_target', work['qty'])

    def local_work(self, work):
        source = work.get('revision', {}).get('mes_source')
        return super().local_work(work) and source in (None, self.cfg['mes']['source_id'])

    def scheduled(self, work):
        source = work.get('revision', {}).get('mes_source')
        if source:
            try:
                cfg, product = self.e.company_erp.authority.route(work['item'], source, pinned=True)
                if cfg is product:
                    return SiteOperator(self.e, cfg) if 'mes_operator' in cfg else None
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise Pending('Pinned production profile is unavailable') from exc
        return self if self.local_work(work) else None

    def start(self, work, *, scheduled=False, intent=None):
        try:
            return super().start(work, scheduled=scheduled, intent=intent)
        except Pending:
            raise
        except (Fault, ProtocolError, RuntimeError, PermissionError, KeyError, TypeError, ValueError) as exc:
            raise Pending('Native MES start could not be confirmed') from exc

    def complete(self, work, rows):
        try:
            labor = work.get('scheduled_labor')
            if labor is None:
                raise Pending('Operating MES completion requires its original scheduled labor')
            result = super().complete(work, rows, labor=labor)
            if result:
                work['mes_execution_confirmed'] = True
                self.e.persist()
            return result
        except Pending:
            raise
        except (Fault, ProtocolError, RuntimeError, PermissionError, KeyError, TypeError, ValueError) as exc:
            raise Pending('Native MES completion could not be confirmed') from exc

    def eligible_samples(self):
        lots = super().eligible_samples(include_manual=False)
        flow = self.e.company_erp
        systems = registered(self.e.run)
        rework_inputs = {op['request']['batch'] for op in flow.state.get('rework', {}).values()}
        for batch, lot in flow.state['lots'].items():
            if lot.get('rework_key'):
                operation = flow.state['rework'][lot['rework_key']]
                if operation.get('retest'):
                    lots[batch] = lot
            elif batch not in rework_inputs and systems:
                manufactured = lot['kind'] in ('finished', 'intermediate')
                if manufactured and 'production-' + lot['item'] not in systems:
                    continue
                lab = flow.attempt('operating-lab-scope:' + batch, 'LAB',
                                   lambda: self.registered_lab(lot, discovery=True))
                if lab is False:
                    lots.pop(batch, None)
                elif lab:
                    lots[batch] = lot
        return lots

    def registered_lab(self, lot, *, discovery=False):
        flow = self.e.company_erp
        authority = getattr(flow, 'authority', None)
        if not hasattr(authority, 'quality_route'):
            return None
        output = lot if lot.get('rework_key') else None
        try:
            if output:
                operation = flow.state['rework'][lot['rework_key']]
                lot = flow.state['lots'][operation['request']['batch']]
            manufactured = lot['kind'] in ('finished', 'intermediate')
            if not manufactured and lot['item'] in self.cfg.get('specifications', {}):
                return None
            cfg, product = authority.quality_route(lot, operating=True)
            external = cfg.get('adapter') == ADAPTER or (manufactured and cfg is product)
            if lot['item'] in cfg.get('recurring_lab', {}).get('assays', {}):
                return RegisteredLab(self, lot, cfg, QMS, output=output)
            if external:
                if discovery:
                    return False  # Exclude manual lots even when a primary BOM requests their inputs.
                raise Pending('Registered production lot requires manual QMS handling')
            return None
        except Pending:
            raise
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise Pending('Registered lab scope could not be verified') from exc

    def completed_work(self, lot):
        source = lot.get('revision', {}).get('mes_source')
        lot_source, separator, _ = lot.get('mes_lot_id', '').rpartition(':stock.lot:')
        if separator and source and source != lot_source:
            raise Pending('Manufactured sample has conflicting MES source identities')
        def report_source(report):
            native_lot = report.get('output', {}).get('lot_id', '')
            prefix, separator, _ = str(native_lot).rpartition(':stock.lot:')
            return report.get('source', prefix if separator else None)

        matches = [op for op in self.e.company_erp.state['completions'].values()
                   if op.get('done') and op['report']['id'] == lot['completion_id']
                   and report_source(op['report']) == (lot_source if separator else source)
                   and op['report'].get('version') == lot.get('completion_version')]
        if len(matches) != 1:
            raise Pending('Manufactured sample lacks a unique pinned MES completion')
        return matches[0]

    def identity(self, lot):
        if lot['kind'] in ('finished', 'intermediate'):
            return [self.completed_work(lot)['work'], lot['item']]
        source = (lot['source_type'], lot['source_id'])
        keys = [key for key, doc in self.e.docs.items() if (doc['doctype'], doc['name']) == source]
        if len(keys) != 1:
            raise Pending('Sample source lacks a durable ERP operation identity')
        return [keys[0], lot['item']]

    def measurement(self, lot, *, source=None):
        """Private physical model. Only the measured scalar enters QMS."""
        seed = self.e.config['seed']
        flow = self.e.company_erp
        if lot.get('rework_key'):
            op = flow.state['rework'][lot['rework_key']]
            if (not op.get('receipt') or not op.get('execution') or not op.get('retest')
                    or op['output_batch'] != lot['batch'] or op['execution'] != lot['completion_id']):
                raise Pending('No confirmed physical rework output for retest')
            original = flow.state['lots'][op['request']['batch']]
            original_identity = self.identity(original)
            if source is not None:
                original_identity = [source, original_identity]
            identity = [original_identity, 'physical-retest']
            truth = self.condition(original, identity=original_identity) - ADJUSTMENT
        else:
            identity = self.identity(lot)
            if source is not None:
                identity = [source, identity]
            truth = self.condition(lot, identity=identity)
        noise = variation(seed, identity, 'sample') * .16 - .08
        return round(truth + noise, 2)

    def condition(self, lot, *, identity=None):
        seed = self.e.config['seed']
        if identity is None:
            identity = self.identity(lot)
        if lot['kind'] == 'incoming' and self.e.config.get('supplier_quality'):
            return supplier_reading(seed, lot, identity, self.e.dated(0))
        if lot['kind'] not in ('finished', 'intermediate'):
            return 5.35 + variation(seed, identity, 'inbound') * .3
        report = self.completed_work(lot)['report']
        if not report['inputs']:
            raise Pending('Manufactured sample lacks confirmed material genealogy')
        lots = self.e.company_erp.state['lots']
        inputs = sorted((self.identity(lots[r['batch']]), r['qty'], r['uom']) for r in report['inputs'])
        mass = [r for r in inputs if r[2] == 'Kg']
        total = sum(qty for _, qty, _ in mass)
        material = (sum(qty * variation(seed, batch, 'inbound') for batch, qty, _ in mass) / total
                    if total > 0 else .5)
        process = variation(seed, [identity, inputs], 'process')
        return 5.05 + .55 * material + 1.25 * process

    def primary_sample(self, lot):
        if not lot.get('rework_key'):
            return PrimarySample(self.e, self.cfg, lot, client=QMS)

    def prepare_samples(self, lots):
        raise Pending('Ordinary samples must run through the scheduled LAB operation')

    def sample(self, batch):
        lot = self.e.company_erp.state['lots'][batch]
        lab = self.registered_lab(lot)
        if lab:
            source = [lab.cfg['qms_source'], lot.get('revision', {}).get('mes_source')]
            value = self.measurement(lot, source=source)
            if lot.get('rework_key'):
                operation = self.e.company_erp.state['rework'][lot['rework_key']]
                reader = lab.client(**lab.cfg['qms_reader'])
                reference = reader.get(INSPECTION, operation['authority']['inspection'])['reference_name']
                lab.row(reader, reference)
            else:
                reference = lab.reference()
            return dict(scope=lab.sample_scope, date=self.e.date(), value=value, reference=reference)
        primary = self.primary_sample(lot)
        value = self.measurement(lot)
        if primary:
            reference = primary.reference(self.parameter(lot))
            if lot['kind'] not in ('finished', 'intermediate'):
                specs = self.cfg.setdefault('specifications', {})
                if lot['item'] not in specs:
                    specs[lot['item']] = self.quality_method
                    save(self.path, self.cfg)
            if not primary.legacy:
                return dict(scope=primary.key, date=self.e.date(), value=value, reference=reference)
        return [self.e.date(), value]

    def retest_actor(self, role):
        lab = self.cfg['lab']
        if qms_target(lab[role]) != qms_target(self.cfg['qms_reader']):
            raise Pending('Physical retest actor must use the original QMS site')
        user = lab['analyst_user'] if role == 'analyst' else self.cfg['qms_approver']
        return authenticated_actor(QMS, lab[role], user)

    def record(self, batch, sample):
        lot = self.e.company_erp.state['lots'][batch]
        lab = self.registered_lab(lot)
        primary = None if lab else self.primary_sample(lot)
        if lab:
            lab.verify_sample(sample)
            sampled_on, value = sample['date'], sample['value']
            if sampled_on > self.e.date():
                raise Pending('Scheduled sample is dated after the business clock')
            cfg, analyst = lab.lab, lab.actor('analyst')
            parameter = lab.assay['parameter']
            low, high = lab.limits
        else:
            try:
                if primary and not primary.legacy:
                    primary.verify_sample(sample, self.e.date())
                    sampled_on, value = sample['date'], sample['value']
                else:
                    sampled_on, value = sample
                sample_date = date.fromisoformat(sampled_on).isoformat()
            except (ValueError, TypeError) as exc:
                raise Pending('Primary QMS requires a dated scheduled measurement') from exc
            if (sample_date != sampled_on or sampled_on > self.e.date() or type(value) not in (int, float)
                    or not isfinite(value) or round(value, 2) != value):
                raise Pending('Primary QMS requires a dated, finite scheduled measurement')
            cfg = self.cfg['lab']
            analyst = primary.actor('analyst') if primary else self.retest_actor('analyst')
            parameter, low, high = self.parameter(lot), LOW, HIGH
        reading = [dict(specification=parameter, numeric=1, min_value=low,
                        max_value=high, reading_1=f'{value:.2f}')]
        fields = dict(readings=reading, custom_decision_rationale='Measured lot sample against the pinned specification.')
        if lot.get('rework_key'):
            op = self.e.company_erp.state['rework'][lot['rework_key']]
            doc = (lab.inspection(analyst, op['retest'], sample) if lab
                   else analyst.get(INSPECTION, op['retest']))
            self.check_scope(lot, doc, lab)
            if not doc['docstatus']:
                if doc.get('readings') and doc['readings'][0].get('reading_1') != reading[0]['reading_1']:
                    raise Pending('Retest already has a different measurement')
                analyst.request('PUT', analyst.resource(INSPECTION, doc['name']),
                                {**fields, 'report_date': sampled_on})
        else:
            if lab:
                lab.require_own_inspection(analyst)
                reference = sample['reference']
                row = lab.row(analyst, reference)
            else:
                primary.require_own_inspection(analyst)
                reference = primary.legacy or sample['reference']
                row = primary.row(analyst, reference)
            # Proposed scope is inert until a separate QA review authorizes the submitted failure.
            salvage = self.salvage(lot, value, lab)
            doc = analyst.create(INSPECTION, lab.operation('initial') if lab else primary.operation,
                inspection_type='In Process', reference_type='Stock Entry', reference_name=reference,
                child_row_reference=row['name'], item_code=lot['item'], batch_no=batch,
                report_date=sampled_on, sample_size=1, inspected_by=cfg['analyst_user'],
                custom_spec_revision=lab.spec if lab else lot_specification(self.cfg, lot),
                custom_lot_quantity=lot['qty'], custom_lot_uom=lot['uom'],
                **(dict(custom_rework_input_batch=batch, custom_rework_salvage_quantity=salvage)
                   if salvage else {}), **fields)
        if primary:
            doc = primary.inspection(analyst, doc['name'], reference)
            self.check_scope(lot, doc)
        if not doc['docstatus']:
            doc = analyst.transition(doc['name'], 'Record')
        if primary:
            doc = primary.inspection(analyst, doc['name'], reference)
        if lab:
            doc = lab.inspection(analyst, doc['name'], sample)
        if (doc['status'] != ('Accepted' if low <= value <= high else 'Rejected')
                or doc['report_date'] != sampled_on or self.observed(lot, doc, lab) != value):
            raise Pending('QMS measurement readback differs from the sample')
        if doc['status'] == 'Rejected':
            case = analyst.create(CASE, lab.sample_operation('case') if lab else batch + ':operating-case', custom_qms_batch=doc['batch_no'],
                custom_failed_inspection=doc['name'], procedure=cfg['procedure'],
                subject='Out-of-specification lot', details=f'Sample measured {value:.2f} against {low}–{high}.')
            analyst.create(ACTION, lab.sample_operation('correct') if lab else batch + ':operating-correct', procedure=cfg['procedure'],
                custom_qms_batch=doc['batch_no'], custom_non_conformance=case['name'],
                corrective_preventive='Corrective',
                resolutions=[dict(problem='Lot sample outside specification', status='Open')])
        measured = sample if isinstance(sample, dict) else {'date': sampled_on, 'value': value}
        return dict(inspection=doc['name'], sample=measured)

    def check_scope(self, lot, doc, lab=None):
        op = self.e.company_erp.state.get('rework', {}).get(lot.get('rework_key'))
        scope = (op['authority']['batch'] if op else lot['batch'], lot['item'],
                 lab.spec if lab else lot_specification(self.cfg, lot), lot['qty'], lot['uom'])
        if (not scope[2] or tuple(doc.get(field) for field in ('batch_no', 'item_code', 'custom_spec_revision',
                'custom_lot_quantity', 'custom_lot_uom')) != scope
                or not doc.get('report_date') or doc['report_date'] > self.e.date()
                or doc.get('inspected_by') != (lab.lab if lab else self.cfg['lab'])['analyst_user']
                or doc.get('manual_inspection')):
            raise Pending('QMS record does not match the authorized sampling scope')
        if op and (doc.get('custom_retest_of'), doc.get('custom_rework_execution'),
                   doc.get('custom_rework_output_batch')) != (op['authority']['inspection'], op['execution'], lot['batch']):
            raise Pending('Retest does not identify the confirmed execution and output batch')

    def observed(self, lot, doc, lab=None):
        self.check_scope(lot, doc, lab)
        parameter = lab.assay['parameter'] if lab else self.parameter(lot)
        low, high = lab.limits if lab else (LOW, HIGH)
        if doc.get('docstatus') != 1:
            raise Pending('Quality evidence has not been submitted')
        rows = doc.get('readings', [])
        if len(rows) != 1 or tuple(rows[0].get(k) for k in ('specification', 'numeric', 'min_value', 'max_value')) != (
                parameter, 1, low, high) or rows[0].get('manual_inspection') or rows[0].get('formula_based_criteria'):
            raise Pending('QMS scope lacks a valid numeric assay')
        value = float(rows[0]['reading_1'])
        if not isfinite(value):
            raise Pending('QMS scope lacks a finite measurement')
        outcome = 'Accepted' if low <= value <= high else 'Rejected'
        if rows[0].get('status') != outcome or doc.get('status') != outcome:
            raise Pending('Native reading status and inspection outcome disagree with the measurement')
        return value

    def review(self, batch, inspection):
        flow = self.e.company_erp
        lot = flow.state['lots'][batch]
        lab = self.registered_lab(lot)
        primary = None if lab else self.primary_sample(lot)
        qualified = bool(lab or (primary and not primary.legacy))
        sample = None
        if isinstance(inspection, dict):
            if set(inspection) != {'inspection', 'sample'} or not isinstance(inspection['sample'], dict):
                raise Pending('QA requires the scheduled inspection and sample')
            sample, inspection = inspection['sample'], inspection['inspection']
            if not qualified and (set(sample) != {'date', 'value'} or type(sample['value']) not in (int, float)):
                raise Pending('QA requires the scheduled measurement')
        elif qualified:
            raise Pending('QA requires a source-qualified result')
        if not isinstance(inspection, str) or not inspection:
            raise Pending('Invalid scheduled inspection')
        retest = flow.state['rework'][lot['rework_key']] if lot.get('rework_key') else None
        if retest and inspection != retest['retest']:
            raise Pending('QA requires the retest linked to the physical rework')
        if lab:
            lab.verify_sample(sample)
            qa = lab.actor('approver')
            doc = lab.inspection(qa, inspection, sample)
            low, high = lab.limits
            signer = lab.cfg['qms_approver']
        else:
            if qualified:
                primary.verify_sample(sample, self.e.date())
            qa = primary.actor('approver') if primary else self.retest_actor('approver')
            if primary:
                reference = primary.legacy or sample['reference']
                doc = primary.inspection(qa, inspection, reference)
            else:
                doc = qa.get(INSPECTION, inspection)
            low, high = LOW, HIGH
            signer = self.cfg['qms_approver']
        value = self.observed(lot, doc, lab)
        if sample is not None and (
                value != sample['value'] or doc.get('report_date') != sample['date']):
            raise Pending('QA measurement differs from the scheduled result')
        latest = qa.list(INSPECTION, {'batch_no': doc['batch_no']}, limit=2, order='creation desc, name desc',
                         fields=('name', 'creation'))
        if not latest or latest[0]['name'] != inspection or (len(latest) == 2 and latest[0]['creation'] == latest[1]['creation']):
            raise Pending('QA review requires the current inspection')
        reviewed_on = doc['report_date']
        def decide(action):
            result = qa.transition(inspection, action)
            if lab:
                result = lab.inspection(qa, inspection, sample)
            if primary:
                result = primary.inspection(qa, inspection, reference)
            if (result.get('name') != inspection or result.get('modified_by') != signer
                    or result.get('workflow_state') != STATES[action]
                    or result.get('report_date') != reviewed_on or self.observed(lot, result, lab) != value):
                raise Pending('Native QA decision differs from the reviewed inspection or approver')
            return result['workflow_state']
        if low <= value <= high:
            return decide('Release Lot')
        cases = qa.list(CASE, {'custom_qms_batch': doc['batch_no'], 'custom_failed_inspection': inspection}, limit=2)
        if len(cases) != 1:
            raise Pending('QA requires one linked nonconformance')
        if lab:
            case = qa.get(CASE, cases[0]['name'])
            actions = qa.list(ACTION, {'custom_qms_batch': doc['batch_no'], 'custom_non_conformance': case['name']},
                              fields=(OP,), limit=2)
            if (case.get(OP) != lab.sample_operation('case') or case.get('procedure') != lab.lab['procedure']
                    or len(actions) != 1 or actions[0].get(OP) != lab.sample_operation('correct')):
                raise Pending('Registered QA requires the linked investigation and corrective action')
        salvage = self.salvage(lot, value, lab)
        if not salvage or (not lab and not self.cfg.get('rework_mes')):
            state = decide('Reject Lot')
            evidence = {'source': lab.cfg['qms_source'], 'id': inspection} if lab else inspection
            self.e.state.setdefault('operating_lot_rejections', {})[batch] = evidence
            self.e.persist()
            return state
        if doc.get('custom_rework_input_batch') != batch or doc.get('custom_rework_salvage_quantity') != salvage:
            raise Pending('Proposed scope differs from the QA work instruction')
        self.rework_request(batch, inspection)
        authority = getattr(flow, 'authority', None)
        if authority is not None:
            if not lab and qms_target(self.cfg['qms_reader']) != qms_target(self.cfg['lab']['approver']):
                raise Pending('Physical rework requires QA on the original QMS site')
            if (authority.rework_for(lot) if hasattr(authority, 'rework_for') else getattr(authority, 'rework', None)) is None:
                raise Pending('QA cannot verify the physical rework bridge')
        return decide('Authorize Rework')

    @staticmethod
    def salvage(lot, value, lab):
        if lab and not lab.physical_rework:
            return 0
        return proposed_salvage(lot, value)

    def rework_request(self, batch, inspection):
        lot = self.e.company_erp.state['lots'][batch]
        lab = self.registered_lab(lot)
        if lab and not lab.physical_rework:
            raise Pending('Registered QMS has no verified physical rework authority')
        target = lab.physical_rework if lab else self.cfg.get('rework_mes', {})
        source, model, native_id = lot['completion_id'].rsplit(':', 2)
        if (source, model) != (target.get('source_id'), 'mrp.production'):
            raise Pending('Physical rework can only act on the original local MES order')
        return dict(batch=batch, qms_batch=batch, inspection=inspection, mes_order=int(native_id))

    def rework_center(self, batch):
        state = self.e.company_erp.state
        work_order = state['lots'][batch]['work_order']
        work = [work for work in state['work'].values()
                if work.get('request', {}).get('work_order') == work_order]
        if len(work) != 1:
            raise Pending('Rework requires one original work center')
        return work[0]['equipment']

    def rework(self, batch, inspection):
        flow = self.e.company_erp
        request = self.rework_request(batch, inspection)
        resolver = getattr(flow.authority, 'rework_for', None)
        bridge = resolver(flow.state['lots'][batch]) if resolver else flow.authority.rework
        if bridge is None:
            raise Pending('Physical rework bridge is not commissioned')
        key = 'rework:' + digest(batch)
        operation = flow.state.setdefault('rework', {}).setdefault(key, {'request': request})
        if operation['request'] != request:
            raise Pending('Rework request changed after checkpoint')
        self.e.persist()
        if not operation.get('retest'):
            if not operation.get('authority'):
                flow.equipment.available(self.rework_center(batch), 'PRODUCTION')
            bridge.reconcile(flow, key, operation)
        return operation['output_batch']


class SiteOperator(Operating):
    """Use the registered site's operator and acknowledged material mapping."""

    def __init__(self, engine, cfg):
        self.e = engine
        self.cfg = dict(cfg)
        mapping = cfg['mapping'][cfg['item']]
        self.cfg['company_id'] = mapping['mes']['settings']['company_id']
        self.materials = [(mapping['erp']['output'], mapping['mes']['output'])]
        self.materials.extend((row, mapping['mes']['components'][key])
                              for key, row in mapping['erp']['components'].items())

    @cached_property
    def execution(self):
        return Mes(**self.cfg['mes_operator'])

    def product(self, item, unit):
        matches = {(native['item'], native['unit']) for erp, native in self.materials
                   if (erp['item'], erp['unit']) == (item, unit)}
        if len(matches) != 1:
            raise Pending('No unique approved MES material and UOM for ' + item)
        (product, uom), = matches
        row, = self.execution.call('product.product', 'read', [product],
                                   fields=['default_code', 'uom_id', 'company_id', 'tracking', 'is_storable', 'active'])
        if (row['id'] != product or row['default_code'] != item or row['uom_id'][0] != uom
                or self.cfg['units'].get(row['uom_id'][1]) != unit or not row['active']
                or (row['company_id'] and row['company_id'][0] != self.cfg['company_id'])
                or row['tracking'] != 'lot' or not row['is_storable']):
            raise Pending('Native material differs from the approved item, UOM or company')
        return product

    def unique(self, model, domain, values):
        return super().unique(model, domain + [['company_id', '=', self.cfg['company_id']]], values)

    def operator(self, work):
        request, revision, dispatch = (work[k] for k in ('request', 'revision', 'dispatch'))
        operator = self.execution
        original = (self.e.company_erp.state['work'][work['key']]
                    if 'production_target' in work else work)
        if (operator.source_id != revision.get('mes_source') or operator.source_id != dispatch.get('source')
                or any(request[k] != original[k] for k in ('item', 'uom', 'qty', 'revision', 'equipment', 'inputs'))):
            raise Pending('MES execution differs from the pinned dispatch scope')
        mo = operator.read('mrp.production', int(dispatch['id']))
        output = self.product(work['item'], work['uom'])
        operation, = mo['workorder_ids']
        wo = operator.read('mrp.workorder', operation)
        center = self.e.company_erp.authority.center_key(revision, wo['workcenter_id'][0])
        if (mo['id'] != int(dispatch['id']) or mo['origin'] != request['work_order']
                or mo['norvale_revision'] != dispatch['version']
                or mo['bom_id'][0] != int(revision['mes_ack_id']) or mo['product_id'][0] != output
                or mo['product_uom_id'][0] != self.materials[0][1]['unit']
                or mo['company_id'][0] != self.cfg['company_id'] or center != work['equipment']
                or wo['production_id'][0] != mo['id']
                or ('native_group' in work and mo['procurement_group_id'][0] != work['native_group'])
                or mo['location_dest_id'][0] != self.cfg['hold_location_id']
                or (mo['state'] not in ('done', 'cancel') and mo['product_qty'] != work['qty'])):
            raise Pending('Native MES order differs from the pinned work')
        stock = mo['location_src_id'][0]
        location, = operator.call('stock.location', 'read', [stock],
                                  fields=['usage', 'active', 'company_id'])
        if (location['id'] != stock or location['usage'] != 'internal' or not location['active']
                or location['company_id'] != mo['company_id'] or stock == self.cfg['hold_location_id']):
            raise Pending('Native MES source stock is outside the pinned company')
        if work.get('mes_stock_location', stock) != stock:
            raise Pending('Native MES source stock changed after execution was pinned')
        self.cfg['stock'] = stock
        if mo['state'] not in ('done', 'cancel'):
            if operator.acknowledge(int(revision['mes_ack_id']))['revision'] != dispatch['version']:
                raise Pending('Native MES BOM has changed since dispatch')
            flow = self.e.company_erp
            for row in work['inputs']:
                lot = flow.state['lots'][row['batch']]
                available, disposition = flow.available(lot, 'PRODUCTION')
                if (available + 1e-8 < row['qty'] or disposition != row['disposition']
                        or (lot['batch'], lot['item'], lot['uom'], lot.get('mes_lot_id')) != (
                            row['batch'], row['item'], row['uom'], row.get('mes_lot_id'))):
                    raise Pending('Pinned MES input no longer has released ERP stock')
                product = self.product(row['item'], row['uom'])
                if row.get('mes_lot_id'):
                    source, identifier = row['mes_lot_id'].rsplit(':stock.lot:', 1)
                    if source != operator.source_id:
                        raise Pending('Pinned input belongs to a different MES')
                    native = operator.read('stock.lot', int(identifier))
                    if native['product_id'][0] != product or native['company_id'] != mo['company_id']:
                        raise Pending('Native input differs from the pinned material or company')
            if 'mes_stock_location' not in work:
                work['mes_stock_location'] = stock
                self.e.persist()
        return operator


def offline_decision(engine, key, role, evidence, question):
    """Use explicit local role rules when provider calls are disabled."""
    if key in engine.state['decisions']:
        return engine.state['decisions'][key]['output']
    approved = False
    if role == 'SCM_MGR' and key.startswith('po-review:'):
        quote = evidence['supplier_quote']
        def lines(record):
            return sorted((row['item_code'], row['qty'], row['rate'], row['amount']) for row in record['items'])
        approved = (evidence['supplier'] == quote['supplier'] and quote['valid_till'] >= engine.date()
                    and evidence['grand_total'] == quote['grand_total'] > 0
                    and lines(evidence) == lines(quote) and bool(lines(quote))
                    and all(qty > 0 and rate > 0 for _, qty, rate, _ in lines(quote)))
    elif role == 'CFO' and key.startswith('review:'):
        approved = bool(evidence['native_balances']) and not evidence['pending_requests']
    reason = ('Dated native evidence meets the offline operating rule.' if approved
              else 'Deferred for role review outside the offline operating rule.')
    output = dict(action='approve' if approved else 'defer', reason=reason, message=reason)
    result = dict(mode='offline-operating-rule', input=dict(role=role, as_of=engine.date(),
                  evidence=evidence, question=question), output=output)
    engine.state['decisions'][key] = result
    engine.persist()
    engine.log('decision', **result)
    return output
