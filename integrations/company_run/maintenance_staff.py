"""Ordinary staffed maintenance for recognized simulated usage faults."""
from datetime import datetime, timedelta

from integrations.mes.client import CENTER, EVENT
from native.company_erp import Pending, digest

ACTORS = {role: 'norvale-maintenance-' + role for role in ('technician', 'tester', 'verifier', 'reader')}
MINUTES = {'diagnosis': 25, 'service': 60, 'test': 20, 'verification': 15}
LIMIT = 250  # Hundredths of a mm/s RMS at the no-load check.


def usage_fault(namespace, key, case):
    meter = case['meter']
    expected = 'eq-' + digest([namespace, case['center'], f'usage:{meter}'])[:32]
    return key == expected and case['reason'] == f'Usage limit reached at {meter} units'


def vibration(seed, center, meter):
    # Physical model: accumulated wear elevates vibration; cleaning and
    # reseating the guide reduces it. A second alignment has a wider correction.
    jitter = int(digest([seed, center, meter, 'guide'])[:8], 16) % 81
    return 340 + jitter


def staffing_scope(case):
    center = case['center']
    return center.rpartition(':')[0] if isinstance(center, str) else None


def staffing(case, cases, day, stage):
    """One employee per role, one job per shift, with immutable retry slots."""
    prior = case.get('staff_turn')
    if prior:
        if prior['stage'] != stage:
            raise Pending('Maintenance stage changed before its staff turn completed')
        return prior
    role = {'diagnosis': 'technician', 'service': 'technician',
            'test': 'tester', 'verification': 'verifier'}[stage]
    if any(turn['day'] == day and turn['role'] == role
           for other in cases.values()
           for turn in other.get('staff_history', []) + ([other['staff_turn']] if other.get('staff_turn') else [])):
        raise Pending('Maintenance employee already assigned for this shift')
    return dict(stage=stage, role=role, day=day, minute=480 - MINUTES[stage], duration=MINUTES[stage])


class Staff:
    def __init__(self, equipment, key, case, ticket, *, authority=None):
        self.equipment, self.key, self.case, self.ticket = equipment, key, case, ticket
        self.e, self.a = equipment.e, authority or equipment.authority
        self.center = self.a.native_center(case['center']) if isinstance(case['center'], str) else case['center']
        settings = self.a.settings()
        self.cfg = settings['maintenance_staff']
        repo = settings.get('maintenance_tickets', {}).get('repo', 'controller/operations')
        self.path = f'/repos/{repo}/issues/{ticket["number"]}'

    def tickets(self, role):
        return self.a.tickets('MAINTENANCE', token=self.cfg[role])

    def comment(self, role, body, *, create=False):
        rows = self.tickets('reader').comments(self.ticket['number'])
        matches = [row for row in rows if row.get('user', {}).get('login') == ACTORS[role]
                   and row['body'].splitlines()[0:1] == body.splitlines()[0:1]]
        if not matches and create:
            self.tickets(role).request('POST', self.path + '/comments', {'body': body})
            return self.comment(role, body)
        if len(matches) != 1 or matches[0]['body'] != body:
            raise Pending(f'Missing or changed native {role} evidence')
        row = matches[0]
        return dict(id=row['id'], version=row['updated_at'],
                    ref=f'{self.ticket["html_url"]}#issuecomment-{row["id"]}')

    def blocked(self, reader, *, replay=False):
        case = self.case
        row = reader.read(EVENT, case['fault']['native_id'])
        if (row['workcenter_id'][0] != self.center or row['loss_type'] != 'availability'
                or row['description'].splitlines()[0] != 'Fault ' + self.key
                or row['norvale_interrupted_id'] != case['fault']['norvale_interrupted_id']):
            raise Pending('MES fault differs from the simulated maintenance scope')
        open_losses = reader.call(EVENT, 'search', [['workcenter_id', '=', self.center],
            ['loss_type', '=', 'availability'], ['date_end', '=', False]])
        state = reader.read(CENTER, self.center)['working_state']
        if row['date_end'] and replay and not open_losses:
            return
        if row['date_end'] or open_losses != [row['id']] or state != 'blocked':
            raise Pending('Maintenance requires one open, isolated equipment fault')

    def record_service(self, reader, description, turn, *, create=False):
        center = self.center
        domain = [['workcenter_id', '=', center], ['description', '=like', description.splitlines()[0] + '%']]
        ids = reader.call(EVENT, 'search', domain)
        start = datetime.fromisoformat(self.e.dated(turn['day'])) + timedelta(minutes=turn['minute'])
        end = start + timedelta(minutes=turn['duration'])
        expected = dict(description=description, date_start=str(start), date_end=str(end))
        if not ids and create:
            tech = self.a.maintenance_mes('MAINTENANCE')
            loss = tech.call('mrp.workcenter.productivity.loss', 'search',
                             [['loss_type', '=', 'performance']], limit=1)
            if not loss:
                raise Pending('No Odoo performance loss type for the service log')
            tech.call(EVENT, 'create', dict(expected, workcenter_id=center, loss_id=loss[0], user_id=tech.uid))
            return self.record_service(reader, description, turn)
        if len(ids) != 1:
            raise Pending('Missing or duplicate Odoo equipment service')
        row, = reader.call(EVENT, 'read', ids, fields=[
            'user_id', 'create_uid', 'loss_type', 'workorder_id', 'write_date', *expected])
        login = self.a.settings()['maintenance_mes']['login']
        uid = reader.call('res.users', 'search', [['login', '=', login]])
        if (len(uid) != 1 or row['user_id'][0:1] != uid or row['create_uid'][0:1] != uid
                or row['loss_type'] != 'performance' or row['workorder_id']
                or any(row[field] != value for field, value in expected.items())):
            raise Pending('Odoo equipment service differs from the performed intervention')
        adjustment = int(row['description'].splitlines()[-1].removeprefix('Applied adjustment: '))
        return dict(id=row['id'], version=row['write_date'], ref=reader._ref(EVENT, row['id'])['source_url'],
                    adjustment=adjustment)

    def advance(self):
        case, e = self.case, self.e
        history = case.setdefault('staff_history', [])
        stage = 'diagnosis'
        if history:
            stage = {'diagnosis': 'service', 'service': 'test',
                     'test': 'verification' if history[-1].get('passed') else 'service',
                     'verification': 'verification'}[history[-1]['stage']]
        reader = self.a.operator('PRODUCTION')
        complete = bool(history and history[-1]['stage'] == 'verification')
        self.blocked(reader, replay=complete)
        if not complete:
            earliest = case['day'] + e.config['maintenance_days']
            if e.day < earliest or (history and e.day <= history[-1]['day']):
                raise Pending('Awaiting the next staffed maintenance shift')
            scope = staffing_scope(case)
            peers = {key: other for key, other in self.equipment.cases.items()
                     if staffing_scope(other) == scope}
            turn = staffing(case, peers, e.day, stage)
            case['staff_turn'] = turn
            e.persist()
        else:
            turn = history[-1]

        fault = case['fault']['source_url']
        before = vibration(e.config['seed'], case['center'], case['meter'])
        diagnosis = f'Diagnosis for {fault}\nGuide vibration {before / 100:.2f} mm/s RMS under modeled coast-down.'
        if stage == 'diagnosis':
            evidence = self.comment('technician', diagnosis, create=True)
        else:
            observed = self.comment('technician', diagnosis)
            if observed != history[0]['evidence']:
                raise Pending('Pinned technician diagnosis changed')
            services = [row for row in history if row['stage'] == 'service']
            attempt = len(services) + (stage == 'service')
            service_turn = turn if stage == 'service' else services[-1]
            action = 'Cleaned and reseated guide' if attempt == 1 else 'Realigned guide and torqued mount'
            adjustment = 145 if attempt == 1 else 205
            description = (f'Service {self.key}/{attempt}\n{action}; center {case["center"]}; '
                           f'fault {fault}; Gitea diagnosis {observed["ref"]}\n'
                           f'Applied adjustment: {adjustment}')
            service = self.record_service(reader, description, service_turn, create=stage == 'service')
            if stage == 'service':
                evidence = service
            else:
                if service != service_turn['evidence']:
                    raise Pending('Pinned equipment service changed')
                measured = before - service['adjustment']
                passed = measured <= LIMIT
                test = (f'Functional test {fault} / {attempt}\nService: {service["ref"]}\n'
                        f'No-load guide vibration: {measured / 100:.2f} mm/s RMS; '
                        f'limit: {LIMIT / 100:.2f} mm/s RMS.\nResult: {"PASS" if passed else "FAIL"}')
                checked = self.comment('tester', test, create=stage == 'test')
                if stage == 'test':
                    evidence = checked
                    turn['passed'] = passed
                else:
                    test_turn = history[-2] if complete else history[-1]
                    if not passed or checked != test_turn['evidence']:
                        raise Pending('Independent functional test is failing or changed')
                    body = (f'Verified restart: {fault}\nDiagnosis: {observed["ref"]}\n'
                            f'Repair: {service["ref"]}\nTest: {checked["ref"]}\nResult: PASS')
                    evidence = self.comment('verifier', body, create=not complete)
        if complete:
            if evidence != turn['evidence']:
                raise Pending('Pinned independent verification changed')
            return evidence
        turn['evidence'] = evidence
        history.append(case.pop('staff_turn'))
        e.persist()
        e.log('maintenance_staff_work', center=case['center'], fault=fault, **turn)
        if stage != 'verification':
            raise Pending('Functional test failed; corrective service required'
                          if turn.get('passed') is False else 'Awaiting next staffed maintenance step')
        return evidence
