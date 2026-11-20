"""Independent acceptance checks must detect native records missing from the checkpoint."""
import json
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from integrations.company_run.verify import active_releases, measured_retests, reconcile_completions, revoked_allocation_held, ordinary_revocations, verified_maintenance


class NativeReconciliationTests(unittest.TestCase):
    def test_shipment_archive_coverage_uses_current_source_and_exact_delivery_rows(self):
        from integrations.company_run.verify import shipment_archive_coverage

        line = dict(name='row-1', item_code='A', qty=4, stock_qty=4, so_detail='SO-row')
        doc = dict(name='D', docstatus=1, modified='v1', items=[line])
        key = ('erp', 'Delivery Note', 'D')
        latest = {key: doc, ('another ERP', 'Delivery Note', 'D'): doc}
        history = SimpleNamespace(erp='erp', latest=latest)
        def check():
            return shipment_archive_coverage(history, {'D': doc})

        self.assertFalse(any(check().values()))
        for snapshot in (dict(doc, modified='v0'), dict(doc, items=[dict(line, stock_qty=2)]),
                         dict(doc, items=[dict(line, so_detail='other')])):
            latest[key] = snapshot
            self.assertEqual(check()['changed'], ['D'])
        for snapshot in (dict(doc, docstatus=2), dict(doc, is_return=1), dict(source_absent=True)):
            latest[key] = snapshot
            self.assertEqual(check()['missing'], ['D'])
        latest.pop(key)
        latest['erp', 'Delivery Note', 'other'] = dict(doc, name='other')
        self.assertEqual(check(), dict(missing=['D'], unexpected=['other'], changed=[]))

    def test_revoked_lot_can_have_shipped_before_the_current_hold(self):
        rev = dict(batch='B', inspection='I', draft='D2', version='2027-01-02 10:00:00')
        inspection = dict(docstatus=1, name='I', workflow_state='Revoked', modified=rev['version'],
                          item_code='SKU', batch_no='B')
        draft = dict(name='D2', docstatus=0, items=[dict(qty=4, item_code='SKU', batch_no='B')])
        pin = dict(batch='B', qty=4, disposition=dict(id='I', status='released', batch='B', qms_source='qa',
                   version='2027-01-01 12:00:00', qty=10, item='SKU'))
        draft['custom_norvale_quality_releases'] = json.dumps([pin])
        lease = ('qa', 'Norvale Dispatch Lease', 'L')
        receipt = dict(delivery='D1', lease_state='submitted', modified='2027-01-02 09:59:59')
        history = SimpleNamespace(latest={lease:receipt})
        trace = dict(shipment=['erp','Delivery Note','D1'], unresolved=[],
                     lots=[dict(batch='B', dispatch_lease=lease)])
        def check(ledger=()):
            return revoked_allocation_held(rev, draft, inspection, ledger, [trace], history, 'qa')
        self.assertTrue(check())
        self.assertFalse(check(['ledger row for D2']))
        for change in ({'id':'other'}, {'qms_source':'other'}, {'version':rev['version']}, {'qty':3}):
            modified = dict(pin, disposition={**pin['disposition'], **change})
            draft['custom_norvale_quality_releases'] = json.dumps([modified])
            self.assertFalse(check(), change)
        draft['custom_norvale_quality_releases'] = json.dumps([dict(pin, qty=3)])
        self.assertFalse(check())
        draft['custom_norvale_quality_releases'] = json.dumps([pin])
        for field, wrong in [('modified', rev['version']), ('modified','2027-01-03'),
                             ('delivery','D2'), ('lease_state','armed')]:
            with self.subTest(field=field, wrong=wrong):
                value = receipt[field]
                receipt[field] = wrong
                self.assertFalse(check())
                receipt[field] = value
        receipt['modified'] = '2027-01-03 10:00:00'
        trace['lots'][0]['quality_release'] = ['qa', 'Quality Inspection', 'new', '2027-01-02 14:00:00']
        self.assertTrue(check())
        trace['lots'][0]['quality_release'][3] = '2027-01-04'
        self.assertFalse(check())
        receipt['modified'] = '2027-01-02 09:59:59'
        draft['items'][0]['batch_no'] = 'OTHER'
        self.assertFalse(check())

    def test_ordinary_withdrawal_needs_matching_native_and_archived_versions(self):
        from integrations.lake.genealogy import ShipmentHistory
        from integrations.qms.disposition_history import ENTITY
        source, batch, item, sample = 'annex-qa', 'salvage', 'product', 'QI-1'
        released, revoked = '2027-02-10 12:00:00', '2027-02-11 10:00:00'
        scope = dict(id=sample, qms_source=source, qms_batch='original', batch=batch, version=released,
                     status='released', item=item, qty=8, uom='Nos', approver='approver')
        pin = dict(batch=batch, disposition=scope, qty=3)
        doc = dict(name=sample, docstatus=1, status='Accepted', workflow_state='Revoked',
                   modified=revoked, item_code=item, batch_no='original', custom_rework_output_batch=batch)
        def snapshot(version, state, site):
            return dict(source=site, entity='Quality Inspection', native_id=sample, ingested_at=version,
                        payload=json.dumps(dict(doc, modified=version, workflow_state=state)))
        rows = [snapshot(released, 'Released', source), snapshot(revoked, 'Revoked', source)]
        def event(name, version, state, site=source, **overrides):
            payload = dict(name=name, inspection=sample, inspection_version=version, disposition=state,
                           item_code=item, batch_no='original', output_batch=batch, spec_revision='SPEC',
                           lot_quantity=8, lot_uom='Nos', approver='approver')
            payload.update(overrides)
            return dict(source=site, entity=ENTITY, native_id=name, ingested_at=revoked,
                        payload=json.dumps(payload))
        release_event = event('1', released, 'Released')
        revoke_event = event('2', revoked, 'Revoked', approver='reviewer')
        native_only = [rows[1], release_event, revoke_event]
        draft = dict(name='DN', docstatus=0, custom_norvale_quality_releases=json.dumps([pin]),
                     items=[dict(batch_no=batch, item_code=item, qty=3)])
        site = dict(qms_source=source, qms_reader=dict(url='http://annex'))
        engine = Mock()
        warehouse = Mock()
        warehouse.get.return_value = draft
        warehouse.list.return_value = []
        engine.api = {'WAREHOUSE':warehouse}
        engine.company_erp.state = dict(shipments={'attempt':dict(rows=[pin], allocation_change=dict(delivery='DN'))},
                                        lots={batch:dict(item=item)})
        engine.company_erp.authority.quality_route.return_value = (site, {})
        primary = dict(source='primary', reader=dict(url='http://primary'))
        def check(archive):
            result, = ordinary_revocations(engine, primary, ShipmentHistory(archive, 'erp'), [])
            return result['verified']
        with patch('integrations.qms.adapter.QMS') as client:
            client.return_value.get.return_value = doc
            self.assertTrue(check(rows))
            client.assert_called_with(url='http://annex')
            self.assertFalse(check([snapshot(released, 'Released', 'primary'), rows[1]]))
            self.assertFalse(check(rows[:1]))
            self.assertTrue(check(native_only))
            self.assertFalse(check([rows[1], revoke_event, event('1', released, 'Released', site='primary')]))
            self.assertFalse(check([rows[1], revoke_event, event('1', '2027-02-09', 'Released')]))
            self.assertFalse(check([rows[1], release_event]))
            self.assertFalse(check([rows[1], release_event, event('2', revoked, 'Revoked', output_batch='other')]))
            for changed in (dict(qty=9), dict(uom='Kg'), dict(approver='someone else')):
                draft['custom_norvale_quality_releases'] = json.dumps([dict(pin, disposition={**scope, **changed})])
                self.assertFalse(check(native_only), changed)
            draft['custom_norvale_quality_releases'] = json.dumps([pin])
            warehouse.list.return_value = [dict(name='posted-ledger-movement')]
            self.assertFalse(check(rows))
            warehouse.list.return_value = []
            draft['custom_norvale_quality_releases'] = json.dumps([dict(pin, disposition=dict(scope, version='2027-02-09'))])
            self.assertFalse(check(rows))

    def test_restarts_are_verified_at_each_registered_site_with_colliding_ids(self):
        from native.company_erp import digest
        engine = Mock()
        equipment = engine.company_erp.equipment
        equipment.cases = {}
        sites = {}
        for site in ('east', 'west'):
            source = Mock()
            source.settings.return_value = {'maintenance_staff':{'reader':'read-only'}}
            source.ticket_fields.return_value = ('fault', site)
            evidence = dict(ref=site + '/issues/1#comment-2')
            source.restart_evidence.return_value = evidence
            source.native_center.return_value = 1
            reader = source.operator.return_value
            reader.source_id = site
            reader.read.return_value = dict(date_end='restored', workcenter_id=[1, 'center'],
                description='Fault 1\nVerified restart gitea:' + digest(evidence['ref']))
            center = site + ':1'
            sites[center] = source
            ref = dict(source_id=site, native_id=1)
            equipment.cases[site] = dict(center=center, fault=dict(ref, description='Fault 1'), restart=ref,
                                         verification=evidence, ticket={'number':1})
        equipment.scoped_authority.side_effect = sites.__getitem__
        self.assertTrue(verified_maintenance(engine))
        equipment.cases['west']['restart'] = dict(source_id='east', native_id=1)
        self.assertFalse(verified_maintenance(engine))

    def test_current_release_uses_latest_observation_at_the_owning_site(self):
        release = dict(source='qms', entity='Quality Inspection', native_id='Q1', ingested_at='01',
                       payload=json.dumps(dict(docstatus=1, status='Accepted', workflow_state='Released')))
        self.assertEqual(active_releases([release], 'qms'), ['Q1'])
        for update in [dict(docstatus=1, status='Accepted', workflow_state='Revoked'),
                       dict(docstatus=2, status='Accepted', workflow_state='Released'),
                       dict(source_absent=True)]:
            rows = [dict(release, ingested_at='02', payload=json.dumps(update)),
                    dict(release, source='other'), release]
            self.assertFalse(active_releases(rows, 'qms'))

    def test_stale_release_observation_after_revocation_cannot_reactivate_lot(self):
        def observed(state, version, ingested):
            return dict(source='qms', entity='Quality Inspection', native_id='Q1', ingested_at=ingested,
                        payload=json.dumps(dict(docstatus=1, status='Accepted', workflow_state=state,
                                                modified=version)))
        released = observed('Released', '2027-01-01 12:01:00', '03')
        for version in ('2027-01-01 12:02:00', '2027-01-01 12:01:00', 'unknown'):
            with self.subTest(version=version):
                self.assertFalse(active_releases([released, observed('Revoked', version, '02')], 'qms'))
        absent = dict(released, ingested_at='02', payload=json.dumps({'source_absent': True}))
        prior = dict(released, ingested_at='01')
        self.assertFalse(active_releases([prior, absent, released], 'qms'))
        restored = observed('Released', '2027-01-02 09:00:00', '03')
        self.assertEqual(active_releases([prior, absent, restored], 'qms'), ['Q1'])
        earlier = observed('Recorded', '2027-01-01 12:00:00', '01')
        self.assertEqual(active_releases([released, earlier], 'qms'), ['Q1'])

    def test_measured_retest_requires_original_failure_under_the_same_scope(self):
        def sample(name, status, value, **fields):
            doc = dict(name=name, docstatus=1, batch_no='lot', item_code='sku', custom_spec_revision='A',
                       status=status, readings=[dict(numeric=1, specification='pH', min_value=5, max_value=6,
                                                     status=status, reading_1=value)], **fields)
            return dict(source='lab', entity='Quality Inspection', payload=json.dumps(doc))
        failure = sample('I1', 'Rejected', 7)
        retest = sample('I2', 'Accepted', 5.5, custom_retest_of='I1', workflow_state='Released')
        self.assertEqual(measured_retests([retest, failure]), [('lab', 'I1', 'I2')])
        self.assertFalse(measured_retests([retest, dict(failure, source='other')]))
        self.assertFalse(measured_retests([retest, sample('I1', 'Rejected', 5.5)]))
        self.assertFalse(measured_retests([failure, sample('I2', 'Accepted', 7,
                                           custom_retest_of='I1', workflow_state='Released')]))

    def test_retest_covers_every_original_method_once_with_matching_limits(self):
        def sample(name, status, readings, **fields):
            doc = dict(name=name, docstatus=1, batch_no='lot', item_code='sku', custom_spec_revision='A',
                       status=status, readings=readings, **fields)
            return dict(source='qa', entity='Quality Inspection', payload=json.dumps(doc))
        ph = dict(numeric=1, specification='pH', min_value=5, max_value=6, reading_1=5.5, status='Accepted')
        mass = dict(numeric=1, specification='mass', min_value=95, max_value=105, reading_1=100, status='Accepted')
        failed = sample('I1', 'Rejected', [dict(ph, reading_1=7, status='Rejected'), mass])
        def evaluate(readings):
            return measured_retests([failed, sample('I2', 'Accepted', readings,
                                    custom_retest_of='I1', workflow_state='Released')])
        self.assertTrue(evaluate([mass, ph]))
        for readings in ([ph], [mass, mass, ph], [ph, dict(mass, min_value=90)],
                         [ph, dict(mass, reading_1=110)], [ph, dict(mass, status='Rejected')],
                         [ph, dict(mass, manual_inspection=1)], [ph, dict(mass, formula_based_criteria=1)]):
            with self.subTest(readings=readings):
                self.assertFalse(evaluate(readings))

    def test_duplicate_missing_and_mislinked_native_postings(self):
        erp, mes = 'erp', 'east'
        def snapshot(source, entity, name, **fields):
            return dict(source=source, entity=entity, native_id=name, ingested_at='t',
                        payload=json.dumps(dict(name=name, **fields)))

        fields = dict(company='C', posting_date='2027-01-03')
        pin = dict(completion_id='job', mes_source=mes, version='v1', output_lot_id='east:stock.lot:17')
        moves, raw, issues = [], [], []
        for i, item, qty, mes_unit, unit_id, erp_unit in (
            ('1', 'Bottle', 2, 'Units', '10', 'Nos'),
            ('2', 'Bottle', 2, 'Units', '10', 'Nos'),
            ('3', 'Oil', 0.0675, 'kg', '20', 'Kg'),
        ):
            batch, bundle, detail = f'B{i}', f'SB{i}', f'D{i}'
            moves.append(dict(line_id=i, lot_id=i, lot_name=batch, item_code=item,
                                   product_id=item, unit=mes_unit, unit_id=unit_id, quantity=qty))
            raw.append(dict(name=detail, batch_no=batch, item_code=item, qty=qty, transfer_qty=qty,
                                 conversion_factor=1, uom=erp_unit, stock_uom=erp_unit,
                                 s_warehouse='Raw', serial_and_batch_bundle=bundle))
            links = dict(voucher_type='Stock Entry', voucher_no='A', voucher_detail_no=detail,
                         docstatus=1, is_cancelled=0, item_code=item, warehouse='Raw')
            issues += [
                snapshot(erp, 'Serial and Batch Bundle', bundle, type_of_transaction='Outward',
                         entries=[dict(batch_no=batch, qty=-qty, warehouse='Raw')], **links),
                snapshot(erp, 'Stock Ledger Entry', f'SLE{i}', actual_qty=-qty, stock_uom=erp_unit,
                         serial_and_batch_bundle=bundle, **fields, **links),
            ]
        mo = snapshot(mes, 'mrp.production', '7', completion_ref='job', modified='v1',
                      erp_work_ref='WO', backorder_id='8', inputs=moves,
                      output=dict(item_code='SKU', quantity=4, lot_id='17', unit='Units', unit_id='10'))

        from native.company_erp import digest
        batch = 'NV-' + digest(['ns', ['MES', mes, 'east:stock.lot:17'], 'SKU'])[:32]
        links = dict(voucher_type='Stock Entry', voucher_no='A', voucher_detail_no='FG',
                     docstatus=1, is_cancelled=0, item_code='SKU', warehouse='Hold')
        issues += [snapshot(erp, 'Serial and Batch Bundle', 'FGB', type_of_transaction='Inward',
                            entries=[dict(batch_no=batch, qty=4, warehouse='Hold')], **links),
                   snapshot(erp, 'Stock Ledger Entry', 'FGL', actual_qty=4, stock_uom='Nos',
                            serial_and_batch_bundle='FGB', **fields, **links)]

        def post(name, **changes):
            return snapshot(erp, 'Stock Entry', name, docstatus=1, stock_entry_type='Manufacture',
                            work_order='WO', fg_completed_qty=4, items=[dict(name='FG', is_finished_item=1,
                            item_code='SKU', uom='Nos', stock_uom='Nos', qty=4, batch_no=batch,
                            t_warehouse='Hold', serial_and_batch_bundle='FGB'), *raw],
                            completion={**pin, **changes}, **fields)

        def reconcile(records):
            return reconcile_completions(records, erp, {'WO'}, 'ns', 'Hold')

        def compare(*records):
            return reconcile([mo, *issues, *records])

        good = [mo, post('A'), *issues]
        verified = reconcile(good)
        self.assertEqual(verified['discrepancies'], [])
        self.assertEqual(len(verified['input_issues'][0]['issues']), 3)
        self.assertEqual(verified['output_receipts'][0]['ledger'], 'FGL')
        self.assertEqual(verified, reconcile(json.loads(json.dumps(good[::-1]))))
        for field, wrong in [('qty', 3), ('item_code', 'other'), ('stock_uom', 'kg')]:
            changed = post('A')
            doc = json.loads(changed['payload'])
            doc['items'][0][field] = wrong
            changed['payload'] = json.dumps(doc)
            self.assertTrue(compare(changed)['discrepancies'])
        self.assertTrue(compare(post('A', output_lot_id='west:stock.lot:17'))['discrepancies'])
        self.assertEqual(compare(post('A'), post('B'))['discrepancies'][-1]['matching_postings'], ['A','B'])
        self.assertEqual(compare()['discrepancies'][0]['matching_postings'], [])
        self.assertEqual(compare(post('A', version='other'))['discrepancies'][0]['matching_completions'], 0)
        self.assertEqual(reconcile([dict(mo, source='west'), *issues, post('A')])
                         ['discrepancies'][-1]['source'], 'west')

        def change(row, mutate, **changes):
            doc = json.loads(row['payload'])
            mutate(doc)
            return dict(row, payload=json.dumps(doc), **changes)

        for field, wrong in [('item_code', 'other'), ('lot_name', 'unknown'), ('unit', 'kg'),
                             ('unit_id', '20'), ('unit', 'Dozens'), ('quantity', 1)]:
            with self.subTest(field=field):
                changed = change(mo, lambda d: d['inputs'][0].update({field: wrong}))
                self.assertTrue(reconcile([changed, *good[1:]])['discrepancies'])
                conflicting = dict(changed, ingested_at='z')
                self.assertTrue(reconcile([*good, conflicting])['discrepancies'])

        for field, wrong in [('stock_uom', 'Kg'), ('batch_no', 'wrong'), ('transfer_qty', 1),
                             ('conversion_factor', 2)]:
            with self.subTest(erp_field=field):
                changed = change(good[1], lambda d: d['items'][1].update({field: wrong}))
                self.assertTrue(reconcile([good[0], changed, *good[2:]])['discrepancies'])

        def duplicate_line(doc):
            doc['inputs'][1]['line_id'] = doc['inputs'][0]['line_id']

        self.assertTrue(reconcile([change(mo, duplicate_line), *good[1:]])['discrepancies'])
        for index in (2, 3):
            with self.subTest(missing=good[index]['entity']):
                self.assertTrue(reconcile(good[:index] + good[index+1:])['discrepancies'])
        duplicate = change(good[3], lambda d: d.update(name='duplicate'), native_id='duplicate')
        orphan = change(duplicate, lambda d: d.update(voucher_detail_no='absent'))
        truncated = change(good[3], lambda d: d.update(actual_qty=-1), ingested_at='z')
        cancelled = change(good[3], lambda d: d.update(is_cancelled=1), ingested_at='z')
        for changed in (duplicate, orphan, truncated, cancelled):
            with self.subTest(ledger=changed['payload']):
                self.assertTrue(reconcile([*good, changed])['discrepancies'])
        self.assertEqual(reconcile([*good, dict(good[3], ingested_at='z')]), verified)

        # A replay of the same readback is harmless; distinct or altered receipts fail.
        bundle, receipt = issues[-2:]
        for row in (bundle, receipt):
            self.assertEqual(reconcile([*good, dict(row, ingested_at='z')]), verified)
            self.assertTrue(reconcile([r for r in good if r is not row])['discrepancies'])
        for field, value in [('actual_qty', 3), ('warehouse', 'Finished'), ('item_code', 'forged'),
                             ('stock_uom', 'Kg'), ('company', 'other'), ('posting_date', '2027-01-04'),
                             ('serial_and_batch_bundle', 'forged'), ('is_cancelled', 1)]:
            with self.subTest(receipt_field=field):
                forged = change(receipt, lambda d: d.update({field: value}), ingested_at='z')
                self.assertTrue(reconcile([*good, forged])['discrepancies'])
        duplicate = change(receipt, lambda d: d.update(name='forged'), native_id='forged')
        self.assertTrue(reconcile([*good, duplicate])['discrepancies'])
        for field, value in [('batch_no', 'forged'), ('t_warehouse', 'Finished'),
                             ('stock_qty', 3), ('conversion_factor', 2)]:
            changed = change(good[1], lambda d: d['items'][0].update({field: value}))
            self.assertTrue(reconcile([good[0], changed, *good[2:]])['discrepancies'])
        for field, value in [('unit', 'kg'), ('unit_id', '20'), ('quantity', 3)]:
            changed = change(mo, lambda d: d['output'].update({field: value}))
            self.assertTrue(reconcile([changed, *good[1:]])['discrepancies'])
            self.assertTrue(reconcile([*good, dict(changed, ingested_at='z')])['discrepancies'])
        changed = change(bundle, lambda d: d['entries'][0].update(batch_no='forged'), ingested_at='z')
        self.assertTrue(reconcile([*good, changed])['discrepancies'])
