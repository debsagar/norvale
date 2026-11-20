"""Finance captures existing ERP evidence at the ordinary settlement boundary."""
from native.company_bank import TRANSACTION
from native.company_records import SalesRecords
from norvale_goals_fixture import start_queue
from test_engine_goals import DAY, Goals, Providers


class SalesCapture(Goals):
    def test_ordinary_phase_requires_independent_bank_evidence(self):
        _, company = self.norvale_to_cash()
        clock = start_queue(company.run / 'ordinary-capture', company, Providers())
        company.additions = clock
        day = 15
        minute = day * DAY + 720
        for _ in range(2):
            company.run_phase(day, f'd{day:04}:settlements', lambda: None)
        self.assertEqual(clock.private_truth()['answers']['C1-paid'], 150)
        self.assertFalse(clock.private_truth(as_of=minute - 1)['answers'])
        self.assertEqual(len(clock.export()['records']), 6)
        payment = next(row for row in clock.export()['records'] if row['kind'] == 'payment')
        statement = company.erp.docs[TRANSACTION, payment['statement_id']]
        statement['payment_entries'][0]['payment_entry'] = 'different-payment'
        with self.assertRaisesRegex(ValueError, 'does not confirm'):
            SalesRecords(company).read(payment['system'], payment['native_id'])

    def test_retry_preserves_capture_order_when_new_payment_appears(self):
        from test_engine_goals import PowerLoss
        _, company = self.norvale_to_cash()
        invoices = list(company.state['ops']['invoices'].values())
        payment = invoices[0]['payments'].pop()
        root = company.run / 'capture-restart'
        def interrupt(stage, op):
            if stage == 'result_persisted' and op['action']['kind'] == 'payment':
                raise PowerLoss()
        clock = start_queue(root, company, Providers(), boundary=interrupt)
        clock.schedule('first', 'company-sales-readback', 15 * DAY, role='FINANCE', payload={})
        with self.assertRaises(PowerLoss):
            clock.run(until=15 * DAY)
        invoices[0]['payments'].append(payment)
        clock = start_queue(root, company, Providers())
        clock.run(until=15 * DAY)
        self.assertEqual(clock.private_truth()['answers']['C1-paid'], 75)
        clock.schedule('next', 'company-sales-readback', 16 * DAY, role='FINANCE', payload={})
        clock.run(until=16 * DAY)
        self.assertEqual(clock.private_truth()['answers']['C1-paid'], 150)

    def test_migrate_legacy_name_with_two_document_types(self):
        from unittest.mock import Mock
        from native.company_records import SOURCES, SYSTEM
        records = SalesRecords(None)
        records.confirmed = lambda: [('shared', 'shipment', {}), ('shared', 'invoice', {})]
        records.read = lambda system, name: dict(system=system, native_id=name, kind='invoice')
        clock = Mock(state={'company_sales_readbacks': ['shared']}, event={'id':'migrate'})
        clock.export.return_value = {'records':[dict(system=SYSTEM, native_id='shared', kind='shipment')]}
        clock.load_consumer_decision.return_value = None
        clock.consumer_write_prefix.return_value = []
        clock.save_consumer_decision.side_effect = lambda event, decision: decision
        records.capture(clock)
        clock.confirm.assert_called_once_with(SOURCES['invoice'], 'invoice', 'shared', {})

    def test_pre_worklist_journal_replays_pending_and_defers_new_payment(self):
        from unittest.mock import patch
        from native.company_records import SYSTEM
        from test_engine_goals import PowerLoss
        import sqlite3
        import json

        _, company = self.norvale_to_cash()
        invoices = list(company.state['ops']['invoices'].values())
        late = invoices[0]['payments'].pop()
        root = company.run / 'legacy-capture'

        def old_capture(records, clock):
            seen = clock.state.setdefault('company_sales_readbacks', [])
            for native_id, kind, shipment in records.confirmed():
                if native_id in seen:
                    continue
                record = records.project(native_id, kind, shipment)
                fields = {k: v for k, v in record.items() if k not in ('system', 'native_id', 'kind')}
                clock.confirm(SYSTEM, kind, native_id, fields)
                seen.append(native_id)

        def interrupt(stage, op):
            if stage == 'intent_persisted' and op['action']['kind'] == 'payment':
                raise PowerLoss()

        clock = start_queue(root, company, Providers(), boundary=interrupt)
        clock.schedule('legacy', 'company-sales-readback', 15 * DAY, role='FINANCE', payload={})
        with patch.object(SalesRecords, 'capture', old_capture), self.assertRaises(PowerLoss):
            clock.run(until=15 * DAY)
        with sqlite3.connect(root / 'engine.sqlite3') as db:
            operations = [json.loads(row[0]) for row in db.execute('SELECT data FROM writes')]
            self.assertTrue(any(op['status'] == 'confirmed' for op in operations))
            self.assertEqual(operations[-1]['status'], 'pending')
            self.assertEqual(db.execute('SELECT count(*) FROM decisions').fetchone()[0], 0)
        invoices[0]['payments'].append(late)
        pending_payment = operations[-1]['read_target']
        statement = next(row for (doctype, _), row in company.erp.docs.items()
                         if doctype == TRANSACTION and any(
                             ref.get('payment_entry') == pending_payment
                             for ref in row.get('payment_entries', [])))
        reference = statement['payment_entries'][0]['payment_entry']
        statement['payment_entries'][0]['payment_entry'] = 'different-payment'
        clock = start_queue(root, company, Providers())
        with self.assertRaisesRegex(ValueError, 'does not confirm'):
            clock.run(until=15 * DAY)
        statement['payment_entries'][0]['payment_entry'] = reference
        clock = start_queue(root, company, Providers())
        clock.run(until=15 * DAY)
        self.assertEqual(clock.private_truth()['answers']['C1-paid'], 75)
        clock.schedule('next', 'company-sales-readback', 16 * DAY, role='FINANCE', payload={})
        clock.run(until=16 * DAY)
        self.assertEqual(clock.private_truth()['answers']['C1-paid'], 150)
        records = clock.export()['records']
        self.assertEqual(len(records), 6)
        clock.run(until=16 * DAY)
        self.assertEqual(clock.export()['records'], records)
