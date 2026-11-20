"""Offline regressions for native Norvale recovery and evidence boundaries.

Run from the release root: python3 -m unittest discover -s tests -p test_norvale_recovery.py
"""
import copy
import io
import smtplib
from email import message_from_bytes
from email.message import EmailMessage
from types import SimpleNamespace
from email.policy import SMTP as SMTP_POLICY, default as DEFAULT_POLICY
import hashlib
from urllib.parse import urlsplit, parse_qs, unquote
import json
import operator
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native'))
import company
import router
import systems_v2


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RecoveryTests(unittest.TestCase):
    def test_opening_company_uses_only_employee_clients_until_admin_is_requested(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from unittest.mock import patch
        from native import company
        with TemporaryDirectory() as directory:
            run = Path(directory)
            company.save(run/'env/role-tokens.json', {'PLANNER': 'planner-token'})
            endpoints = {'erp': 'http://erp.test', 'site': 'test'}
            with patch.object(company.Stack, 'endpoints', return_value=endpoints), patch.object(company, 'ErpClient') as erp:
                engine = company.Engine(run)
                erp.assert_called_once_with(endpoints['erp'], endpoints['site'], 'planner-token')
                erp.return_value.login.assert_not_called()
                self.assertFalse((run/'env/controller-token.json').exists())
                company.save(run/'env/controller-token.json', {'token': 'saved-controller'})
                self.assertIs(engine.admin, engine.admin)
                erp.assert_called_with(endpoints['erp'], endpoints['site'], 'saved-controller')
                erp.return_value.login.assert_not_called()

    def test_through_checks_reloaded_stage_before_crossing_day_boundary(self):
        engine = company.Engine.__new__(company.Engine)
        engine.config = {'days': 2}
        engine.state = {'completed': [], 'docs': {}, 'day': 0}
        engine.events = lambda: iter(((0, 'today', None), (1, 'tomorrow', None)))
        with tempfile.TemporaryDirectory() as directory:
            engine.run = Path(directory)
            path = engine.run/'state.json'
            path.write_text(json.dumps(dict(engine.state, completed=['today'])))
            saved = path.read_bytes()
            with patch.object(engine, 'run_phase') as dispatch:
                engine.through(0)
            dispatch.assert_not_called()
            self.assertEqual(path.read_bytes(), saved)


    def ticket_server(self, kind, timing=None, altered=None):
        with tempfile.TemporaryDirectory() as tmp:
            return self.exercise_ticket_server(Path(tmp), kind, timing, altered)

    def exercise_ticket_server(self, run, kind, timing, altered):
        key, title, body = 'service:1', 'Service', 'Fix it'
        marker = systems_v2.Tickets.marker(key)
        issue_body = marker + '\n' + body
        issues = {41: {'number': 41, 'title': title, 'body': issue_body, 'state': 'open'}} if kind != 'open' else {}
        comments = {}
        calls = []
        failed = False
        base = '/api/v1/repos/controller/operations/issues'

        def urlopen(req, timeout):
            nonlocal failed
            url = urlsplit(req.full_url)
            path, method = url.path, req.get_method()
            calls.append((method, path))
            if method == 'GET':
                if path == base:
                    result = list(issues.values())
                elif path == base + '/41/comments':
                    result = list(comments.values())
                elif path.startswith(base + '/comments/'):
                    result = comments[int(path.rsplit('/', 1)[1])]
                else:
                    result = issues[int(path.rsplit('/', 1)[1])]
                return Response(json.dumps(result).encode())
            if method == 'POST':
                if not failed and timing == 'before':
                    failed = True
                    raise TimeoutError('before commit')
                payload = json.loads(req.data)
                if path == base:
                    record = dict(payload, number=42, state='open')
                    issues[42] = {**record, **(altered or {})}
                else:
                    self.assertEqual(path, base + '/41/comments')
                    record = dict(payload, id=100, issue_url='http://offline.invalid/controller/operations/issues/41')
                    comments[100] = {**record, **(altered or {})}
                if not failed and timing == 'after':
                    failed = True
                    raise TimeoutError('after commit')
                return Response(json.dumps(record).encode())
            self.assertEqual((method, path), ('PATCH', base + '/41'))
            record = {**issues[41], **json.loads(req.data)}
            if kind != 'patch':
                issues[41] = record
            return Response(json.dumps(record).encode())

        def operation():
            tickets = systems_v2.Tickets.__new__(systems_v2.Tickets)
            tickets.run = run
            tickets.config = {'url': 'http://offline.invalid', 'token': 'fake'}
            if kind == 'open':
                return tickets.open(key, title, body)
            return tickets.close(41, 'restarted', key=key, title=title, body=body)

        with patch.object(router.urllib.request, 'urlopen', side_effect=urlopen), patch.object(router.time, 'sleep'):
            if altered or kind == 'patch':
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'Ticket .* readback differs'):
                        operation()
                self.assertEqual(sum(method == 'POST' for method, _ in calls), 1)
                if kind == 'close':
                    self.assertNotIn(('PATCH', base + '/41'), calls)
                return
            with self.assertRaises(TimeoutError):
                operation()
            result = operation()
        self.assertTrue(failed)
        self.assertEqual(sum(method == 'POST' for method, _ in calls), 2 if timing == 'before' else 1)
        if kind == 'open':
            self.assertEqual(result, {'number': 42, 'body': issue_body, 'title': title, 'state': 'open'})
            self.assertEqual(calls[-1], ('GET', base + '/42'))
        else:
            self.assertEqual(comments[100]['body'], systems_v2.Tickets.marker('resolution:' + key) + '\nrestarted')
            self.assertEqual(result['state'], 'closed')
            self.assertIn(('GET', base + '/comments/100'), calls)
            self.assertEqual(calls[-2:], [('PATCH', base + '/41'), ('GET', base + '/41')])

    def test_gitea_timeouts(self):
        for kind in ('open', 'close'):
            for timing in ('before', 'after'):
                with self.subTest(kind=kind, timing=timing):
                    self.ticket_server(kind, timing)

    def test_altered_native_ticket(self):
        for kind, altered in (
                ('open', {'body': 'missing reference'}),
                ('open', {'title': 'wrong'}),
                ('open', {'state': 'closed'}),
                ('close', {'body': 'changed resolution'}),
                ('close', {'issue_url': 'http://offline.invalid/controller/operations/issues/42'}),
                ('patch', None)):
            with self.subTest(kind=kind, altered=altered):
                self.ticket_server(kind, altered=altered)

    def exercise_mail(self, mode, *, altered=None):
        addresses = {r['role_id']: r['email'].replace('.example', '.test') for r in company.ROLES}
        origin, recipient = addresses['CFO'], addresses['CEO']
        boxes, submissions, reads, pending, logs = {}, [], [], [], []
        failed = False
        test = self

        class SMTP:
            def __init__(self, host, port, timeout):
                test.assertEqual((host, port), ('offline.invalid', 25))

            def __enter__(self): return self

            def __exit__(self, *args):
                if mode == 'quit' and args[0] is None:
                    raise smtplib.SMTPServerDisconnected('QUIT disconnected')

            def login(self, address, password):
                nonlocal failed
                test.assertEqual(address, origin)
                if mode == 'before' and not failed:
                    failed = True
                    raise TimeoutError('before transaction')

            def send_message(self, message):
                nonlocal failed
                submissions.append(message.as_bytes(policy=SMTP_POLICY))
                if mode == 'reject' and not failed:
                    failed = True
                    raise smtplib.SMTPDataError(451, b'explicit rejection')
                if altered:
                    message = message_from_bytes(submissions[-1], policy=DEFAULT_POLICY)
                    field, value = altered
                    if field == 'body':
                        message.set_content(value)
                    elif value is None:
                        del message[field]
                    else:
                        message.replace_header(field, value)
                wire = message.as_bytes(policy=SMTP_POLICY)
                if mode in ('delayed', 'uncertain'):
                    pending.append(wire)
                else:
                    boxes.setdefault(recipient, []).append(wire)
                if mode == 'after':
                    for i in range(205):
                        unrelated = EmailMessage()
                        unrelated['Subject'] = f'Unrelated {i}'
                        unrelated.set_content('other mail')
                        boxes[recipient].append(unrelated.as_bytes(policy=SMTP_POLICY))
                if mode in ('after', 'uncertain'):
                    raise smtplib.SMTPServerDisconnected('DATA response lost')
                return {}

        class IMAP:
            def __init__(self, host, port):
                test.assertEqual((host, port), ('offline.invalid', 143))

            def __enter__(self): return self

            def __exit__(self, *args): pass

            def login(self, address, password):
                test.assertEqual(address, recipient)
                self.mailbox = boxes.setdefault(address, [])

            def select(self, folder, readonly=False):
                test.assertEqual(folder, 'INBOX')
                return 'OK', [str(len(self.mailbox)).encode()]

            def uid(self, command, *args):
                if command == 'search':
                    return 'OK', [b' '.join(str(i+1).encode() for i in range(len(self.mailbox)))]
                uid, query = args
                message = self.mailbox[int(uid)-1]
                if query == '(RFC822)':
                    reads.append((recipient, str(uid)))
                else:
                    message = message.split(b'\r\n\r\n', 1)[0] + b'\r\n\r\n'
                return 'OK', [(b'1 (FLAGS ()', message), b')']

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'env').mkdir()
            (run / 'env/secrets.json').write_text(json.dumps({r: {'password': 'fake'} for r in addresses}))
            (run / 'state.json').write_text(json.dumps({'day': 0}))
            body = 'Review text\n' + 'long body line\n'*400

            def operation(day=0):
                e = company.Engine.__new__(company.Engine)
                e.run, e.day = run, day
                e.eps = {'mail': {'host': 'offline.invalid', 'smtp': 25, 'imap': 143}}
                e.state = json.loads((run / 'state.json').read_text())
                def settle():
                    if mode == 'delayed':
                        raise TimeoutError('queue still busy')
                e.stack = SimpleNamespace(settle=settle)
                e.log = lambda kind, **kw: logs.append((kind, kw))
                e.mail('key', 'CFO', 'CEO', 'Review', body)

            def status():
                return json.loads((run / 'state.json').read_text()).get('mail_operations', {}).get('key', {}).get('status')

            with patch('norvale.agents.comms.smtplib.SMTP', SMTP), patch('norvale.agents.comms.imaplib.IMAP4', IMAP):
                if altered:
                    for _ in range(2):
                        with self.assertRaisesRegex(RuntimeError, 'Mail delivery readback differs'):
                            operation()
                    self.assertEqual(len(submissions), 1)
                    self.assertEqual(status(), 'accepted')
                    self.assertEqual(logs, [])
                    return
                if mode in ('delayed', 'uncertain'):
                    for _ in range(2):
                        with self.assertRaisesRegex(TimeoutError, 'Mail delivery pending'):
                            operation()
                        self.assertEqual(status(), 'accepted' if mode == 'delayed' else 'uncertain')
                        self.assertEqual(len(submissions), 1)
                    boxes.setdefault(recipient, []).extend(pending)
                elif mode in ('before', 'reject'):
                    with self.assertRaises(TimeoutError if mode == 'before' else smtplib.SMTPDataError):
                        operation()
                    self.assertIsNone(status())
                operation(1 if mode in ('delayed','uncertain') else 0)
                self.assertEqual(status(), 'confirmed')
                operation(2)

            self.assertEqual(len(submissions), 2 if mode == 'reject' else 1)
            self.assertEqual(reads, [(recipient, '1'), (recipient, '1')])
            self.assertEqual(logs[-1][1]['message']['body'].replace('\r\n', '\n'), body)
            sent = message_from_bytes(submissions[-1], policy=DEFAULT_POLICY)
            self.assertEqual((str(sent['From']), str(sent['To'])), (origin, recipient))
            self.assertEqual(str(sent['Subject']), '[Ref ' + hashlib.sha256(b'key').hexdigest()[:16] + '] Review')

    def test_mail_recovery(self):
        for mode in ('before', 'reject', 'after', 'quit', 'delayed', 'uncertain'):
            with self.subTest(mode=mode):
                self.exercise_mail(mode)

    def test_stage_retries_only_temporary_smtp_rejections(self):
        for code in (451, 550):
            with self.subTest(code=code):
                e = company.Engine.__new__(company.Engine)
                rejection = smtplib.SMTPDataError(code, b'rejected')
                e.log = lambda *a, **kw: None
                with patch.object(e, 'step', side_effect=[rejection, None]) as step, patch.object(company.time, 'sleep'):
                    if code == 451:
                        e.run_all()
                        self.assertEqual(step.call_count, 2)
                    else:
                        with self.assertRaises(smtplib.SMTPDataError):
                            e.run_all()
                        step.assert_called_once()

    def test_altered_message(self):
        for altered in (('From', 'other@norvale.test'), ('To', 'other@norvale.test'),
                        ('Date', None), ('Date', 'Mon, 01 Jan 2029 00:00:00 +0000'),
                        ('Subject', 'missing marker'), ('Message-ID', '<wrong@norvale.test>'),
                        ('body', 'Review text\n' + 'long body line\n'*400 + 'altered tail')):
            with self.subTest(field=altered[0]):
                self.exercise_mail('altered', altered=altered)

    def test_management_step_recovers_crashes(self):
        for crash in ('before_task', 'after_task'):
            with self.subTest(crash=crash), tempfile.TemporaryDirectory() as tmp:
                run = Path(tmp)
                config = dict(company.DEFAULT_CONFIG, days=91)
                (run / 'config.json').write_text(json.dumps(config))
                # All earlier phases are complete; the normal step selects weekly management.
                completed = ['seed', 'recurring-setup'] + [f'd{d:04}:{phase}' for d in range(91) for phase in ['service','arrivals','inspections','demand','fulfillment','production','replenishment','approvals','settlements','management'] if (d, phase) != (90, 'management')]
                state = {'docs': {}, 'completed': completed, 'physical': {}, 'decisions': {}, 'day': 90, 'management_quarter': 0}
                (run / 'state.json').write_text(json.dumps(state))
                records, mails, failed = {}, [], [False]
                class Clock:
                    def now(self):
                        from datetime import datetime, timezone
                        return datetime(2028, 1, 1, tzinfo=timezone.utc)
                class Stack:
                    def __init__(self, run): self.clock = Clock()
                    def endpoints(self): return {'erp': 'offline', 'site': 'offline'}
                    def settle(self): pass
                class Admin:
                    def __init__(self, *a): pass
                    def login(self, *a): pass
                    def call(self, *a): return {'api_secret': 'fake'}
                    def get(self, *a): return {'api_key': 'fake'}
                def install(e):
                    def write(key, dt, data, *args):
                        if key.startswith('followup:') and crash == 'before_task' and not failed[0]:
                            failed[0] = True
                            raise TimeoutError('before task')
                        records.setdefault(key, {'name': key, 'doctype': dt, **data})
                        e.docs[key] = {'name': key, 'doctype': dt}
                        e.persist()
                        return records[key]
                    e.write = write
                    e.get = lambda key, *args: records[key]
                    e.observe = lambda *args: []
                    e.choose = lambda *args, **kw: {'action': 'investigate', 'reason': 'Check costs', 'message': 'Please check'}
                    def mail(*args):
                        if crash == 'after_task' and not failed[0]:
                            failed[0] = True
                            raise TimeoutError('after task before email')
                        mails.append(args)
                    e.mail = mail
                with patch.object(company, 'Stack', Stack), patch.object(company, 'ErpClient', Admin):
                    first = company.Engine(run); install(first)
                    with self.assertRaises(TimeoutError): first.step('d0090:management')
                    second = company.Engine(run); install(second)
                    second.step('d0090:management')
                self.assertTrue(failed[0])
                tasks = [r for r in records.values() if r['doctype'] == 'ToDo']
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]['description'], 'Check costs')
                self.assertEqual(mails, [('report:90', 'CFO', 'CEO', 'Periodic operating review', 'Please check')])
                self.assertIn('d0090:management', json.loads((run / 'state.json').read_text())['completed'])

    def test_observe_excludes_future_posted_records(self):
        rows = [{'name': 'today', 'posting_date': '2027-01-01'},
                {'name': 'future', 'posting_date': '2027-01-02'}]
        class API:
            def list(self, dt, filters, fields, limit):
                cutoff = filters.get('posting_date')
                return [r for r in rows if not cutoff or r['posting_date'] <= cutoff[1]]
        e = company.Engine.__new__(company.Engine)
        e.day, e.api = 0, {'CFO': API()}
        e.log = lambda *a, **kw: None
        observed = e.observe('CFO', 'GL Entry', {}, ['name', 'posting_date'])
        self.assertEqual([r['name'] for r in observed], ['today'])

    def test_as_of_receipts_and_bin(self):
        rows = {'Purchase Receipt': [
            {'name':'old','posting_date':'2026-12-30','supplier':'A','docstatus':1},
            {'name':'today','posting_date':'2027-01-01','supplier':'A','docstatus':1},
            {'name':'future','posting_date':'2027-01-02','supplier':'A','docstatus':1},
            {'name':'draft','posting_date':'2026-12-30','supplier':'A','docstatus':0},
            {'name':'other','posting_date':'2026-12-30','supplier':'B','docstatus':1}],
            'Bin': [{'item_code':'A','actual_qty':3}]}
        requests = []
        api = company.ErpClient('http://offline.invalid', 'offline', 'fake')
        def open_request(req, timeout):
            url = urlsplit(req.full_url)
            dt = unquote(url.path.rsplit('/', 1)[1])
            args = parse_qs(url.query)
            filters = json.loads(args.get('filters', ['{}'])[0])
            requests.append((dt, filters))
            self.assertEqual(args['limit_page_length'], ['10000'])
            predicates = ([[k, *v] if isinstance(v, list) else [k, '=', v]
                           for k, v in filters.items()] if isinstance(filters, dict) else filters)
            compare = {'<=': operator.le, '>=': operator.ge, '=': operator.eq}
            data = [row for row in rows[dt]
                    if all(compare[op](row[field], value) for field, op, value in predicates)]
            return Response(json.dumps({'data': data}).encode())

        e = company.Engine.__new__(company.Engine)
        e.day, e.api, e.log = 0, {'CFO': api}, lambda *a, **kw: None
        base = {'supplier': 'A', 'docstatus': 1}
        and_filters = [['supplier', '=', 'A'], ['docstatus', '=', 1],
                       ['posting_date', '>=', '2026-12-30']]
        cutoff = ['posting_date', '<=', '2027-01-01']
        cases = [
            (base, ['old', 'today'], {**base, 'posting_date': cutoff[1:]}),
            ({**base, 'posting_date': ['<=', '2026-12-30']}, ['old'],
             {**base, 'posting_date': ['<=', '2026-12-30']}),
            ({**base, 'posting_date': '2027-01-01'}, ['today'],
             [*and_filters[:2], ['posting_date', '=', '2027-01-01'], cutoff]),
            ({**base, 'posting_date': '2027-01-02'}, [],
             [*and_filters[:2], ['posting_date', '=', '2027-01-02'], cutoff]),
            (and_filters, ['old', 'today'], [*and_filters, cutoff]),
        ]
        with patch.object(api.opener, 'open', side_effect=open_request):
            for filters, names, serialized in cases:
                with self.subTest(filters=filters):
                    self.assertEqual([r['name'] for r in e.observe(
                        'CFO', 'Purchase Receipt', filters, ['name', 'posting_date'])], names)
                    self.assertEqual(requests[-1], ('Purchase Receipt', serialized))
            self.assertEqual(e.observe('CFO', 'Bin', {}, ['item_code', 'actual_qty']), rows['Bin'])
            self.assertEqual(requests[-1], ('Bin', {}))

    def test_cache_evidence(self):
        for change in ('unchanged', 'reordered', 'quote', 'orders', 'receipts', 'usage', 'coverage',
                       'total', 'schedule', 'receiving_policy'):
            with self.subTest(change=change):
                e = company.Engine.__new__(company.Engine); e.day = 10
                policy = {'role':'SCM_MGR','effective_date':'2027-01-01','review_date':'2027-02-01','approval_limit':1000,'reorder_target':100,'suppliers':['Supplier'],'items':['A']}
                evidence = {'supplier':'Supplier','grand_total':100,'items':[{'item_code':'A','qty':10,'rate':10}], 'coverage':[{'item_code':'A','released_raw_materials_qty':0,'open_po_qty_excluding_this':1,'quarantine_qty':0}], 'supplier_quote':{'name':'Q-1','items':[{'item_code':'A','qty':10,'rate':10}]}, 'other_open_orders':[{'name':'PO-1','supplier':'Supplier','grand_total':10}]}
                evidence.update(
                    supplier_receipts=[{'name': 'REC-1', 'posting_date': '2027-01-01'},
                                       {'name': 'REC-2', 'posting_date': '2027-01-02'}],
                    observed_daily_usage={'A': 10}, receiving_policy='Inspect before release')
                previous = {'mode':'standing-policy','input':{'role':'SCM_MGR','as_of':'2027-01-05','evidence':copy.deepcopy(evidence)},'policy':policy,'classification':False,'output':{'action':'approve'}}
                e.state = {'decisions':{'old':previous},'standing_policies':[policy]}; e.persist = lambda: None; e.log = lambda *a, **kw: None
                e.decision = lambda *a, **kw: {'output':{'action':'investigate'}}
                if change == 'quote': evidence['supplier_quote']['items'][0]['rate'] = 20
                if change == 'orders': evidence['other_open_orders'][0]['grand_total'] = 90
                if change == 'reordered': evidence['supplier_receipts'].reverse()
                if change == 'receipts': evidence['supplier_receipts'][0]['posting_date'] = '2027-01-03'
                if change == 'usage': evidence['observed_daily_usage']['A'] = 11
                if change == 'coverage': evidence['coverage'][0]['open_po_qty_excluding_this'] = 2
                if change == 'total': evidence['grand_total'] = 101
                if change == 'schedule': evidence['items'][0]['schedule_date'] = '2027-01-15'
                if change == 'receiving_policy': evidence['receiving_policy'] = 'Hold for sampling'
                with patch.object(router, 'route', return_value={'judgment':True}) as route:
                    result = e.choose('new','SCM_MGR',evidence,'Review purchase')
                if change in ('unchanged', 'reordered'):
                    route.assert_not_called(); self.assertEqual(result['action'], 'approve')
                else:
                    route.assert_called_once()
                    self.assertEqual({k: route.call_args.args[0][k] for k in evidence}, evidence)
                    self.assertEqual(result['action'], 'investigate')


if __name__ == '__main__':
    unittest.main()
