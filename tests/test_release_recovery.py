"""Interrupt an ordinary recurring inspection and resume with the original ERP records."""
import json
from copy import deepcopy
from unittest.mock import patch

import generate
from integrations.company_run import release_recovery
from test_engine_goals import Goals


class ReleaseRecovery(Goals):
    def ready_transfer(self):
        _, company, _ = self.norvale_fixture(6)
        batch = company.receipt('opening-material', 6)
        company.inspect(batch, 5.5)
        flow = company.company_erp
        return company, batch, flow.state['lots'][batch]

    def test_salvage_retest_cannot_release_original_stock(self):
        from native.company_erp import Pending
        company, batch, lot = self.ready_transfer()
        company.authority.inspections[batch].update(
            custom_retest_of='failed-inspection', custom_rework_execution='execution-1',
            custom_rework_output_batch='salvaged-output')
        with self.assertRaisesRegex(Pending, 'denied'):
            company.company_erp.sync(lot)
        self.assertEqual(company.erp.balance(batch, 'RM', 'Hold'), 6)
        self.assertFalse(any(call[:2] == ('submit', 'Stock Entry') for call in company.erp.calls))

    def test_backdated_draft_cannot_acquire_release_authority(self):
        from native.company_erp import Pending
        company, batch, lot = self.ready_transfer()
        create = company.erp.create
        def backdate(entity, doc):
            if entity == 'Stock Entry' and doc.get('stock_entry_type') == 'Material Transfer':
                doc = dict(doc, posting_date='2020-01-01')
            return create(entity, doc)
        with patch.object(company.erp, 'create', side_effect=backdate), self.assertRaises(Pending):
            company.company_erp.sync(lot)
        self.assertFalse(any(m.get('done') for m in company.company_erp.state['moves'].values()))
        self.assertEqual(company.erp.balance(batch, 'RM', 'RM'), 0)

    def test_lost_native_replies_keep_one_transfer_and_hold_revocation_until_readback(self):
        for lost_at in ('active', 'erp', 'submitted'):
            with self.subTest(lost_at=lost_at):
                company, batch, lot = self.ready_transfer()
                authority = company.authority
                native = authority.scripted_transfer_lease

                def drop_once(request, outcome, commit, **kwargs):
                    result = native(request, outcome, commit, **kwargs)
                    if outcome == lost_at and not dropped:
                        dropped.append(outcome)
                        raise TimeoutError('QMS reply lost')
                    return result

                dropped = []
                if lost_at == 'erp':
                    company.erp.lost.add('Material Transfer')
                with patch.object(authority, 'scripted_transfer_lease', side_effect=drop_once):
                    with self.assertRaises(TimeoutError):
                        company.company_erp.sync(lot)
                    if lost_at != 'submitted':
                        with self.assertRaisesRegex(ValueError, 'blocked by dispatch ordering'):
                            authority.revoke_transfer_inspection(batch)
                    else:
                        authority.revoke_transfer_inspection(batch)
                    company.reload()
                    company.company_erp.reconcile_moves(lot)
                move, = company.company_erp.state['moves'].values()
                self.assertTrue(move['done'])
                self.assertEqual(company.erp.calls.count(('submit', 'Stock Entry', move['target_id'])), 1)
                self.assertIsNone(authority.qms_batch_pointers[batch])
                receipt, = authority.release_leases.values()
                self.assertEqual(receipt['lease_state'], 'submitted')

    def test_armed_release_with_lost_arm_or_missing_erp_leg_remains_pending(self):
        from native.company_erp import Pending

        for fault in ('arm', 'leg'):
            with self.subTest(fault=fault):
                company, batch, lot = self.ready_transfer()
                authority = company.authority
                native = authority.scripted_transfer_lease

                def lose_arm(request, outcome, commit, **kwargs):
                    result = native(request, outcome, commit, **kwargs)
                    if outcome == 'armed':
                        raise TimeoutError('QMS arm reply lost')
                    return result

                original = company.erp.list

                def omit_ledger(entity, filters, *args, **kwargs):
                    if entity == 'Stock Ledger Entry' and filters.get('warehouse') == 'RM':
                        return []
                    return original(entity, filters, *args, **kwargs)

                with patch.object(authority, 'scripted_transfer_lease', side_effect=lose_arm if fault == 'arm' else native), patch.object(
                        company.erp, 'list', side_effect=omit_ledger if fault == 'leg' else original):
                    with self.assertRaises(TimeoutError if fault == 'arm' else Pending):
                        company.company_erp.sync(lot)
                    company.reload()
                    with self.assertRaises(Pending):
                        company.company_erp.sync(lot)
                receipt, = authority.release_leases.values()
                self.assertEqual(receipt['lease_state'], 'armed')
                with self.assertRaisesRegex(ValueError, 'blocked by dispatch ordering'):
                    authority.revoke_transfer_inspection(batch)
                with self.assertRaisesRegex(ValueError, 'requires ERP commit readback'):
                    native(json.loads(receipt['request_json']), 'aborted', None, actor='Administrator')
                submits = [call for call in company.erp.calls if call[:2] == ('submit', 'Stock Entry')]
                self.assertEqual(len(submits), 0 if fault == 'arm' else 1)

    def test_revocation_or_changed_version_before_acquisition_blocks_release(self):
        from native.company_erp import Pending

        for fault in ('revocation', 'version'):
            with self.subTest(fault=fault):
                company, batch, lot = self.ready_transfer()
                authority = company.authority
                native = authority.scripted_transfer_lease

                def invalidate(request, outcome, commit, **kwargs):
                    self.assertEqual(outcome, 'active')
                    if fault == 'revocation':
                        authority.revoke_transfer_inspection(batch)
                    else:
                        authority.inspections[batch]['modified'] = 'signed-2'
                    return native(request, outcome, commit, **kwargs)

                with patch.object(authority, 'scripted_transfer_lease', side_effect=invalidate), self.assertRaisesRegex(Pending, 'denied'):
                    company.company_erp.sync(lot)
                self.assertFalse(any(call[:2] == ('submit', 'Stock Entry') for call in company.erp.calls))
                self.assertEqual(company.erp.balance(batch, 'RM', 'Hold'), 6)

    def test_stock_entry_posted_before_arm_cannot_finalize(self):
        from native.company_erp import Pending

        company, batch, lot = self.ready_transfer()
        authority = company.authority
        native = authority.scripted_transfer_lease

        def intervene(request, outcome, commit, **kwargs):
            self.assertEqual(outcome, 'active')
            result = native(request, outcome, commit, **kwargs)
            company.erp.update('Stock Entry', request['transfer']['stock_entry'], {'docstatus': 1})
            raise TimeoutError('interrupted before arm')

        with patch.object(authority, 'scripted_transfer_lease', side_effect=intervene), self.assertRaises(TimeoutError):
            company.company_erp.sync(lot)
        company.reload()
        with self.assertRaisesRegex(Pending, 'no matching armed QMS transfer attempt'):
            company.company_erp.sync(lot)
        receipt, = authority.release_leases.values()
        self.assertEqual(receipt['lease_state'], 'active')
        self.assertIsNone(receipt['commit_json'])
        self.assertEqual(authority.qms_batch_pointers[batch], receipt['name'])

    def test_qms_rejects_finalization_before_arm_and_preserves_first_commit(self):
        company, _, lot = self.ready_transfer()
        company.company_erp.sync(lot)
        authority = company.authority
        receipt, = authority.release_leases.values()
        request = json.loads(receipt['request_json'])
        commit = json.loads(receipt['commit_json'])
        native = authority.scripted_transfer_lease
        with self.assertRaisesRegex(ValueError, 'differs from the finalized receipt'):
            bad = deepcopy(commit)
            bad['legs']['Outward']['ledger'] = 'another ledger row'
            native(request, 'submitted', bad)
        request['id'] = 'a-fresh-lease'
        self.assertEqual(native(request, 'active', None)['state'], 'active')
        with self.assertRaisesRegex(ValueError, 'Invalid dispatch lease transition'):
            native(request, 'submitted', commit)
        self.assertTrue(native(request, 'armed', None)['submit_permitted'])
        with self.assertRaisesRegex(ValueError, 'requires matching ERP commit'):
            native(request, 'submitted', None)


    def test_interrupted_release_is_transferred_once_across_restart(self):
        self.exercise_cut(hard_exit=False)

    def test_process_exit_immediately_after_persisting_cut(self):
        self.exercise_cut(hard_exit=True)

    def exercise_cut(self, hard_exit):
        clock, company, _ = self.norvale_fixture(6)
        batch = company.receipt('opening-material', 6)
        company.inspect(batch, 5.5)
        company.additions = clock
        company.config.update(days=1)
        (company.run/'env').mkdir(exist_ok=True)
        (company.run/'env/company-sources.json').write_text('{}')
        company.state['completed'] = []
        company.persist()
        (company.run/'config.json').write_text(json.dumps(company.config))
        company.events = lambda: [(0, 'd0000:inspections', company.company_erp.inspections)]
        with patch('company.Engine', return_value=company):
            if hard_exit:
                save = release_recovery.save
                def exit_after_save(path, cut):
                    save(path, cut)
                    raise SystemExit(9)
                with patch.object(release_recovery, 'save', exit_after_save), self.assertRaises(SystemExit):
                    release_recovery.main(company.run)
            else:
                with self.assertRaisesRegex(InterruptedError, release_recovery.INTERRUPTION):
                    release_recovery.main(company.run)
            cut = json.loads((company.run/'ordinary-release-interruption.json').read_text())
            self.assertEqual(cut['batch'], batch)
            self.assertFalse(release_recovery.transfers(company, cut))
            company.reload()
            company.log('controller_started')
            for _ in range(2):
                generate.main(['--run', str(company.run)])
        name = release_recovery.verified_resume(company, cut)
        self.assertTrue(name)
        self.assertEqual(company.erp.balance(batch, 'RM', 'RM'), 6)
        doc = company.erp.docs['Stock Entry', name]
        company.erp.docs['Stock Entry', 'duplicate'] = dict(doc, name='duplicate')
        self.assertIsNone(release_recovery.verified_resume(company, cut))

    def test_archive_requires_two_active_movements_under_the_owning_source(self):
        from genealogy import ShipmentHistory
        from integrations.lake.boundary import project
        pin = dict(id='QI', version='v1', item='X', batch='B', qty=3, uom='Nos',
                   authorized=True, status='released', approver='approver', qms_source='lab')
        cut = dict(batch='B', item='X', uom='Nos', qty=3, source='Q', target='FG', disposition=pin)
        transfer = project('Stock Entry', dict(name='T', company='C', posting_date='2027-01-01',
            stock_entry_type='Material Transfer', docstatus=1, remarks=json.dumps(pin), items=[dict(
                name='line', item_code='X', stock_uom='Nos', batch_no='B', qty=3, transfer_qty=3,
                s_warehouse='Q', t_warehouse='FG')]))
        source = [('erp', 'Stock Entry', transfer), ('lab', 'Quality Inspection',
            dict(name='QI', modified='v1', docstatus=1, status='Accepted', workflow_state='Released',
                 batch_no='B', item_code='X', custom_lot_quantity=3, custom_lot_uom='Nos', modified_by='approver'))]
        for wh, direction, qty in (('Q','Outward',-3),('FG','Inward',3)):
            fields = dict(docstatus=1, is_cancelled=0, voucher_type='Stock Entry', voucher_no='T',
                          voucher_detail_no='line', item_code='X', warehouse=wh)
            source.extend([('erp','Serial and Batch Bundle',dict(fields,name='bundle-'+wh,
                type_of_transaction=direction, entries=[{'warehouse':wh,'qty':qty,'batch_no':'B'}])),
                ('erp','Stock Ledger Entry', dict(fields,name='ledger-'+wh, stock_uom='Nos', actual_qty=qty,
                    company='C', posting_date='2027-01-01', serial_and_batch_bundle='bundle-'+wh))])
        def history(records):
            rows = [dict(source=site, entity=entity, native_id=doc['name'], ingested_at='1',
                         payload=json.dumps(doc)) for site,entity,doc in records]
            return ShipmentHistory(rows, 'erp')

        def archive(records):
            return history(records).transfer_lineage('T')

        self.assertIsNone(release_recovery.archived_resume(history(source), 'T', cut))
        self.assertEqual(archive(source)['legs']['Inward']['ledger'],'ledger-FG')
        self.assertEqual(archive(source)['authorization_at_transfer'], 'unresolved')
        with patch.dict(source[1][2], report_date='2027-02-01'):
            self.assertIsNone(archive(source))
        revoked = dict(source[1][2], modified='v2', workflow_state='Revoked')
        self.assertEqual(archive(source + [('lab', 'Quality Inspection', revoked)])['authorization_at_transfer'],
                         'unresolved')
        self.assertIsNone(archive(source[:-1]))
        self.assertIsNone(archive(source[:-1] + [('other-erp', *source[-1][1:])]))
        source[-1][2]['actual_qty'] = 4
        self.assertIsNone(archive(source))

    def test_scheduled_delta_confirms_exact_native_transfer_and_receipt(self):
        from genealogy import ShipmentHistory
        from integrations.company_run.lake import scheduled_scan
        from integrations.lake.adapter import Lake
        from integrations.lake.boundary import Frappe

        company, batch, lot = self.ready_transfer()
        authority = company.authority
        pin = company.company_erp.disposition(lot, 'QA')
        company.company_erp.sync(lot)
        move, = company.company_erp.state['moves'].values()
        name = move['target_id']
        cut = dict(batch=batch, item=lot['item'], uom=lot['uom'], qty=6, source='Hold',
                   target='RM', disposition=pin)
        qms_document = deepcopy(authority.inspections[batch])
        # Native wall clocks can disagree by arbitrary amounts.
        qms_document['creation'] = '2041-01-01 01:00:00'
        records = dict(company.erp.docs)
        receipt, = authority.release_leases.values()
        receipt.update(creation='2040-01-01 01:00:00', modified='2040-01-01 01:00:01')
        entities = ('Stock Entry', 'Serial and Batch Bundle', 'Stock Ledger Entry')
        for (entity, _), doc in records.items():
            if entity in entities:
                doc['modified'] = '2021-01-01 01:00:00'

        env = company.run/'env'
        env.mkdir(exist_ok=True)
        config = env/'analytics.json'
        root = company.run/'lake'
        config.write_text(json.dumps(dict(
            erp=dict(source='erp-a', reader=dict(url='erp', api_key='reader', api_secret='test'), entities=entities),
            qms=dict(source='qms-a', reader=dict(url='qms', api_key='reader', api_secret='test'),
                     entities=['Quality Inspection']),
            plm=dict(source='plm', reader={}, parts=[]))))
        (env/'company-sources.json').write_text(json.dumps(dict(plm_source='plm', plm={}, products={})))
        (env/'planning-report.json').write_text(json.dumps(dict(settings=str(config), archive=str(root))))

        def connect(reader, url, *args, **kwargs):
            reader.url = url

        def documents(reader, entity):
            if reader.url == 'erp':
                return [doc for (kind, _), doc in records.items() if kind == entity]
            return {'Quality Inspection': [qms_document], 'Norvale Dispatch Lease': [receipt]}.get(entity, [])

        with (patch.object(Frappe, '__init__', connect), patch.object(Frappe, 'documents', documents),
              patch('integrations.company_run.publications.collect'),
              patch('integrations.company_run.publications.publication_source'),
              patch('integrations.lake.plm_reader.scan_bom'), patch('integrations.plm.PLM')):
            scheduled_scan(company, 1)
            self.assertEqual(company.state['lake_scans']['errors'], [])
            authority.revoke_transfer_inspection(batch)
            qms_document.update(modified='revoked-2', workflow_state='Revoked')
            scheduled_scan(company, 2)
            self.assertEqual(company.state['lake_scans']['errors'], [])

        rows = Lake(root/'delta', {})._rows()
        traced = release_recovery.archived_resume(ShipmentHistory(rows, 'erp-a'), name, cut)
        self.assertEqual(traced['authorization_at_transfer'], 'confirmed')
        self.assertEqual(traced['authorization_receipt'], ['qms-a', 'Norvale Dispatch Lease', receipt['name']])
        future = deepcopy(rows)
        for row in future:
            if row['entity'] == 'Quality Inspection':
                observation = json.loads(row['payload'])
                observation['report_date'] = '2041-01-01'
                row['payload'] = json.dumps(observation)
        self.assertIsNone(release_recovery.archived_resume(ShipmentHistory(future, 'erp-a'), name, cut))
        missing_date = deepcopy(rows)
        for row in missing_date:
            if row['entity'] == 'Quality Inspection':
                q = json.loads(row['payload'])
                q.pop('report_date', None)
                row['payload'] = json.dumps(q)
        self.assertIsNone(release_recovery.archived_resume(ShipmentHistory(missing_date, 'erp-a'), name, cut))
        for field, value in (
            ('kind', None), ('lease_state', 'armed'), ('name', 'wrong-receipt'),
            ('transfer.erp_source', 'other-erp'), ('transfer.stock_entry', 'wrong-entry'),
            ('transfer.row', 'wrong-row'), ('transfer.source', 'wrong-warehouse'),
            ('transfer.posting_date', '1990-01-01'), ('scopes.0.revision', 'wrong-spec'),
            ('scopes.0.version', 'wrong-version'), ('commit.quantity', 7),
            ('commit.legs.Outward.ledger', 'wrong-ledger'), ('commit.legs.Inward.bundle', 'wrong-bundle'),
        ):
            with self.subTest(field=field):
                modified = deepcopy(rows)
                row, = (row for row in modified if row['entity'] == 'Norvale Dispatch Lease')
                payload = json.loads(row['payload'])
                target = payload
                *path, key = field.split('.')
                for part in path:
                    target = target[int(part)] if isinstance(target, list) else target[part]
                target[key] = value
                row['payload'] = json.dumps(payload)
                history = ShipmentHistory(modified, 'erp-a')
                self.assertEqual(history.transfer_lineage(name)['authorization_at_transfer'], 'unresolved')
                self.assertIsNone(release_recovery.archived_resume(history, name, cut))

        wrong_site = [dict(row, source='qms-b') if row['entity'] == 'Norvale Dispatch Lease' else row for row in rows]
        self.assertIsNone(release_recovery.archived_resume(ShipmentHistory(wrong_site, 'erp-a'), name, cut))
