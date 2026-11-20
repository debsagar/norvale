"""Recurring equipment faults and independent restart / batch disposition gates."""
from collections import defaultdict
from xmlrpc.client import Error as RPCError

from integrations.qms.adapter import APIError
from native.company_erp import Pending, digest


class Equipment:
    def __init__(self, flow):
        self.flow = flow
        self.e = flow.e
        self.authority = flow.authority

    @property
    def cases(self):
        return self.e.state['ops'].setdefault('equipment_faults', {})

    def legacy_open(self):
        return [(key, case) for key, case in self.e.state['ops']['maintenance'].items()
                if case.get('closed') is None and not case.get('equipment_case')]

    def available(self, center, role, *, running=False):
        if not isinstance(center, str) and self.legacy_open() and self.authority.legacy_equipment() in (None, center):
            raise Pending('Legacy maintenance awaits equipment reconciliation')
        if not center or any(case['center'] == center and not case.get('restart')
                             for case in self.cases.values()):
            raise Pending('Equipment awaits a verified restart')
        self.native(self.authority.require_equipment, center, role=role, running=running)
        return center

    @staticmethod
    def native(action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except (RPCError, APIError, OSError, RuntimeError, KeyError) as exc:
            raise Pending(str(exc)) from exc

    def observe(self, center, observation_id, reason, meter, *, fault=None):
        """Checkpoint an observed fault before writing MES or Gitea."""
        key = 'eq-' + digest([self.flow.namespace, center, observation_id])[:32]
        if key not in self.cases:
            self.cases[key] = dict(center=center, reason=reason, meter=meter, day=self.e.day, fault=fault)
            self.e.persist()
        return key

    def service(self):
        meters = defaultdict(int)
        for work in self.flow.state['work'].values():
            center = work.get('equipment')
            if type(center) is int or (isinstance(center, str) and ':' in center):
                meters[center] += work.get('produced', 0)
        for key, legacy in self.legacy_open():
            center = self.authority.legacy_equipment()
            if center:
                legacy['equipment_case'] = self.observe(
                    center, 'legacy:' + key, f'Existing maintenance issue #{legacy["ticket"]}',
                    meters.get(center, legacy['meter']))
                self.e.persist()
        self.flow.attempt('equipment-observations', 'PRODUCTION',
                          lambda: self.native(self.discover, self.authority, {
                              center: meter for center, meter in meters.items() if type(center) is int}))
        # Read the current acknowledged route even before a new site has produced work.
        centers = set(meters)
        items = getattr(type(self.authority), 'maintenance_items', None)
        for item in items(self.authority) if items else ():
            center = self.flow.attempt('equipment-route:' + item, 'PRODUCTION',
                                       lambda: self.native(self.registered_center, item))
            if isinstance(center, str):
                centers.add(center)
        sites = defaultdict(dict)
        for center in centers:
            if isinstance(center, str):
                sites[center.rpartition(':')[0]][center] = meters.get(center, 0)
        for source, site_meters in sorted(sites.items()):
            self.flow.attempt('equipment-observations:' + source, 'PRODUCTION',
                              lambda: self.native(self.discover, self.authority.maintenance_site(source), site_meters))
        for center, meter in meters.items():
            cases = [c for c in self.cases.values() if c['center'] == center]
            previous = max((c['meter'] for c in cases), default=0)
            if meter - previous >= self.e.config['maintenance_units'] and all(c.get('restart') for c in cases):
                self.observe(center, f'usage:{meter}', f'Usage limit reached at {meter} units', meter)
        for key, case in self.cases.items():
            if not case.get('restart'):
                self.flow.attempt('maintenance:' + key, 'MAINTENANCE',
                                  lambda: self.native(self.advance, key, case))
            if case.get('interrupted') and not case.get('disposition'):
                self.flow.attempt('interrupted:' + key, 'QA',
                                  lambda: self.native(self.dispose, case))

    def registered_center(self, item):
        revision = self.authority.definition(item, as_of=self.e.date(), role='PLANNER')
        return self.authority.work_center(revision, role='PRODUCTION')

    def scoped_authority(self, center):
        if isinstance(center, str):
            return self.authority.maintenance_site(center.rpartition(':')[0])
        return self.authority

    def discover(self, authority, meters):
        scoped = authority is not self.authority
        native_centers = {(authority.native_center(center) if scoped else center): center
                          for center in meters}
        known = {(c['fault'].get('source_id'), c['fault']['native_id'])
                 for c in self.cases.values() if c.get('fault')}
        # A crash can commit the fault before its readback reaches the checkpoint.
        expected = {(c['center'], f'Fault {key}') for key, c in self.cases.items()
                    if not c.get('fault') and c['center'] in meters}
        for fault in authority.observed_faults(list(native_centers), role='PRODUCTION'):
            description = fault['description'].partition('\n')[0]
            native_id = fault['workcenter_id'][0]
            center = native_centers[native_id] if scoped else native_id
            if (fault.get('source_id'), fault['native_id']) in known or (center, description) in expected:
                continue
            self.observe(center, f'mes:{fault["native_id"]}', description, meters.get(center, 0), fault=fault)
            self.e.log('mes_equipment_fault', center=center, fault=fault)

    def advance(self, key, case):
        authority = self.scoped_authority(case['center'])
        if not case.get('fault'):
            fault = authority.equipment_fault(case['center'], key, role='MAINTENANCE')
            case['fault'] = fault
            self.e.persist()
            self.e.log('mes_equipment_fault', center=case['center'], fault=fault)
        if 'interrupted' not in case:
            case['interrupted'] = authority.interrupted_batch(case['fault'], role='PRODUCTION')
            self.e.persist()
            if case['interrupted']:
                self.e.log('interrupted_batch_pending', **case['interrupted'])
        ticket = authority.maintenance_ticket(key, case, role='MAINTENANCE')
        identity = {k: ticket[k] for k in ('number', 'html_url', 'source_id') if k in ticket}
        if 'source_id' in identity and case.get('ticket', identity) != identity:
            raise Pending('Pinned maintenance ticket identity changed')
        if not case.get('ticket'):
            case['ticket'] = identity
            self.e.persist()
            self.e.log('equipment_maintenance_ticket', **case['ticket'])
        staffed = getattr(authority, 'staffed_restart', None)
        staff_evidence = staffed(self, key, case, ticket) if staffed else None
        verified = authority.restart_evidence(ticket, case, role='MAINTENANCE')
        if not verified or (staff_evidence and any(verified[k] != staff_evidence[k]
                                                    for k in ('id', 'version', 'ref'))):
            raise Pending('Diagnosis, repair and authorized restart check are pending')
        if case.get('verification') and case['verification'] != verified:
            raise Pending('Pinned restart evidence changed')
        case['verification'] = verified
        self.e.persist()
        restart = authority.equipment_restart(case, role='MAINTENANCE')
        authority.close_maintenance_ticket(key, case, role='MAINTENANCE')
        case['restart'] = restart
        self.e.persist()
        self.e.log('mes_verified_restart', center=case['center'], fault=restart, verification=verified)

    def dispose(self, case):
        authority = self.scoped_authority(case['center'])
        batch = case['interrupted']
        matches = [work for work in self.flow.state['work'].values()
                   if work.get('request', {}).get('work_order') == batch['work_order']
                   and work.get('equipment') == case['center']
                   and (authority is self.authority
                        or work.get('revision', {}).get('mes_source') == authority.source)]
        if len(matches) != 1:
            raise Pending('Interrupted MES batch has no reconciled ERP work order')
        work, = matches
        if not 0 < batch['qty'] <= work['qty'] - work.get('produced', 0):
            raise Pending('Interrupted batch quantity exceeds the unposted work remainder')
        as_of = case.get('qa_as_of', self.e.date())
        evidence = authority.interrupted_disposition(batch, work, as_of=as_of, role='QA')
        if not evidence:
            raise Pending('Interrupted batch awaits authorized QMS rejection')
        if case.get('qa_evidence') and case['qa_evidence'] != evidence:
            raise Pending('Pinned interrupted batch disposition changed')
        case['qa_evidence'] = evidence
        case['qa_as_of'] = as_of
        self.e.persist()
        if authority.interrupted_disposition(batch, work, as_of=as_of, role='QA') != evidence:
            raise Pending('QMS disposition changed before interrupted work cancellation')
        cancelled = authority.reject_interrupted(case, role='MAINTENANCE')
        case['disposition'] = cancelled
        work['rejected_remainder'] = batch['qty']
        self.e.persist()
        self.e.log('interrupted_batch_rejected', batch=batch, evidence=evidence, cancellation=cancelled)

    def guard_execution(self, completion_ref):
        for case in self.cases.values():
            batch = case.get('interrupted')
            if batch and completion_ref == batch['completion_ref']:
                raise Pending('Interrupted batch cannot enter ERP finished stock')
