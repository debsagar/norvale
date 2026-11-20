"""Explicit integration hooks; importing this module never runs the simulation."""
import json
import hashlib
import subprocess
import urllib.request
from pathlib import Path
from company import Stack, project, save

RUN = Path(__file__).parent / 'recurring-run'


def stack_for(run=RUN):
    s = Stack(run)
    s.project = project(run)
    return s


def identities(engine, roster):
    """After seed: roster is topologically ordered, with role/user/name/manager."""
    result = {}
    for r in roster:
        if not engine.admin.list('User', {'name': r['user']}):
            engine.admin.create('User', {'email': r['user'], 'first_name': r['name'],
                                        'send_welcome_email': 0})
        found = engine.admin.list('Employee', {'user_id': r['user']})
        data = {'first_name': r['name'], 'company': engine.company,
                'user_id': r['user'], 'company_email':r.get('mail',r['user']), 'gender': r.get('gender', 'Other'),
                'date_of_birth': r.get('date_of_birth', '1990-01-01'),
                'date_of_joining': '2027-01-01', 'status': 'Active'}
        if r.get('manager'):
            data['reports_to'] = result[r['manager']]['employee']
        employee = (engine.admin.update('Employee', found[0]['name'], data) if found
                    else engine.admin.create('Employee', data))
        result[r['role']] = {'employee': employee['name'], 'user': r['user'],
                             'manager': r.get('manager')}
    p = engine.run / 'private/identity-crosswalk.json'
    save(p, result)
    p.chmod(0o600)
    return result


def operation_bom(engine, item, minutes_per_unit=0.1):
    """Before any Work Order: create a routed replacement BOM, retain old BOM."""
    if 'v2-bom:'+item in engine.docs:return engine.get('v2-bom:'+item)
    engine.write('v2-operation', 'Operation', {'name': 'Fill and pack', 'operation': 'Fill and pack'})
    engine.write('v2-workstation', 'Workstation', {'workstation_name': 'Filler F1'})
    old = engine.get('bom:' + item)
    return engine.write('v2-bom:' + item, 'BOM', {
        'item': item, 'quantity': 1, 'company': engine.company, 'currency': 'EUR',
        'with_operations': 1, 'items': [{k: r[k] for k in ('item_code', 'qty', 'rate')} for r in old['items']],
        'operations': [{'operation': 'Fill and pack', 'workstation': 'Filler F1',
                        'time_in_mins': minutes_per_unit}]}, submit=True)


def job_cards(engine, work_order):
    """After routed WO submit, before manufacture: return native generated cards."""
    cards = engine.admin.list('Job Card', {'work_order': work_order}, limit=1000)
    if not cards:
        raise RuntimeError('Work Order has no operation-backed Job Cards')
    return [engine.admin.get('Job Card', c['name']) for c in cards]


def complete_job(engine, name, employee, start, end, quantity):
    """Caller supplies actual modeled labor interval; no fabricated elapsed time."""
    if end <= start or quantity <= 0:
        raise ValueError('Positive labor interval and quantity required')
    api=engine.api['PRODUCTION']
    card = api.get('Job Card', name)
    if card['docstatus'] == 1:
        return card
    api.update('Job Card', name, {'time_logs': [{'employee': employee,
        'from_time': start, 'to_time': end, 'completed_qty': quantity}]})
    return api.update('Job Card', name, {'docstatus': 1})


class Tickets:
    """Gitea Issues REST API; credentials loaded privately, records in Gitea DB."""
    repo = 'controller/operations'

    def __init__(self, run=RUN, *, token=None, target=None):
        self.run = Path(run)
        if target is not None:
            self.repo = target['repo']
            self.config = {'url': target['url'].rstrip('/'), 'token': token}
            return
        self.config = json.loads((self.run / 'env/tickets.json').read_text())
        if token is not None:
            self.config['token'] = token
        from norvale.environment import docker
        self.config['url']='http://'+docker.container_ip(project(self.run),'tickets')+':3000'

    def request(self, method, path, body=None):
        req = urllib.request.Request(self.config['url'] + '/api/v1' + path,
            data=None if body is None else json.dumps(body).encode(), method=method,
            headers={'Authorization': 'token ' + self.config['token'], 'Content-Type': 'application/json'})
        from router import request_json
        # A stage retry rechecks native records before repeating a creation.
        return request_json(req, attempts=1 if method == 'POST' else 4)

    def records(self):
        result = []
        page = 1
        while True:
            rows = self.request('GET', f'/repos/{self.repo}/issues?state=all&limit=50&page={page}')
            result.extend(rows)
            if len(rows) < 50:
                return result
            page += 1

    def comments(self, number):
        result = []
        page = 1
        while True:
            rows = self.request('GET', f'/repos/{self.repo}/issues/{number}/comments?limit=50&page={page}')
            result.extend(rows)
            if len(rows) < 50:
                return result
            page += 1

    @staticmethod
    def marker(key):
        return '<!-- reference:' + hashlib.sha256(key.encode()).hexdigest() + ' -->'

    def native_id(self, operation, find_or_create):
        # A successful mutation supplies identity only. Keep it across a failed readback.
        path = self.run / 'ticket-operations.json'
        operations = json.loads(path.read_text()) if path.exists() else {}
        if operation not in operations:
            operations[operation] = find_or_create()
            save(path, operations)
        return operations[operation]

    def verify_issue(self, number, key, title, body, states):
        issue = self.request('GET', f'/repos/{self.repo}/issues/{number}')
        if (issue.get('number') != number or issue.get('state') not in states
                or issue.get('title') != title or issue.get('body') != self.marker(key) + '\n' + body):
            raise RuntimeError('Ticket issue readback differs from intended record')
        return issue

    def open(self, key, title, body, *, allow_closed=False):
        marker = self.marker(key)

        def find_or_create():
            matches = [row for row in self.records() if marker in (row.get('body') or '')]
            if len(matches) > 1:
                raise RuntimeError('Duplicate ticket operation reference')
            if matches:
                return matches[0]['number']
            issue = self.request('POST', f'/repos/{self.repo}/issues',
                                 {'title': title, 'body': marker + '\n' + body})
            return issue['number']

        number = self.native_id(f'issue:{key}', find_or_create)
        states = ('open', 'closed') if allow_closed else ('open',)
        return self.verify_issue(number, key, title, body, states)

    def close(self, number, resolution, *, key, title, body):
        path = f'/repos/{self.repo}/issues/{number}'
        issue = self.verify_issue(number, key, title, body, ('open', 'closed'))
        marker = self.marker('resolution:' + key)
        expected = marker + '\n' + resolution

        def find_or_create():
            matches = []
            page = 1
            while True:
                comments = self.request('GET', f'{path}/comments?limit=50&page={page}')
                matches.extend(row for row in comments if marker in row['body'] or row['body'] == resolution)
                if len(comments) < 50:
                    break
                page += 1
            if len(matches) > 1:
                raise RuntimeError('Duplicate ticket resolution reference')
            if matches:
                return {'id': matches[0]['id'], 'legacy': matches[0]['body'] == resolution}
            return {'id': self.request('POST', f'{path}/comments', {'body': expected})['id'],
                    'legacy': False}

        identity = self.native_id(f'resolution:{key}:{number}', find_or_create)
        comment_id = identity['id']
        comment = self.request('GET', f'/repos/{self.repo}/issues/comments/{comment_id}')
        if (comment.get('id') != comment_id
                or comment.get('body') != (resolution if identity['legacy'] else expected)
                or not (comment.get('issue_url') or '').endswith(f'/{self.repo}/issues/{number}')):
            raise RuntimeError('Ticket comment readback differs from intended resolution')
        if issue['state'] != 'closed':
            self.request('PATCH', path, {'state': 'closed'})
        return self.verify_issue(number, key, title, body, ('closed',))


def export_native(run=RUN):
    """Privileged DB snapshot, separate mail archive and API ticket export."""
    s = stack_for(run)
    out = Path(run) / 'export'
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    from norvale.environment import docker
    with (out / 'erp.sql').open('w') as f:
        subprocess.run(['docker','compose','-p',s.project,'-f',str(s.file),'exec','-T',
                        'db','mariadb-dump','-uroot','-padmin','--single-transaction',s.db_name()],
                       stdout=f, stderr=subprocess.PIPE, check=True)
    with (out / 'mail.tar').open('wb') as f:
        subprocess.run(['docker','compose','-p',s.project,'-f',str(s.file),'exec','-T',
                        'mail','tar','cf','-','/var/mail'], stdout=f, stderr=subprocess.PIPE, check=True)
    t = Tickets(run)
    issues = t.records()
    for issue in issues:
        issue['exported_comments'] = t.request('GET',f'/repos/{t.repo}/issues/{issue["number"]}/comments')
    save(out / 'tickets.json', issues)
    private = Path(run) / 'private'
    private.mkdir(exist_ok=True)
    private.chmod(0o700)
    save(private / 'system-crosswalk.json', {'tickets': {str(i['number']): i['html_url'] for i in issues}, 'identity_file': 'identity-crosswalk.json', 'simulation_started': bool(json.loads((Path(run)/'state.json').read_text()).get('completed')) if (Path(run)/'state.json').exists() else False})
    (private / 'system-crosswalk.json').chmod(0o600)
    for p in out.iterdir():p.chmod(0o700 if p.is_dir() else 0o600)
