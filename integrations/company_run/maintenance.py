"""Native role-scoped evidence for the recurring maintenance workflow."""
from datetime import date, datetime
import os
import re

from integrations.mes.client import CENTER, EVENT, MO, WO, Mes
from integrations.qms.adapter import CASE, INSPECTION, OP, QMS
from integrations.qms.measurements import numeric_results
from native.company_erp import Pending, digest


class MaintenanceAuthority:
    def legacy_equipment(self):
        return self.settings().get('center')

    def operator(self, role):
        if role not in ('PLANNER', 'PRODUCTION'):
            raise PermissionError('MES operational reads require planning or production')
        config = self.settings().get('operator')
        if not config:
            raise Pending('MES operator reader is unavailable')
        return Mes(**config)

    def maintenance_mes(self, role):
        if role != 'MAINTENANCE':
            raise PermissionError('MES fault mutations require maintenance')
        config = self.settings().get('maintenance_mes')
        if not config:
            raise Pending('MES maintenance identity is unavailable')
        return Mes(**config)

    def work_center(self, revision, *, role):
        mes = self.operator(role)
        bom, = mes.call('mrp.bom', 'read', [int(revision['mes_ack_id'])], fields=['operation_ids'])
        if len(bom['operation_ids']) != 1:
            raise Pending('Expected one operation on the acknowledged MES route')
        op, = mes.call('mrp.routing.workcenter', 'read', bom['operation_ids'], fields=['workcenter_id'])
        if not op['workcenter_id']:
            raise Pending('Acknowledged MES route has no equipment')
        return op['workcenter_id'][0]

    def require_equipment(self, center, *, role, running=False):
        equipment = self.operator(role).availability(center)
        if equipment['working_state'] == 'blocked' or (not running and not equipment['available']):
            raise Pending('MES equipment is blocked or occupied')

    def observed_faults(self, centers, *, role):
        mes = self.operator(role)
        centers = sorted(set(centers) | set(self.settings().get('equipment_centers', [])))
        ids = mes.call(EVENT, 'search', [['workcenter_id', 'in', centers],
                       ['loss_type', '=', 'availability'], ['date_end', '=', False],
                       ['description', '=like', 'Fault %']])
        return [{**mes.read(EVENT, identifier), **mes._ref(EVENT, identifier)} for identifier in ids]

    def equipment_fault(self, center, key, *, role):
        fault = self.maintenance_mes(role).fault(self.native_center(center), key)
        return self.operator('PRODUCTION').read(EVENT, fault['native_id']) | {
            k: fault[k] for k in ('source_id', 'native_id', 'source_url')
        }

    def interrupted_batch(self, fault, *, role):
        mes = self.operator(role)
        if fault.get('source_id', mes.source_id) != mes.source_id:
            raise Pending('Interrupted fault belongs to another MES deployment')
        if not fault['norvale_interrupted_id']:
            return None
        wo = mes.read(WO, fault['norvale_interrupted_id'][0])
        mo = mes.read(MO, wo['production_id'][0])
        if wo['workcenter_id'][0] != fault['workcenter_id'][0]:
            raise Pending('Interrupted operation does not match fault equipment')
        completion = f'{mes.source_id}:{MO}:{mo["id"]}'
        item = mes.read('product.product', mo['product_id'][0])['default_code']
        return dict(batch='NV-WIP-' + digest(completion)[:24], completion_ref=completion,
                    work_order=mo['origin'], mo=mo['id'], qty=mo['product_qty'],
                    item=item,
                    uom=self.interrupted_settings(item)['units'][mo['product_uom_id'][1]])

    def interrupted_settings(self, item):
        return self.settings()

    def native_center(self, center):
        return center

    def tickets(self, role, *, token=None):
        if role != 'MAINTENANCE':
            raise PermissionError('Maintenance ticket access requires maintenance')
        from native.systems_v2 import Tickets
        target = self.settings().get('maintenance_tickets')
        if target:
            token = token or target['token']
            if not token:
                raise Pending('Site maintenance ticket identity is unavailable')
            return Tickets(self.run, token=token, target=target)
        return Tickets(self.run, token=token)

    @staticmethod
    def ticket_fields(case):
        fault = case['fault']
        batch = case['interrupted']
        title = f'MES equipment {case["center"]} fault'
        body = f'{case["reason"]}\nMES fault {fault["source_url"]}'
        if batch:
            body += (f'\nInterrupted batch {batch["batch"]} ({batch["work_order"]}); QA disposition required.'
                     f'\nMES completion identity {batch["completion_ref"]}; scope '
                     f'{batch["qty"]} {batch["uom"]} of {batch["item"]}.')
        return title, body

    def maintenance_ticket(self, key, case, *, role):
        ticket = self.tickets(role).open(key, *self.ticket_fields(case), allow_closed=True)
        target = self.settings().get('maintenance_tickets')
        if target:
            ticket = {**ticket, 'source_id': target['url'].rstrip('/') + '/' + target['repo']}
        return ticket

    def staffed_restart(self, equipment, key, case, ticket):
        from .maintenance_staff import ACTORS, Staff, usage_fault

        cfg = self.settings()
        staff = cfg.get('maintenance_staff')
        if not usage_fault(equipment.flow.namespace, key, case):
            return None
        if not staff:
            return None
        if (set(staff) != set(ACTORS) or not all(staff.values())
                or len(set(staff.values())) != len(ACTORS)
                or cfg.get('maintenance_tickets', {}).get('token') in staff.values()):
            raise Pending('Independent maintenance actors are unavailable')
        return Staff(equipment, key, case, ticket, authority=self).advance()

    def restart_evidence(self, ticket, case, *, role):
        cfg = self.settings()
        history = case.get('staff_history', [])
        if history and history[-1]['stage'] == 'verification':
            from .maintenance_staff import ACTORS
            verifier = ACTORS['verifier']
            reader_token = cfg.get('maintenance_staff', {}).get('reader')
        else:
            verifier = cfg.get('restart_verifier')
            reader_token = cfg.get('maintenance_ticket_reader')
        if not verifier or verifier == 'controller' or not reader_token:
            return None
        tickets = self.tickets(role, token=reader_token)
        fault = case['fault']['source_url']
        prefix = f'Verified restart: {fault}\n'
        comments = tickets.comments(ticket['number'])
        matches = [row for row in comments if row.get('user', {}).get('login') == verifier
                   and (row.get('body') or '').startswith(prefix)]
        if len(matches) != 1:
            return None
        comment = matches[0]
        lines = comment['body'][len(prefix):].splitlines()
        fields = ('Diagnosis: ', 'Repair: ', 'Test: ', 'Result: PASS')
        if (len(lines) != len(fields) or lines[-1] != fields[-1]
                or any(not line.startswith(label) or not line[len(label):].strip()
                       for line, label in zip(lines[:-1], fields[:-1]))
                or datetime.fromisoformat(comment['created_at']) < datetime.fromisoformat(ticket['created_at'])):
            return None
        return dict(id=comment['id'], actor=verifier, version=comment['updated_at'],
                    body=comment['body'], ref=f'{ticket["html_url"]}#issuecomment-{comment["id"]}')

    def equipment_restart(self, case, *, role):
        mes = self.maintenance_mes(role)
        ref = 'gitea:' + digest(case['verification']['ref'])
        restart = mes.restart(self.native_center(case['center']), case['fault']['native_id'], ref)
        reader = self.operator('PRODUCTION')
        event = reader.read(EVENT, restart['native_id'])
        center = reader.read(CENTER, self.native_center(case['center']))
        if (not event['date_end'] or event['description'] != restart['description']
                or center['working_state'] == 'blocked'):
            raise Pending('Verified equipment restart is not visible in MES')
        return restart

    def close_maintenance_ticket(self, key, case, *, role):
        title, body = self.ticket_fields(case)
        self.tickets(role).close(case['ticket']['number'],
                                 'MES restart verified by ' + case['verification']['ref'],
                                 key=key, title=title, body=body)

    def interrupted_disposition(self, batch, work, *, as_of, role):
        if role != 'QA':
            raise PermissionError('Interrupted batch disposition requires QA')
        cfg = self.interrupted_settings(batch['item'])
        revision = work.get('revision', {}).get('inspection_spec')
        if not revision or work['item'] != batch['item'] or work['uom'] != batch['uom']:
            return None
        if not cfg.get('qms_reader'):
            return None
        qms = QMS(**cfg['qms_reader'])
        rows = qms.list(INSPECTION, {'batch_no': batch['batch']}, limit=2, order='creation desc, name desc',
                        fields=('name', 'modified', 'creation'))
        if not rows or (len(rows) == 2 and rows[0]['creation'] == rows[1]['creation']):
            return None
        doc = qms.get(INSPECTION, rows[0]['name'])
        try:
            inspected = date.fromisoformat(doc['report_date'])
            released_on = work['request'].get('released_on') or work['request'].get('as_of')
            if not date.fromisoformat(released_on) <= inspected <= date.fromisoformat(as_of):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        rationale = doc.get('custom_decision_rationale') or ''
        reference = re.escape(batch['completion_ref'])
        if not (doc['name'] == rows[0]['name'] and doc['modified'] == rows[0]['modified'] and doc['docstatus'] == 1
                and doc['status'] == 'Rejected' and doc['workflow_state'] == 'Lot Rejected'
                and doc['modified_by'] == cfg['qms_approver'] and doc['batch_no'] == batch['batch']
                and doc['item_code'] == batch['item'] and doc['custom_spec_revision'] == revision
                and doc['custom_lot_uom'] == batch['uom'] and doc['custom_lot_quantity'] == batch['qty']
                and doc[OP] == batch['batch'] + ':interrupted'
                and re.search(rf'(?<![\w:./-]){reference}(?![\w:/-]|\.\w)', rationale)
                and False in numeric_results(doc).values()):
            return None
        cases = qms.list(CASE, {'custom_qms_batch': batch['batch'],
                                'custom_failed_inspection': doc['name']}, limit=2)
        if len(cases) != 1:
            return None
        return dict(id=doc['name'], version=doc['modified'], approver=doc['modified_by'],
                    case=cases[0]['name'], batch=batch['batch'])

    def reject_interrupted(self, case, *, role):
        mes = self.maintenance_mes(role)
        return mes.reject_interrupted(case['interrupted']['mo'], case['fault']['native_id'],
                                      'qms:' + digest(case['qa_evidence']))


class SiteMaintenance(MaintenanceAuthority):
    """Bind existing maintenance operations to one trusted add-on MES deployment."""
    def __init__(self, authority, cfg):
        self.authority, self.run = authority, authority.run
        self.source = cfg['mes']['source_id']
        self.cfg = {**cfg, 'operator': cfg['mes']}
        scope = cfg.get('maintenance')
        if scope:
            password = os.environ.get(scope['password_env'])
            if password:
                self.cfg['maintenance_mes'] = dict(cfg['mes'], login=scope['login'], password=password)
            self.cfg['maintenance_tickets'] = {
                'url': scope['ticket_url'], 'repo': scope['ticket_repo'],
                'token': os.environ.get(scope['ticket_operator_env']),
            }
            self.cfg['maintenance_staff'] = {
                role: os.environ.get(scope[role + '_env'])
                for role in ('technician', 'tester', 'verifier', 'reader')
            }

    def settings(self):
        return self.cfg

    def native_center(self, center):
        source, _, identifier = center.rpartition(':')
        if source != self.source or not identifier.isdecimal() or int(identifier) <= 0:
            raise Pending('Equipment does not belong to this MES deployment')
        return int(identifier)

    def tickets(self, role, *, token=None):
        if 'maintenance_tickets' not in self.cfg:
            raise Pending('Site maintenance ticket scope is unavailable')
        return super().tickets(role, token=token)

    def interrupted_settings(self, item):
        cfg, product = self.authority.route(item, self.source, pinned=True)
        if cfg is not product:
            raise Pending('Interrupted item does not belong to this registered MES deployment')
        return cfg
