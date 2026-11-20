"""Translate staged CompanyERP handoffs into scoped native application calls."""
from collections import defaultdict
from functools import cached_property, wraps
import json
from math import isclose
from xmlrpc.client import Error as RPCError, Fault, ProtocolError
from pathlib import Path

from integrations.mes.client import Mes
from integrations.plm import Blocked, PLM
from integrations.qms.adapter import APIError, QMS, INSPECTION, eligible
from integrations.revisions import Bridge, RevisionMismatch
from integrations.revisions.receivers import ERPNext, Odoo
from native.company_erp import Pending, quantity, stock_steps
from native.production_profiles import profile, quality_target, registered
from norvale.loader.erp_client import ErpClient
from .maintenance import MaintenanceAuthority, SiteMaintenance
from .quality import finished_specification, lot_specification
from .registered_lab import authenticated_actor


def qms_target(settings):
    return settings['url'].rstrip('/'), settings.get('site', 'qms.localhost')


def handoff(*roles):
    def decorate(method):
        @wraps(method)
        def call(self, *args, role, **kwargs):
            if role not in roles:
                raise Pending('Role is not authorized for the native handoff')
            try:
                return method(self, *args, **kwargs)
            except Pending:
                raise
            except (Blocked, RevisionMismatch, APIError, Fault, ProtocolError,
                    PermissionError, ValueError, KeyError, TypeError) as exc:
                raise Pending(f'Native evidence unavailable for {method.__name__}') from exc
        return call
    return decorate


class Authority(MaintenanceAuthority):
    def __init__(self, run):
        self.run = Path(run)

    def settings(self):
        path = self.run / 'env/company-sources.json'
        return json.loads(path.read_text()) if path.exists() else {}

    @cached_property
    def rework(self):
        cfg = self.settings()
        if 'rework_mes' not in cfg:
            return None
        from integrations.qms.rework import ReworkQuality
        from native.company_rework import Rework

        target, original = cfg['rework_mes'], cfg['mes']
        if (target['url'].rstrip('/'), target['db'], target['source_id']) != (
                original['url'].rstrip('/'), original['db'], original['source_id']):
            raise Pending('Rework bridge must use the original MES deployment')
        lab = cfg['lab']
        if qms_target(lab['analyst']) != qms_target(cfg['qms_reader']):
            raise Pending('Physical rework requires the same QMS site as the original inspection')
        quality = ReworkQuality(QMS(**cfg['qms_reader']), QMS(**lab['analyst']),
                                lab['analyst_user'], cfg['qms_approver'])
        try:
            mes = Mes(**target)
        except (RPCError, OSError) as exc:
            raise Pending('Could not authenticate the MES rework bridge') from exc
        return Rework(mes, quality, cfg['uoms'], qms_source=cfg.get('qms_source'))

    def rework_for(self, lot):
        try:
            cfg, product = self.route(lot['item'], lot.get('revision', {}).get('mes_source'),
                                      required=False, pinned=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Pending('Could not resolve the pinned physical rework scope') from exc
        if cfg is not product:
            return self.rework
        if not cfg.get('physical_rework'):
            return None

        from integrations.qms.rework import ReworkQuality, rework_assay
        from native.company_rework import Rework

        lab = cfg['recurring_lab']
        assay = rework_assay(lab, lot['item'])
        if not assay:
            return None
        output = cfg['mapping'][lot['item']]
        uoms = {output['erp']['output']['unit']: output['mes']['output']['unit']}
        try:
            mes = Mes(**cfg['physical_rework'])
            if not mes.call('res.users', 'has_group', [mes.uid], 'norvale_mes.group_rework_bridge'):
                raise Pending('Registered MES identity lacks QMS rework bridge authority')
        except (RPCError, OSError, PermissionError) as exc:
            raise Pending('Could not verify the registered MES rework bridge') from exc
        try:
            analyst = authenticated_actor(QMS, lab['analyst'], lab['analyst_user'])
        except APIError as exc:
            raise Pending('Could not verify the registered QMS rework analyst') from exc
        quality = ReworkQuality(QMS(**cfg['qms_reader']), analyst, lab['analyst_user'],
                                cfg['qms_approver'], assay=assay, procedure=lab['procedure'])
        return Rework(mes, quality, uoms, qms_source=cfg['qms_source'])

    def quality_route(self, lot, *, operating=False):
        item = lot['item']
        cfg, product = self.route(item, lot.get('revision', {}).get('mes_source'),
                                  required=False, pinned=lot.get('kind') in ('finished', 'intermediate'))
        if product is None and item not in cfg.get('specifications', {}):
            # Unpinned inbound materials can belong to an add-on. Shared raw
            # materials need a single inspection authority.
            candidates = []
            for system, spec in registered(self.run).items():
                _, candidate = profile(self.run, system, spec)
                if item in candidate['specifications']:
                    candidates.append(candidate)
            if candidates:
                cfg = candidates[0]
                def scope(candidate):
                    manual = (candidate['qms_reader'], candidate['qms_approver'],
                              candidate['specifications'][item])
                    if not operating:
                        return manual
                    lab = candidate.get('recurring_lab', {})
                    assay = lab.get('assays', {}).get(item)
                    return (*manual, assay, {key: value for key, value in lab.items()
                                             if key != 'assays'} if assay else None)
                if any(scope(other) != scope(cfg) for other in candidates[1:]):
                    raise Pending('Ambiguous QMS authority for inbound item')

        return cfg, product

    @handoff('QA', 'PLANNER', 'WAREHOUSE', 'PRODUCTION')
    def disposition(self, lot, *, as_of):
        cfg, _ = self.quality_route(lot)
        spec = lot_specification(cfg, lot)
        if not spec:
            return None
        qms = QMS(**cfg['qms_reader'])
        row = qms.release(lot['batch'], spec, lot['qty'], lot['uom'])
        if not row:
            return None
        doc = qms.get(INSPECTION, row['name'])
        if not doc.get('report_date') or doc['report_date'] > as_of:
            return None
        if (doc['modified'] != row['modified'] or doc['item_code'] != lot['item']
                or not eligible([doc], lot['batch'], spec, lot['qty'], lot['uom'])
                or doc['modified_by'] != cfg['qms_approver']):
            raise Pending('Inspection scope or approver changed')
        return dict(batch=lot['batch'], item=lot['item'], status='released', id=row['name'],
                    version=row['modified'], approver=doc['modified_by'], evidence=row['name'],
                    authorized=True, uom=lot['uom'], qty=lot['qty'],
                    **({'qms_source': cfg['qms_source']} if cfg.get('qms_source') else {}))

    def release_transfer_source(self, erp):
        cfg = self.settings()
        target = cfg.get('erp', {})
        if (target.get('base', '').rstrip('/'), target.get('site')) != (erp.base.rstrip('/'), erp.site):
            raise Pending('Release transfer ERP instance differs from the configured receiver')
        source = cfg.get('erp_source') or (cfg['source'] + '-erp' if cfg.get('source') else None)
        if not source:
            raise Pending('Release transfer requires a registered ERP source')
        return source

    @handoff('WAREHOUSE')
    def dispatch_lease(self, request, *, outcome):
        if request.get('kind', 'dispatch') != 'dispatch':
            raise Pending('Dispatch requires a Delivery Note lease')
        return self._lease(request, outcome)

    @handoff('QA')
    def release_transfer_lease(self, request, *, outcome, commit=None):
        if request.get('kind') != 'release_transfer':
            raise Pending('Quarantine release requires a Stock Entry lease')
        return self._lease(request, outcome, commit)

    def _lease(self, request, outcome, commit=None):
        scopes = []
        credential = None
        for row in request['scopes']:
            lot, decision = row['lot'], row['disposition']
            if request.get('kind') == 'release_transfer':
                cfg, _ = self.quality_route(lot)
            else:
                cfg, _ = self.route(lot['item'], lot.get('revision', {}).get('mes_source'),
                                    required=False, pinned=True)
            spec = lot_specification(cfg, lot)
            dispatcher = cfg.get('qms_dispatcher')
            if not dispatcher and not (cfg.get('physical_rework') or 'mes_operator' in cfg):
                dispatcher = self.settings().get('qms_dispatcher')
            if (not dispatcher or qms_target(dispatcher) != qms_target(cfg['qms_reader'])
                    or (credential is not None and credential != dispatcher)):
                raise Pending('Dispatch requires one dedicated QMS Dispatcher and one QMS site')
            if not spec or decision.get('qms_source') != cfg.get('qms_source'):
                raise Pending('Dispatch lease requires the pinned inspection specification and QMS source')
            credential = dispatcher
            batch = decision['qms_batch'] if lot.get('rework_key') else lot['batch']
            scopes.append(dict(batch=batch, item=lot['item'], revision=spec, uom=lot['uom'],
                               qty=decision['qty'], dispatch_qty=row['dispatch_qty'],
                               inspection=decision['id'], version=decision['version'],
                               approver=decision['approver']))
            if lot.get('rework_key') and request.get('kind') == 'release_transfer':
                scope = scopes[-1]
                scope.update(output_batch=lot['batch'], retest_of=decision['retest_of'],
                             rework_execution=decision['rework_execution'], report_date=decision['report_date'])
                if decision.get('spec_revision') != spec:
                    raise Pending('Retest specification differs from the pinned lot specification')
        scopes.sort(key=lambda scope: scope['batch'])
        return QMS(**credential).dispatch_lease({**request, 'scopes': scopes}, outcome=outcome,
                                                **({'commit': commit} if commit else {}))

    def route(self, item, source=None, *, required=True, pinned=False):
        system = 'production-' + item
        spec = registered(self.run, item).get(system)
        if spec:
            _, cfg = profile(self.run, system, spec)
            product = cfg
        else:
            cfg = self.settings()
            product = cfg.get('products', {}).get(item)
        if (source is not None or (pinned and cfg is product)) and source != cfg.get('mes', {}).get('source_id'):
            raise Pending('Pinned MES source does not match registered family')
        if product is None and required:
            raise Pending('No registered PLM family for ' + item)
        return cfg, product

    def clients(self, item):
        cfg, product = self.route(item)
        return product, *self.family_clients(cfg, product)

    def family_clients(self, cfg, product):
        if product is cfg:
            erp = ErpClient(**cfg['erp'])
        else:
            endpoints = json.loads((self.run / 'env/endpoints.json').read_text())
            base, site = endpoints['erp'], endpoints['site']
            if (cfg['erp']['base'], cfg['erp']['site']) != (base, site):
                raise Pending('Primary revision receiver differs from the commissioned ERP site')
            roles = json.loads((self.run / 'env/role-tokens.json').read_text())
            erp = ErpClient(base=base, site=site, token=roles['PLANNER'])
            if erp.call('frappe.auth.get_logged_user', {}) != 'planner@norvale.test':
                raise Pending('Primary revision receiver requires the native planner')
        plm = PLM(**cfg['plm'])
        mes = Mes(**cfg['mes'])
        bridge = Bridge(plm, ERPNext(erp), Odoo(mes),
                        source=cfg['source'], mapping=product['mapping'],
                        checkpoint=self.run / 'revision-pins.sqlite',
                        scoped_receipts=cfg.get('scoped_receipts', False))
        return plm, bridge, mes

    def recipe(self, item, cfg, product, *, as_of):
        plm, bridge, mes = self.family_clients(cfg, product)
        pin = plm.pin(product['family'], 'planning:' + item, on=as_of)
        publication = plm.resolve_pin(pin, work_id=pin['work_id'], on=as_of)
        return pin, publication, bridge, mes

    def verify_profile(self, cfg, *, as_of, company):
        """Registration reads installed masters and approved receiver records."""
        item = cfg['item']
        try:
            pin, publication, bridge, mes = self.recipe(item, cfg, cfg, as_of=as_of)
            plans, installed = bridge.installed(pin, publication)
            erp_plan = plans['erp']
            client = bridge.receivers['erp'].client
            intermediate = cfg.get('material_kind') == 'intermediate'
            if intermediate != (publication.get('definition', {}).get('material_kind') == 'intermediate'):
                raise Pending('Registered material kind differs from the approved revision')
            if not intermediate:
                customer = client.get('Customer', cfg['customer'])
                if customer['name'] != cfg['customer'] or customer.get('disabled'):
                    raise Pending('Registered customer is unavailable')
            item_record = client.get('Item', item)
            location, = mes.call('stock.location', 'read', [cfg['hold_location_id']],
                                 fields=['usage', 'active', 'company_id'])
            if (erp_plan['output']['item'] != item or erp_plan['settings']['company'] != company
                    or not item_record.get('has_batch_no') or item_record.get('has_serial_no')
                    or location['id'] != cfg['hold_location_id'] or location['usage'] != 'internal'
                    or not location['active'] or (location['company_id'] and
                        location['company_id'][0] != plans['mes']['settings']['company_id'])):
                raise Pending('Native production master scope differs from the approved profile')
            # Do not replace a legacy family or silently let two site endpoints
            # use the same MES source ID.
            legacy = self.settings()
            if item in legacy.get('products', {}):
                raise Pending('Item already has a legacy production family')
            if legacy.get('mes'):
                self.check_sources(cfg, legacy)
                if cfg.get('maintenance') and cfg['mes']['source_id'] == legacy['mes']['source_id']:
                    raise Pending('Primary MES equipment uses the existing primary maintenance scope')
            if 'mes_reader' in cfg:
                from .lake import check_mes_reader, registered_mes_scan

                mes_plan = plans['mes']
                reader = registered_mes_scan(cfg, mes_plan['output']['item'],
                                             mes_plan['settings']['company_id'], legacy.get('mes_analytics'))
                marker = installed['mes'].get('receiver_marker')
                if marker is not None and marker != reader['instance']:
                    raise Pending('MES analytics instance differs from the installed revision receiver')
                check_mes_reader(Mes(**reader['reader']), reader)
        except (Blocked, RevisionMismatch, APIError, Fault, ProtocolError,
                PermissionError, KeyError, TypeError) as exc:
            raise Pending('Installed production profile could not be verified') from exc

    @staticmethod
    def check_sources(cfg, other):
        left, right = cfg['mes'], other['mes']
        same_id = left['source_id'] == right['source_id']
        same_target = (left['url'].rstrip('/'), left['db']) == (right['url'].rstrip('/'), right['db'])
        if same_id != same_target:
            raise Pending('MES source IDs must identify one deployment each')
        if 'item' in other and same_id and cfg.get('maintenance') != other.get('maintenance'):
            raise Pending('Profiles on the same MES deployment must share maintenance authority')
        def plm_source(settings):
            return settings.get('plm_source') or (settings['source'] if 'item' in settings
                                                  else settings['source'] + '-plm')

        if (plm_source(cfg) == plm_source(other)) != (
                cfg['plm']['url'].rstrip('/') == other['plm']['url'].rstrip('/')):
            raise Pending('PLM source IDs must identify one deployment each')
        if cfg.get('qms_source') and other.get('qms_source'):
            if (cfg['qms_source'] == other['qms_source']) != (quality_target(cfg) == quality_target(other)):
                raise Pending('QMS source IDs must identify one deployment each')

    @handoff('PLANNER')
    def definition(self, item, *, as_of):
        cfg, product = self.route(item)
        pin, publication, bridge, mes = self.recipe(item, cfg, product, as_of=as_of)
        addon = cfg is product
        plans, _ = bridge.installed(pin, publication)
        if plans['erp']['output']['item'] != item:
            raise Pending('Approved definition targets a different native item')
        assay = product.get('quality_method') or cfg.get('specifications', {}).get(item)
        if not assay:
            raise Pending('No approved finished-product inspection method')
        inspection = finished_specification(item, assay, cfg['source'], publication)
        metadata = self.material_metadata(item, cfg, product, publication, mes)
        return dict(**metadata, item=item, inspection_spec=inspection, erp_bom=pin['receiver_ids']['erp'], approved=True,
                    acknowledged=True, effective_from=publication['approval']['effective'],
                    id=str(pin['native_id']), version=pin['source_version'],
                    approval_id=str(publication['approval']['evidence']), erp_ack_id=pin['receiver_ids']['erp'],
                    mes_ack_id=str(pin['receiver_ids']['mes']),
                    **({'receiver_markers': pin['receiver_markers']} if 'receiver_markers' in pin else {}),
                    **({'mes_source': mes.source_id, 'plm_source': cfg['plm_source']} if addon
                       else {'plm_source': cfg['plm_source']} if cfg.get('plm_source') else {}))

    def material_metadata(self, item, cfg, product, publication, mes):
        kind = product.get('material_kind', 'finished')
        definition = publication.get('definition', {})
        if kind not in ('finished', 'intermediate') or kind != definition.get('material_kind', 'finished'):
            raise Pending('Profile material kind differs from the approved revision')
        mapping = product.get('mapping', {}).get(item, {})
        erp = mapping.get('erp', {})
        component_mapping = erp.get('components', {})
        components = {line['source_id']: component_mapping[line['source_id']]
                      for line in definition.get('lines', []) if line['source_id'] in component_mapping}
        bulk = []
        if kind != 'intermediate' and components:
            registered_products = registered(self.run)
            local_products = self.settings().get('products', {})
            for line in components.values():
                code = line['item']
                system = 'production-' + code
                peer_cfg = cfg
                if system in registered_products:
                    _, peer_cfg = profile(self.run, system, registered_products[system])
                    peer = peer_cfg
                else:
                    peer = local_products.get(code, {})
                    peer_cfg = self.settings()
                if peer.get('material_kind') == 'intermediate':
                    self.check_sources(cfg, peer_cfg)
                    if peer_cfg['mes']['source_id'] != mes.source_id:
                        raise Pending('Bulk and packing must share the same native MES')
                    bulk.append(code)
        if kind != 'intermediate' and not bulk:
            return {}
        native = mapping['mes']
        inputs = native['components']
        units = list({native['output']['unit'], *(inputs[key]['unit'] for key in components)})
        rounding = {row['id']: quantity(row['rounding']) for row in
                    mes.call('uom.uom', 'read', units, fields=['rounding'])}
        metadata = {'input_steps': {line['item']: rounding[inputs[key]['unit']]
                                   for key, line in components.items()}}
        if kind == 'intermediate':
            step = quantity(product.get('stock_step'))
            if (step < 1e-8 or stock_steps(step, 1e-8) != step
                    or stock_steps(step, rounding[native['output']['unit']]) != step):
                raise Pending('Intermediate stock increment conflicts with native UOM precision')
            metadata.update(material_kind=kind, stock_uom=erp['output']['unit'], stock_step=step)
        else:
            metadata['intermediate_inputs'] = sorted(set(bulk))
        return metadata

    def execution_reader(self, cfg, product):
        if cfg is product:
            return Mes(**cfg['mes'])
        reader = self.operator('PRODUCTION')
        if (reader.source_id, reader.url, reader.db) != (
                cfg['mes']['source_id'], cfg['mes']['url'].rstrip('/'), cfg['mes']['db']):
            raise Pending('MES operator has a different source from dispatch')
        return reader

    def center_key(self, revision, native_id):
        source = revision.get('mes_source')
        legacy_source = self.settings().get('mes', {}).get('source_id')
        return f'{source}:{native_id}' if source and source != legacy_source else native_id

    def maintenance_items(self):
        return [system.removeprefix('production-') for system in registered(self.run)]

    def maintenance_site(self, source):
        for system, spec in registered(self.run).items():
            try:
                _, cfg = profile(self.run, system, spec)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if cfg['mes']['source_id'] == source:
                return SiteMaintenance(self, cfg)
        raise Pending('No authorized maintenance scope matches the pinned MES source')

    def work_center(self, revision, *, role):
        source = revision.get('mes_source')
        if not source:
            return super().work_center(revision, role=role)
        return self.family_work_center(revision, role=role)

    @handoff('PLANNER', 'PRODUCTION')
    def family_work_center(self, revision):
        cfg, product = self.route(revision['item'], revision['mes_source'])
        mes = self.execution_reader(cfg, product)
        bom, = mes.call('mrp.bom', 'read', [int(revision['mes_ack_id'])], fields=['operation_ids'])
        operation, = bom['operation_ids']
        op, = mes.call('mrp.routing.workcenter', 'read', [operation], fields=['workcenter_id'])
        return self.center_key(revision, op['workcenter_id'][0])

    def require_equipment(self, center, *, role, running=False):
        if not isinstance(center, str):
            return super().require_equipment(center, role=role, running=running)
        return self.require_family_equipment(center, role=role, running=running)

    @handoff('PLANNER', 'PRODUCTION')
    def require_family_equipment(self, center, *, running):
        source, native_id = center.rsplit(':', 1)
        for system, spec in registered(self.run).items():
            _, cfg = profile(self.run, system, spec)
            if cfg['mes']['source_id'] == source:
                equipment = Mes(**cfg['mes']).availability(int(native_id))
                if equipment['working_state'] == 'blocked' or (not running and not equipment['available']):
                    raise Pending('MES equipment is blocked or occupied')
                return
        raise Pending('No MES equipment deployment matches the pinned source')

    def work(self):
        state = json.loads((self.run / 'state.json').read_text())
        return state['company_erp']['work'].values()

    @handoff('PRODUCTION')
    def find_dispatch(self, key):
        request, = [w['request'] for w in self.work() if w.get('request', {}).get('key') == key]
        cfg, product = self.route(request['item'], request['revision'].get('mes_source'), pinned=True)
        mes = self.execution_reader(cfg, product)
        ids = mes.call('mrp.production', 'search', [['origin', '=', request['work_order']],
                       ['backorder_sequence', 'in', [0, 1]], ['norvale_revision', '!=', False]], limit=2)
        if len(ids) > 1:
            raise Pending('Ambiguous MES dispatch')
        return self.dispatch(request, role='PRODUCTION') if ids else None

    @handoff('PRODUCTION')
    def dispatch(self, request):
        revision = request['revision']
        cfg, product = self.route(request['item'], revision.get('mes_source'), pinned=True)
        _, bridge, mes = self.family_clients(cfg, product)
        work = request['work_order']
        released_on = request.get('released_on', request['as_of'])
        pin = dict(work_id=work, released_on=released_on, native_id=int(revision['id']),
                   source_version=revision['version'],
                   receiver_ids={'erp':revision['erp_ack_id'], 'mes':revision['mes_ack_id']},
                   **({'receiver_markers': revision['receiver_markers']} if 'receiver_markers' in revision else {}))
        saved = bridge.release(product['family'], work, on=released_on, installed=cfg is product, pinned=pin)
        if (str(saved['pin']['native_id']) != revision['id']
                or saved['pin']['source_version'] != revision['version']
                or {k: str(v) for k, v in saved['pin']['receiver_ids'].items()}
                != {'erp': revision['erp_ack_id'], 'mes': revision['mes_ack_id']}):
            raise Pending('PLM pin changed before work release')
        center = self.legacy_equipment() if request['equipment'] == 'Filler' else request['equipment']
        if self.work_center(revision, role='PRODUCTION') != center:
            raise Pending('Acknowledged MES route differs from pinned equipment')
        row = bridge.dispatch(work, on=request['as_of'], quantity=request['qty'],
                              hold_location_id=product['hold_location_id'])
        reader = self.execution_reader(cfg, product)
        centers = {self.center_key(revision, reader.read('mrp.workorder', wo)['workcenter_id'][0])
                   for wo in row['workorder_ids']}
        if centers != {center}:
            raise Pending('MES dispatch work center differs from pinned equipment')
        bulk = [inp for inp in request['inputs'] if inp.get('mes_lot_id')]
        if bulk:
            lots = json.loads((self.run / 'state.json').read_text())['company_erp']['lots']
        for inp in bulk:
            lot = lots[inp['batch']]
            current = self.disposition(lot, as_of=request['as_of'], role='PRODUCTION')
            if (lot['kind'] != 'intermediate' or inp['mes_lot_id'] != lot['mes_lot_id']
                    or current is None or current != inp['disposition'] or current['qty'] != lot['qty']):
                raise Pending('Intermediate lot is not fully released for MES staging')
            source, native = lot['mes_lot_id'].rsplit(':stock.lot:', 1)
            if source != mes.source_id:
                raise Pending('Intermediate lot belongs to another MES instance')
            mes.release_intermediate(int(native), inp['item'], lot['qty'], row['location_src_id'][0],
                                     origin='QMS bulk release ' + inp['batch'])
        return dict(id=str(row['id']), version=row['norvale_revision'], request=request, source=mes.source_id)

    @handoff('PRODUCTION')
    def completions(self, dispatch, *, as_of):
        work, = [w for w in self.work() if w.get('dispatch') == dispatch]
        source = dispatch.get('source', work['revision'].get('mes_source'))
        cfg, product = self.route(work['item'], source, pinned=True)
        operations = work.get('production_operations')
        if operations is None and not work.get('mes_execution_confirmed'):
            if 'mes_operator' in cfg or work.get('mes_start'):
                return []
            if work.get('scheduled_labor') and not work.get('produced'):
                if json.loads((self.run / 'config.json').read_text()).get('company_operating'):
                    return []
        mes = self.execution_reader(cfg, product)
        def native_id(ref, entity):
            if (not isinstance(ref, dict) or ref.get('source_id') != mes.source_id
                    or ref.get('system') != 'mes' or ref.get('entity') != entity
                    or type(ref.get('native_id')) is not int or ref['native_id'] <= 0):
                raise Pending('MES completion contains a reference outside the pinned instance')
            return ref['native_id']

        mo = int(dispatch['id'])
        reports = []
        seen = set()
        while (operations is None or 'completed_day' in operations.get(str(mo), {})) and (
                mes.read('mrp.production', mo)['state'] == 'done'):
            if mo in seen:
                raise Pending('Cyclic MES backorder chain')
            seen.add(mo)
            report = mes.completion(mo)
            if (native_id(report['work'], 'mrp.production') != mo
                    or report['completion_ref'] != f'{mes.source_id}:mrp.production:{mo}'
                    or report['erp_work_ref'] != work['request']['work_order']
                    or native_id(report['output_hold_location'], 'stock.location') != product['hold_location_id']
                    or report['revision'] != dispatch['version']
                    or native_id(report['bom'], 'mrp.bom') != int(work['revision']['mes_ack_id'])):
                raise Pending('MES completion differs from the pinned execution, work order, hold or BOM')
            if operations is not None:
                operation = operations[str(mo)]
                intent = operation['mes_consumption']
                reserved = {row['lot_id']: pin for row, pin in zip(
                    operation['mes_start']['inputs'], operation['inputs'], strict=True)}
                consumed = defaultdict(float)
                for row in report['inputs']:
                    identifier = native_id(row['lot'], 'stock.lot')
                    pin = reserved.get(identifier)
                    if not pin or (not pin.get('mes_lot_id') and row['lot_name'] != pin['batch']):
                        raise Pending('MES completion consumed a different reserved lot')
                    consumed[identifier] += quantity(row['quantity'])
                if (native_id(report['output']['lot'], 'stock.lot') != intent['output']
                        or report['output']['quantity'] != intent['qty']
                        or len(consumed) != len(intent['inputs'])
                        or any(row['lot_id'] not in consumed or not isclose(
                            row['quantity'], consumed[row['lot_id']], rel_tol=0, abs_tol=1e-9)
                               for row in intent['inputs'])):
                    raise Pending('MES completion differs from the pinned lot allocation')
            units = cfg['units']
            pinned = {r['mes_lot_id']: r['batch'] for r in work['inputs'] if r.get('mes_lot_id')}

            def movement(row, *, component=False):
                native_id(row['lot'], 'stock.lot')
                native_id(row['record'], 'stock.move.line')
                result = dict(batch=row['lot_name'], item=row['item_code'],
                              qty=row['quantity'], uom=units[row['uom_name']])
                if component and row['item_code'] in work['revision'].get('intermediate_inputs', []):
                    native = f"{mes.source_id}:stock.lot:{row['lot']['native_id']}"
                    if native not in pinned:
                        raise Pending('MES consumed a different intermediate lot')
                    result.update(batch=pinned[native], mes_lot_id=native, mes_move=row['record'])
                return result
            output = movement(report['output'])
            output['lot_id'] = f"{mes.source_id}:stock.lot:{report['output']['lot']['native_id']}"
            reports.append(dict(id=report['completion_ref'], version=report['recorded_at_utc'],
                       work_order=work['request']['work_order'], dispatch_id=dispatch['id'],
                       revision=work['revision'], authorized=True, output=output,
                       inputs=[movement(row, component=True) for row in report['inputs']],
                       **({'source': source} if 'source' in dispatch else {})))
            if not report['backorder']:
                break
            mo = native_id(report['backorder'], 'mrp.production')
        return reports
