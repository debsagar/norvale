import hashlib
import json
import unittest
from types import SimpleNamespace
from boundary import project
from genealogy import shipment, ShipmentHistory
from rework_archive import reconcile


class ReworkReaderTest(unittest.TestCase):
    def test_missing_rework_evidence_stays_unresolved(self):
        post = project('Stock Entry', {'name':'R', 'docstatus':1, 'stock_entry_type':'Repack',
            'remarks':json.dumps({'rework':'mes:mrp.production:2', 'qms_authority':{'inspection':'I', 'version':'v'},
                                  'scrap_id':3, 'scrapped':2, 'uom':'Nos', 'private':'secret'}),
            'items':[{'name':'out','item_code':'X','stock_uom':'Nos','t_warehouse':'Hold',
                      'serial_and_batch_bundle':'B'}]})
        self.assertNotIn('secret', json.dumps(post))
        records = [('erp','Stock Entry',post), ('erp','Delivery Note',{'name':'D','docstatus':1,
            'items':[{'name':'line','item_code':'X','stock_uom':'Nos','serial_and_batch_bundle':'DB'}]}),
            ('erp','Serial and Batch Bundle',{'name':'B','docstatus':1,'voucher_type':'Stock Entry',
                'voucher_no':'R','voucher_detail_no':'out','item_code':'X','type_of_transaction':'Inward',
                'entries':[{'batch_no':'NEW','qty':8}]}),
            ('erp','Serial and Batch Bundle',{'name':'DB','docstatus':1,'voucher_type':'Delivery Note',
                'voucher_no':'D','voucher_detail_no':'line','item_code':'X','type_of_transaction':'Outward',
                'entries':[{'batch_no':'NEW','qty':-1}]})]
        rows = [dict(source=s,entity=e,native_id=d['name'],ingested_at='t',payload=json.dumps(d)) for s,e,d in records]
        result = shipment(SimpleNamespace(_rows=lambda: rows), 'erp', 'D')
        self.assertIn(['NEW','rework batch movements'], result['unresolved'])
        self.assertNotIn('rework', result['lots'][0])

    def test_shipped_salvage_uses_pinned_manufacture_even_after_native_update(self):
        authority = dict(batch='FAILED', inspection='I', version='bad', case='NC', quantity=7, approver='qa')
        request = dict(batch='FAILED', qms_batch='FAILED', inspection='I', mes_order=1)
        marker = 'QMS:I:' + hashlib.sha256(json.dumps([request,authority],sort_keys=True).encode()).hexdigest()
        old = dict(product_id='5', unit_id='2', lot_id='100', quantity=10)
        original = dict(name='1', modified='m1', completion_ref='plant:mrp.production:1', company_id='4',
                        revision='rev', hold_location_id='8', inputs=[dict(lot_id='RAW')], output=old)
        report = dict(original, name='2', modified='m2', completion_ref='plant:mrp.production:2',
                      inputs=[dict(old, quantity=7)], output=dict(old, lot_id='101',quantity=7),
                      rework_native=dict(source_order=1, input_location=8, authority=marker,
                      scrap=dict(id=3,state='done',production_id=2,destination_id=12,lines=[
                          dict(state='done',lot_id=100,product_id=5,unit_id=2,location_id=8,
                               destination_id=12,quantity=3)])))
        failure = dict(name='I', modified='bad', docstatus=1, workflow_state='Rework Authorized',
                       status='Rejected', modified_by='qa', batch_no='FAILED', item_code='X',
                       readings=[dict(numeric=1,specification='pH',min_value=5,max_value=6,reading_1='7.2',status='Rejected')],
                       custom_lot_uom='ea', custom_lot_quantity=10, custom_spec_revision='spec',
                       custom_rework_input_batch='FAILED', custom_rework_salvage_quantity=7)
        retest = dict(failure, name='R', modified='2026-11-19 10:00:00.100000', status='Accepted', workflow_state='Released',
                      custom_retest_of='I', custom_rework_output_batch='NEW', custom_lot_quantity=7,
                      custom_rework_execution=report['completion_ref'],
                      readings=[dict(failure['readings'][0],reading_1='5.5',status='Accepted')])
        tables = {'plant':{'mrp.production':[original, report, dict(original,modified='m3',inputs=[])]},
                  'qa':{'Quality Inspection':[failure,retest],
                        'Non Conformance':[dict(name='NC',custom_failed_inspection='I',custom_qms_batch='FAILED')]},
                  'plm':{'InvenTree Publication':[dict(name='PUB',version='rev')]}}
        erp = tables['erp'] = {'Serial and Batch Bundle':[], 'Work Order':[
            dict(name='WO',definition=dict(id='PUB',version='rev',plm_source='plm'))]}
        def row(parent, name, batch, qty, **fields):
            key = 'bundle-' + name
            erp['Serial and Batch Bundle'].append(dict(name=key,docstatus=1,voucher_no=parent,
                voucher_type='Delivery Note' if parent=='DN' else 'Stock Entry',voucher_detail_no=name,
                item_code='X',type_of_transaction='Inward' if qty>0 else 'Outward',
                entries=[dict(batch_no=batch,qty=qty)]))
            return dict(name=name,item_code='X',batch_no=batch,stock_uom='ea',stock_qty=abs(qty),serial_and_batch_bundle=key,**fields)
        pin = dict(rework=report['completion_ref'],rework_version='m2',qms_authority=authority,
                   scrapped=3,scrap_id=3,uom='ea')
        erp['Stock Entry'] = [
            dict(name='M', company='C',docstatus=1,stock_entry_type='Manufacture',work_order='WO',
                 completion=dict(completion_id=original['completion_ref'],version='m1',mes_source='plant'),
                 items=[row('M','output','FAILED',10,is_finished_item=1,t_warehouse='H')]),
            dict(name='RE', company='C',posting_date='2027-01-03',docstatus=1,stock_entry_type='Repack',rework=pin,items=[
                row('RE','failed','FAILED',-10,s_warehouse='H'),row('RE','salvage','NEW',7,t_warehouse='H')])]
        erp['Delivery Note'] = [dict(name='DN',company='C',posting_date='2027-01-03',docstatus=1,items=[row('DN','shipped','NEW',-2)],
            quality_releases=[dict(batch='NEW',qty=2,disposition=dict(batch='NEW',item='X',id='R',
                                   version='2026-11-19 10:00:00.100000',status='released',qms_source='qa'))])]
        erp['Delivery Note'][0]['items'][0]['warehouse'] = 'WH'
        shipment_bundle = erp['Serial and Batch Bundle'][-1]
        shipment_bundle['is_cancelled'] = 0
        shipment_bundle['warehouse'] = 'WH'
        shipment_bundle['entries'][0]['warehouse'] = 'WH'
        erp['Stock Ledger Entry'] = [dict(name='SLE',company='C',posting_date='2027-01-03',docstatus=1,is_cancelled=0,voucher_type='Delivery Note',
            voucher_no='DN',voucher_detail_no='shipped',item_code='X',warehouse='WH',stock_uom='ea',
            serial_and_batch_bundle='bundle-shipped',actual_qty=-2)]
        for name, batch, qty in [('failed','FAILED',-10), ('salvage','NEW',7)]:
            bundle = next(b for b in erp['Serial and Batch Bundle'] if b['name'] == 'bundle-' + name)
            bundle.update(is_cancelled=0, warehouse='H')
            bundle['entries'][0]['warehouse'] = 'H'
            erp['Stock Ledger Entry'].append(dict(name='L-'+name,company='C',posting_date='2027-01-03',
                docstatus=1,is_cancelled=0,voucher_type='Stock Entry',voucher_no='RE',voucher_detail_no=name,
                item_code='X',warehouse='H',stock_uom='ea',serial_and_batch_bundle=bundle['name'],actual_qty=qty))
        erp['Work Order'][0].update(docstatus=1, creation='2027-01-02', production_item='X', bom_no='EB')
        erp['Work Order'][0]['definition'].update(item='X', erp_bom='EB', effective_from='2027-01-01', approval_id='approval')
        original.update(bom_id='MB', erp_work_ref='WO')
        original['output']['item_code'] = 'X'
        tables['plm']['InvenTree Publication'][0].update(product='X', approved_on='2027-01-01', effective_on='2027-01-01',
            approval=dict(on='2027-01-01', evidence='approval', sha256='rev'), acknowledgments={
                system: dict(native_id=receiver, on='2027-01-01', plm_native_id='PUB', plm_source_version='rev')
                for system, receiver in [('erp','EB'),('mes','MB')]})
        def trace():
            rows = [dict(source=s,entity=e,native_id=d['name'],ingested_at='t',payload=json.dumps(d))
                    for s,entities in tables.items() for e,docs in entities.items() for d in docs]
            return shipment(SimpleNamespace(_rows=lambda:rows),'erp','DN')
        def archive():
            rows = [dict(source=src,entity=entity,native_id=doc['name'],ingested_at='t',payload=json.dumps(doc))
                    for src,entities in tables.items() for entity,docs in entities.items() for doc in docs]
            return reconcile(rows, 'erp')[0]
        self.assertEqual(archive()['status'], 'released')
        for table, observed in [(tables['qa']['Quality Inspection'], failure),
                                (tables['qa']['Quality Inspection'], retest),
                                (tables['plant']['mrp.production'], report),
                                (tables['plant']['mrp.production'], original)]:
            table.append(dict(observed))
            self.assertEqual(archive()['status'], 'released')
            table[-1] = dict(observed, contradictory_observation=True)
            self.assertEqual(archive()['status'], 'broken')
            table.pop()
        for entity, name, changed in [
            ('Stock Ledger Entry', 'L-salvage', {'actual_qty': 6}),
            ('Serial and Batch Bundle', 'bundle-failed', {'entries': [{'batch_no':'FAILED', 'qty':-9}]}),
            ('Stock Entry', 'M', {'company':'other'}),
            ('Stock Entry', 'RE', {'docstatus': 2}),
        ]:
            docs = erp[entity]
            valid = next(doc for doc in docs if doc['name'] == name)
            valid['modified'] = 'erp-v1'
            conflict = dict(valid, **changed)
            for index in (0, len(docs)):
                docs.insert(index, conflict)
                with self.subTest(entity=entity, order=index):
                    self.assertEqual(archive()['status'], 'broken')
                    self.assertNotIn('rework', trace()['lots'][0])
                docs.pop(index)
            # A less detailed projection with the same values remains compatible.
            docs.insert(0, dict(name=name, modified='erp-v1', docstatus=1))
            self.assertEqual(archive()['status'], 'released')
            docs.pop(0)
        # Transfer receipt binds the original QMS lot to the exact salvage output.
        retest['report_date'] = '2027-01-03'
        transfer_pin = dict(id='R', version='2026-11-19 10:00:00.100000', item='X', batch='NEW', qms_batch='FAILED',
                            qty=7, uom='ea', authorized=True, approver='qa', qms_source='qa',
                            retest_of='I', rework_execution=report['completion_ref'],
                            report_date='2027-01-03', spec_revision='spec')
        transfer_row = dict(name='transfer-row', item_code='X', batch_no='NEW', stock_uom='ea',
                            qty=7, transfer_qty=7, conversion_factor=1, s_warehouse='H', t_warehouse='FG')
        erp['Stock Entry'].append(dict(name='T', company='C', posting_date='2027-01-03', docstatus=1,
             stock_entry_type='Material Transfer', release_disposition=transfer_pin,
             qms_transfer_lease='lease', items=[transfer_row]))
        binding = dict(erp_source='erp', stock_entry='T', row='transfer-row', company='C',
                       posting_date='2027-01-03', source='H', target='FG')
        legs = {}
        for wh, direction, qty in [('H','Outward',-7), ('FG','Inward',7)]:
            identifier = 'transfer-' + wh
            erp['Serial and Batch Bundle'].append(dict(name=identifier, docstatus=1, is_cancelled=0,
                voucher_type='Stock Entry', voucher_no='T', voucher_detail_no='transfer-row', item_code='X',
                warehouse=wh, type_of_transaction=direction, entries=[dict(batch_no='NEW', warehouse=wh, qty=qty)]))
            erp['Stock Ledger Entry'].append(dict(name='ledger-'+wh, docstatus=1, is_cancelled=0,
                voucher_type='Stock Entry', voucher_no='T', voucher_detail_no='transfer-row', item_code='X',
                warehouse=wh, company='C', posting_date='2027-01-03', stock_uom='ea', actual_qty=qty,
                serial_and_batch_bundle=identifier))
            legs[direction] = dict(ledger='ledger-'+wh, bundle=identifier)
        scope = dict(batch='FAILED', output_batch='NEW', retest_of='I', rework_execution=report['completion_ref'],
                     report_date='2027-01-03', inspection='R', version='2026-11-19 10:00:00.100000', revision='spec', item='X',
                     qty=7, dispatch_qty=7, uom='ea', approver='qa')
        tables['qa']['Norvale Dispatch Lease'] = [dict(name='lease', kind='release_transfer', lease_state='submitted',
            transfer=binding, scopes=[scope], commit=dict(transfer=binding, quantity=7, legs=legs))]
        def transfer_trace():
            rows = [dict(source=src, entity=entity, native_id=doc['name'], ingested_at='t', payload=json.dumps(doc))
                    for src, entities in tables.items() for entity, docs in entities.items() for doc in docs]
            return ShipmentHistory(rows, 'erp').transfer_lineage('T')
        self.assertEqual(transfer_trace()['authorization_at_transfer'], 'confirmed')
        for field, wrong in [('output_batch','WRONG'), ('retest_of','WRONG'),
                             ('rework_execution','wrong:mrp.production:2'), ('report_date','2027-01-02'),
                             ('version','changed'), ('revision','wrong')]:
            value = scope[field]
            scope[field] = wrong
            self.assertEqual(transfer_trace()['authorization_at_transfer'], 'unresolved')
            scope[field] = value
        erp['Stock Entry'].pop()
        del erp['Stock Ledger Entry'][-2:]
        del erp['Serial and Batch Bundle'][-2:]
        del tables['qa']['Norvale Dispatch Lease']
        source_row = erp['Stock Entry'][1]['items'][0]
        source_row['qty'] = source_row.pop('stock_qty')
        source_row['uom'] = source_row['stock_uom']
        self.assertEqual(archive()['status'], 'released')
        source_row['uom'] = 'carton'
        self.assertEqual(archive()['status'], 'broken')
        source_row['uom'] = source_row['stock_uom']
        source_row['conversion_factor'] = 2
        self.assertEqual(archive()['status'], 'broken')
        source_row['conversion_factor'] = 1
        tables['qa2'] = {'Quality Inspection':[failure], 'Non Conformance':tables['qa']['Non Conformance']}
        self.assertEqual(archive()['status'], 'broken')
        pin['qms_source'] = 'qa'
        self.assertEqual(archive()['status'], 'released')
        missing = erp['Stock Ledger Entry'].pop()
        self.assertEqual(archive()['status'], 'broken')
        erp['Stock Ledger Entry'].append(missing)
        revoked = dict(retest, modified='2026-11-19 10:00:00.200000', workflow_state='Revoked')
        tables['qa']['Quality Inspection'].append(revoked)
        self.assertEqual(archive()['status'], 'revoked')
        # A lagging scan can re-observe the old release after the native revocation.
        inspections = tables['qa']['Quality Inspection']
        inspections.remove(retest)
        inspections.append(retest)
        self.assertEqual(archive()['status'], 'broken')
        inspections.remove(revoked)
        inspections.append(revoked)
        for changed in [dict(name='R', source_absent=True),
                        dict(revoked, modified='2026-11-19 10:00:00.090000'),
                        dict(revoked, modified='unknown-version'),
                        dict(revoked, workflow_state='Recorded'),
                        dict(revoked, custom_rework_output_batch='OTHER'),
                        dict(revoked, custom_lot_quantity=6)]:
            with self.subTest(changed=changed):
                tables['qa']['Quality Inspection'][-1] = changed
                evidence = archive()
                self.assertEqual(evidence['status'], 'broken')
                self.assertTrue(evidence['unresolved'])
        tables['qa']['Quality Inspection'][-1] = revoked
        tables['qa']['Quality Inspection'].remove(revoked)
        tables['qa']['Quality Inspection'].remove(retest)
        self.assertEqual(archive()['status'], 'pending')
        tables['qa']['Quality Inspection'].append(retest)
        retest['readings'][0]['reading_1'] = '7.2'
        self.assertEqual(archive()['status'], 'broken')
        retest['readings'][0]['reading_1'] = '5.5'
        result = trace()
        self.assertEqual(result['unresolved'], [])
        lot, = result['lots']
        self.assertEqual(lot['publication'], ['plm','InvenTree Publication','PUB'])
        self.assertEqual(lot['rework']['original_inputs'], [{'lot_id':'RAW'}])
        self.assertEqual(lot['rework']['scrapped'],3)
        retest['status'] = 'Rejected'
        self.assertIn(['NEW','released rework retest version'], trace()['unresolved'])
        retest['status'] = 'Accepted'
        retest['readings'][0]['reading_1'] = '7.2'
        self.assertIn(['NEW','measured failure and passing retest'], trace()['unresolved'])
        retest['readings'][0]['reading_1'] = '5.5'
        tables['plant']['mrp.production'].remove(original)
        self.assertIn(['NEW','original ERP posting and MES version'], trace()['unresolved'])

if __name__ == '__main__':
    unittest.main()
