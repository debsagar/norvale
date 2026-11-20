"""Ordinary purchasing survives exhausted provider transport retries."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from native.company import Engine


class PurchaseOutage(TestCase):
    def test_router_and_judgment_outages_leave_draft_pending_across_restart(self):
        for failed in ('JEV', 'Model'):
            with self.subTest(provider=failed), TemporaryDirectory() as directory:
                self.exercise(Path(directory), failed)

    def exercise(self, run, failed):
        line = dict(item_code='RAW', qty=4, rate=2, amount=8, schedule_date='2027-01-10')
        po = dict(name='PO', docstatus=0, supplier='Supplier', items=[line], grand_total=8)
        submitted = dict(po, name='PO-OTHER', docstatus=1)
        quote = dict(po, name='QUOTE', valid_till='2027-01-12')
        native_docs = {('Purchase Order', po['name']): po, ('Purchase Order', submitted['name']): submitted,
                       ('Supplier Quotation', quote['name']): quote}
        keys = {'draft': ('Purchase Order', po['name']), 'submitted': ('Purchase Order', submitted['name']),
                'quote:draft': ('Supplier Quotation', quote['name'])}
        engine = Engine.__new__(Engine)
        engine.run, engine.day = run, 2
        engine.state = dict(decisions={}, ops=dict(purchases={'draft': {'day': 1}, 'submitted': {'day': 1}},
                                                 changes={}))
        engine.docs = engine.state['docs'] = {key: dict(doctype=kind, name=name)
                                              for key, (kind, name) in keys.items()}
        engine.config = dict(company_operating=True, seed=4)
        engine.wh = dict(rm='Raw', hold='Hold')
        engine.regime = lambda: {'supply': 1}
        engine.observe = Mock(return_value=[])
        engine.observed_usage = Mock(return_value={})
        engine.effective_policies = Mock(return_value=[])
        engine.log = Mock()
        engine.admin = Mock()
        engine.admin.list.return_value = [{'name': 'ToDo-custom_norvale_operation'}]

        def find_task(kind, filters):
            return [{'name': name} for (doctype, name), doc in native_docs.items() if doctype == kind
                    and all(doc.get(field) == expected for field, expected in filters.items())]

        def create_task(kind, fields):
            self.assertEqual(kind, 'ToDo')
            self.assertFalse(find_task(kind, {'custom_norvale_operation': fields['custom_norvale_operation']}))
            native_docs[kind, 'TASK-1'] = dict(fields, name='TASK-1', owner='scm_mgr@norvale.test')
            raise TimeoutError('task committed, acknowledgement lost')

        def approve(method, args):
            self.assertEqual((args['action'], json.loads(args['doc'])['name']), ('Approve', 'PO'))
            po['docstatus'] = 1
        engine.api = {'SCM_MGR': Mock()}
        client = engine.api['SCM_MGR']
        client.call.side_effect = approve
        client.get.side_effect = lambda kind, name: dict(native_docs[kind, name])
        client.list.side_effect = find_task
        client.create.side_effect = create_task
        buyer = engine.api['BUYER'] = Mock()
        buyer.get.side_effect = client.get.side_effect

        def response(req, on_attempt):
            provider = 'JEV' if 'typesafe' in req.full_url else 'Model'
            if provider == failed and engine.day == 2:
                raise TimeoutError('offline fixture')
            if provider == 'JEV':
                self.assertEqual(json.loads(req.data)['state']['cases'], [])
                return {'answers': {'judgment': {'noul': 1}}}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(
                dict(action='approve', reason='Reviewed revised evidence', message='Approved'))}}]}

        with patch.dict(os.environ, TYPESAFE_API_KEY='fixture', OPENAI_API_KEY='fixture', NORVALE_OFFLINE='0'), \
                patch('router.request_json', side_effect=response) as request:
            with self.assertRaisesRegex(TimeoutError, 'task committed'):
                engine.approvals()
            engine.state = json.loads((run/'state.json').read_text())
            engine.docs = engine.state['docs']
            self.assertNotIn('pending:draft:2', engine.docs)
            self.assertNotIn('review_after', engine.state['ops']['purchases']['draft'])
            before_retry = request.call_count
            task = native_docs['ToDo', 'TASK-1']
            self.assertEqual(task['date'], engine.dated(4))
            with patch.dict(task, reference_name='another order'):
                with self.assertRaisesRegex(RuntimeError, 'buyer follow-up'):
                    engine.approvals()
            self.assertNotIn('review_after', engine.state['ops']['purchases']['draft'])
            engine.approvals()
            buyer.get.assert_called_with('ToDo', 'TASK-1')
            self.assertEqual(request.call_count, before_retry)
            state = json.loads((run/'state.json').read_text())
            self.assertEqual(state['decisions']['po-review:draft:2']['mode'], 'provider-unavailable')
            self.assertIn('arrival_due', state['ops']['purchases']['submitted'])
            self.assertNotIn('arrival_due', state['ops']['purchases']['draft'])
            self.assertEqual(state['docs']['pending:draft:2'], dict(doctype='ToDo', name='TASK-1'))
            self.assertEqual(native_docs['ToDo', 'TASK-1']['reference_name'], 'PO')
            client.create.assert_called_once()
            client.get.assert_any_call('ToDo', 'TASK-1')
            self.assertEqual(po['docstatus'], 0)
            engine.api['SCM_MGR'].call.assert_not_called()
            engine.state = state
            count = request.call_count
            engine.approvals()
            self.assertEqual(request.call_count, count)
            engine.day = state['ops']['purchases']['draft']['review_after']
            engine.approvals()
            self.assertEqual(po['docstatus'], 1)
            self.assertIn('arrival_due', engine.state['ops']['purchases']['draft'])
            engine.api['SCM_MGR'].call.assert_called_once()
