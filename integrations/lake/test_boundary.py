import json
import tempfile
import unittest
from pathlib import Path
from boundary import ingest, project, trace

class BoundaryTest(unittest.TestCase):
    def test_replay_late_cancel_filter_failure_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            doc = {'name':'D1','modified':'2027-01-02','docstatus':1,'secret':'no','items':[{'batch_no':'B1','against_sales_order':'S1','password':'no'}]}
            self.assertEqual(ingest(p, 'Delivery Note', lambda: [doc]), 1)
            self.assertEqual(ingest(p, 'Delivery Note', lambda: [doc]), 0)
            late = dict(doc, modified='2026-01-01', docstatus=2)
            self.assertEqual(ingest(p, 'Delivery Note', lambda: [late]), 1)
            def fail(): raise RuntimeError('source unavailable')
            with self.assertRaises(RuntimeError): ingest(p, 'Delivery Note', fail)
            state = json.loads((p/'state.json').read_text())['Delivery Note']
            self.assertTrue(state['error'])
            self.assertEqual(state['watermark'], '2027-01-02')
            self.assertEqual(ingest(p, 'Delivery Note', lambda: [late]), 0)
            self.assertNotIn('secret', json.dumps(project('Delivery Note',doc)))
            self.assertNotIn('password', json.dumps(project('Delivery Note',doc)))
    def test_quality_readback_preserves_measurement_authorship_and_overrides(self):
        reading = dict(specification="pH", reading_1="5.5", manual_inspection=0,
                       formula_based_criteria=1)
        inspection = dict(name="QI-1", inspected_by="analyst", modified_by="approver",
                          manual_inspection=0)
        observed = project("Quality Inspection", {
            **inspection, "readings": [{**reading, "private": "omit"}], "private": "omit"
        })
        for field, value in inspection.items():
            self.assertEqual(observed[field], value)
        self.assertEqual(observed["readings"], [reading])
        self.assertNotIn("private", observed)

    def test_instances_keep_independent_watermarks(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = {'name':'QI-1','modified':'2027-01-02'}
            for source in ('erpnext',"qms-east's site"):
                self.assertEqual(ingest(tmp,'Quality Inspection',lambda: [doc],source=source),1)
            state = json.loads((Path(tmp)/'state.json').read_text())
            self.assertEqual({e['source'] for e in state.values()},{'erpnext',"qms-east's site"})
            self.assertEqual(len(state),2)

    def test_overlapping_native_scans_keep_separate_freshness(self):
        from adapter import Lake
        with tempfile.TemporaryDirectory() as tmp:
            entity, source = 'InvenTree Publication', 'plm'
            doc = dict(name='7', effective_on='2027-03-01', version='content-hash')
            def scan(fetch, scope):
                return ingest(tmp, entity, fetch, source=source, scope=scope,
                              projection=lambda entity, record: record)
            self.assertEqual(scan(lambda:[doc], None), 1)
            self.assertEqual(scan(lambda:[doc], {'family':7}), 0)
            def unavailable():
                raise ConnectionError('disconnected')
            with self.assertRaises(ConnectionError):
                scan(unavailable, {'family':7})
            self.assertEqual(scan(lambda:[doc], None), 0)
            entries = json.loads((Path(tmp)/'state.json').read_text()).values()
            by_scope = {e.get('scope', {}).get('family'):e for e in entries}
            self.assertIsNone(by_scope[None]['error'])
            self.assertEqual(by_scope[7]['error'], 'ConnectionError')
            saved, = Lake(Path(tmp)/'delta',{})._rows()
            self.assertEqual(saved['effective_at'], '2027-03-01')
            self.assertEqual(saved['source_recorded_at'], '')

    def test_bundle_resolves_native_lots_without_guessing(self):
        bundle = project('Serial and Batch Bundle', {'name':'SB-1', 'item_code':'L',
            'entries':[{'batch_no':'LOT-3','qty':-2,'warehouse':'FG','private':'omit'},
                       {'batch_no':'LOT-4','qty':-1,'warehouse':'FG'}]})
        self.assertNotIn('private', bundle['entries'][0])
        docs = {'Delivery Note':[{'name':'DN-1','items':[{'serial_and_batch_bundle':'SB-1'}]}],
                'Serial and Batch Bundle':[bundle]}
        edges = trace(docs)
        self.assertIn(('Delivery Note','DN-1','Serial and Batch Bundle','SB-1'), edges)
        for lot in ('LOT-3','LOT-4'):
            self.assertIn(('Serial and Batch Bundle','SB-1','Batch',lot), edges)
        self.assertEqual(bundle['entries'][0]['qty'], -2)

    def test_commercial_links_keep_native_allocations_and_omit_bank_details(self):
        data = {'Payment Entry': [project('Payment Entry', {'name':'PAY','paid_from':'private account',
                    'reference_no':'bank identifier','posting_date':'2027-02-01','paid_amount':13,
                    'references':[{'reference_doctype':'Purchase Invoice','reference_name':'PI',
                                   'allocated_amount':8,'private':'hidden'}]})],
                'Purchase Invoice': [project('Purchase Invoice', {'name':'PI','items':[{
                    'purchase_receipt':'PR','pr_detail':'PR-row','purchase_order':'PO','po_detail':'PO-row'}]})],
                'Stock Ledger Entry': [project('Stock Ledger Entry',dict(name='SLE',voucher_type='Purchase Receipt',
                                     voucher_no='PR',voucher_detail_no='PR-row',actual_qty=4))]}
        edges = trace(data)
        self.assertIn(('Payment Entry','PAY','Purchase Invoice','PI'),edges)
        self.assertIn(('Purchase Invoice','PI','Purchase Receipt','PR'),edges)
        self.assertIn(('Stock Ledger Entry','SLE','Purchase Receipt','PR'),edges)
        self.assertEqual(data['Payment Entry'][0]['references'][0]['allocated_amount'],8)
        self.assertEqual(data['Purchase Invoice'][0]['items'][0]['pr_detail'],'PR-row')
        for value in ('private account','bank identifier','hidden'):
            self.assertNotIn(value,json.dumps(data))

    def test_explicit_links_only(self):
        docs={'Work Order':[{'name':'W','sales_order':'S'}], 'Stock Entry':[{'name':'E','work_order':'W','items':[{'batch_no':'B','is_finished_item':1}]}], 'Quality Inspection':[{'name':'Q','reference_type':'Stock Entry','reference_name':'E'}], 'Delivery Note':[{'name':'D','items':[{'batch_no':'B'}]}]}
        edges=trace(docs)
        self.assertIn(('Stock Entry','E','Work Order','W'),edges)
        self.assertIn(('Delivery Note','D','Batch','B'),edges)
        self.assertIn(('Quality Inspection','Q','Stock Entry','E'),edges)
        self.assertNotIn(('Work Order','W','Batch','B'),edges)

class FrappeTest(unittest.TestCase):
    def test_page_deletion_does_not_skip_unseen_name(self):
        from boundary import Frappe
        from urllib.parse import parse_qs, urlparse
        client = Frappe('http://localhost','key','secret')
        names = [f'QI-{i:04}' for i in range(102)]
        def page(path):
            query = parse_qs(urlparse(path).query)
            if 'filters' in query:
                self.assertEqual(json.loads(query['filters'][0]), [['name','>','QI-0099']])
                return [{'name':n} for n in names if n > 'QI-0099']
            first = names[:100]
            del names[0]
            return [{'name':n} for n in first]
        client.get = page
        self.assertEqual(list(client.names('Quality Inspection')), [f'QI-{i:04}' for i in range(102)])

    def test_only_exact_native_absence_confirms_deletion(self):
        from boundary import Frappe
        from urllib.error import HTTPError
        from unittest.mock import patch
        from io import BytesIO
        client = Frappe('http://localhost','key','secret')
        name = 'QA-7'
        for status, kind, message, outcome in [
                (403,'PermissionError','',None), (404,'DoesNotExistError','Quality Action QA-7 not found',True),
                (404,'DoesNotExistError','DocType Quality Action not found',False),
                (404,'HTTPError','Not found',False)]:
            body = json.dumps({'exc_type':kind,'_server_messages':json.dumps([json.dumps({'message':message})])})
            error = HTTPError('http://localhost',status,'',{},BytesIO(body.encode()))
            with self.subTest(status=status,kind=kind,message=message), patch.object(client,'get',side_effect=error):
                if outcome is False:
                    with self.assertRaises(HTTPError):
                        client.missing('Quality Action',name)
                else:
                    self.assertEqual(client.missing('Quality Action',name),
                                     {'name':name,'source_absent':True} if outcome else None)

    def test_authenticated_api(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from threading import Thread
        from boundary import Frappe
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get('Authorization') != 'token key:secret':
                    self.send_response(403); self.end_headers(); return
                data = {'name':'D1','modified':'2027-01-01','secret':'drop'} if '?' not in self.path else [{'name':'D1'}]
                body = json.dumps({'data':data}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        server = HTTPServer(('127.0.0.1',0),Handler)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            url = 'http://127.0.0.1:' + str(server.server_port)
            self.assertEqual(list(Frappe(url,'key','secret').documents('Delivery Note'))[0]['name'],'D1')
            with self.assertRaises(Exception): list(Frappe(url,'bad','secret').documents('Delivery Note'))
        finally:
            server.shutdown(); thread.join(); server.server_close()

if __name__ == '__main__': unittest.main()
