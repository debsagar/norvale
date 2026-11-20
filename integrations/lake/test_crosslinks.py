import json
import unittest

from boundary import crosslinks, project


class CrossSourceTest(unittest.TestCase):
    def test_quarantine_transfer_requires_exact_native_release_scope(self):
        from integrations.qms.disposition_history import ENTITY
        pin = dict(id='I', version='t1', item='sku', batch='salvage', qms_batch='original',
                   qty=8, uom='Nos', status='released', qms_source='east', approver='qa', authorized=True,
                   comment='private')
        doc = dict(name='STE', docstatus=1, stock_entry_type='Material Transfer', remarks=json.dumps(pin),
                   items=[dict(item_code='sku', batch_no='salvage', stock_uom='Nos', transfer_qty=3,
                               s_warehouse='Hold', t_warehouse='Finished')])
        transfer = project('Stock Entry', doc)
        self.assertNotIn('private', json.dumps(transfer))
        native = dict(name='event', disposition='Released', inspection='I', inspection_version='t1',
                      item_code='sku', batch_no='original', output_batch='salvage', lot_quantity=8,
                      lot_uom='Nos', approver='qa')
        data = {'erp':{'Stock Entry':[transfer]}, 'east':{ENTITY:[native]}, 'west':{ENTITY:[native]}}
        edge = (('erp','Stock Entry','STE'), 'release_transfer', ('east','Quality Inspection','I'))
        self.assertEqual(crosslinks(data)['edges'], [edge])
        for field, wrong in [('version','t2'), ('qty',9), ('qms_source','other'), ('qms_batch','wrong'),
                             ('uom','kg'), ('approver','other'), ('authorized',False)]:
            data['erp']['Stock Entry'] = [project('Stock Entry', dict(doc, remarks=json.dumps(dict(pin, **{field:wrong}))))]
            self.assertFalse(crosslinks(data)['edges'], field)
        pin.pop('qms_source')
        data['erp']['Stock Entry'] = [project('Stock Entry', dict(doc, remarks=json.dumps(pin)))]
        self.assertEqual(crosslinks(data)['unresolved'][0][-1], 2)

    def test_pinned_revision_completion_and_historical_disposition(self):
        work = project('Work Order', {'name':'WO', 'custom_norvale_definition':json.dumps(
            {'id':7, 'version':'sha', 'private':'redacted'})})
        posting = project('Stock Entry', {'name':'STE', 'work_order':'WO', 'remarks':json.dumps(
            {'completion_id':'plant:mrp.production:5','version':'t3','private':'redacted'})})
        delivery = project('Delivery Note', {'name':'DN', 'custom_norvale_quality_releases':json.dumps([{
            'batch':'B', 'qty':2, 'disposition':{'id':'QI','status':'released','batch':'B',
                                                'item':'S','version':'v4','private':'redacted'}}])})
        released = dict(name='QI',workflow_state='Released',docstatus=1,status='Accepted',modified='v4',batch_no='B',item_code='S')
        data = {'erp':{'Work Order':[work],'Stock Entry':[posting],'Delivery Note':[delivery]},
                'plm':{'InvenTree Publication':[dict(name='7',version='sha')]},
                'plant':{'mrp.production':[dict(name='5',completion_ref='plant:mrp.production:5',modified='t3')]},
                'qms':{'Quality Inspection':[released,{**released,'modified':'v5','workflow_state':'Revoked'}]}}
        self.assertNotIn('redacted',json.dumps(data))
        links = crosslinks(data)
        self.assertEqual(len(links['edges']),3)
        self.assertEqual(links['unresolved'],[])
        data['second-qms'] = data['qms']
        links = crosslinks(data)
        self.assertEqual(len(links['edges']),2)
        self.assertEqual(links['unresolved'][0][-1],2)
        data['second-plm'] = data['plm']
        work['definition']['plm_source'] = 'second-plm'
        delivery['quality_releases'][0]['disposition']['qms_source'] = 'qms'
        qualified = crosslinks(data)
        self.assertEqual(qualified['unresolved'], [])
        self.assertIn((('erp','Delivery Note','DN'),'release',('qms','Quality Inspection','QI')),
                      qualified['edges'])
        self.assertIn((('erp','Work Order','WO'),'publication',('second-plm','InvenTree Publication','7')),
                      qualified['edges'])
        del data['qms']
        self.assertEqual(crosslinks(data)['unresolved'][0][-1],0)

    def test_complaint_requires_matching_instance_and_shipped_bundle(self):
        entities = {'Delivery Note':[dict(name='DN', docstatus=1, customer='C',
                            items=[dict(name='ROW', serial_and_batch_bundle='SB')])],
                    'Serial and Batch Bundle':[dict(name='SB', docstatus=1, voucher_type='Delivery Note',
                            voucher_no='DN',voucher_detail_no='ROW',type_of_transaction='Outward',
                            entries=[dict(batch_no='B',qty=-2)])]}
        case = dict(name='NC',custom_delivery_source='east',custom_delivery_note='DN',
                    custom_delivery_customer='C',custom_delivery_row='ROW',custom_qms_batch='B')
        data = {'east':entities,'west':entities,'qms':{'Non Conformance':[case]}}
        self.assertEqual(crosslinks(data)['edges'],[(('qms','Non Conformance','NC'),
                        'shipment',('east','Delivery Note','DN'))])
        case['custom_qms_batch'] = 'different'
        self.assertEqual(crosslinks(data)['unresolved'][0][-1],0)

    def test_return_link_requires_scoped_case_and_exact_inward_bundle(self):
        receipt = dict(complaint='CASE', qms_source='qms', erp_source='east', release='I',
                       delivery='DN', line='ROW', batch='B', observed_on='2026-01-02',
                       quantity=1, private_note='omit')
        note = project('Delivery Note', dict(name='RET', docstatus=1, is_return=1,
                       return_against='DN', customer='C',
                       custom_norvale_quality_releases=json.dumps({'customer_return':receipt}),
                       items=[dict(name='LINE', dn_detail='ROW', serial_and_batch_bundle='SB',
                                   item_code='S', warehouse='Hold')]))
        self.assertNotIn('private_note', note['customer_return'])
        bundle = dict(name='SB', docstatus=1, voucher_type='Delivery Note', voucher_no='RET',
                      voucher_detail_no='LINE', type_of_transaction='Inward',
                      item_code='S', warehouse='Hold', entries=[dict(batch_no='B', qty=1)])
        case = dict(name='CASE', custom_delivery_source='east', custom_delivery_note='DN',
                    custom_delivery_customer='C', custom_delivery_row='ROW', custom_qms_batch='B',
                    custom_withdrawal_release='I', custom_withdrawal_approver='qa')
        data = {'east':{'Delivery Note':[note], 'Serial and Batch Bundle':[bundle]},
                'qms':{'Non Conformance':[case]}, 'other-qms':{'Non Conformance':[case]}}
        edge = (('east','Delivery Note','RET'), 'return_case', ('qms','Non Conformance','CASE'))
        self.assertIn(edge, crosslinks(data)['edges'])
        bundle['entries'][0]['qty'] = 2
        self.assertNotIn(edge, crosslinks(data)['edges'])
        bundle['entries'] = [dict(batch_no='B', qty=0.1), dict(batch_no='B', qty=0.2)]
        note['customer_return']['quantity'] = 0.3
        self.assertIn(edge, crosslinks(data)['edges'])
        del data['qms']
        self.assertNotIn(edge, crosslinks(data)['edges'])

    def test_trace_shipment_to_measured_input_and_release(self):
        from genealogy import shipment
        from types import SimpleNamespace
        def bundle(key, typ, voucher, line, outward):
            return dict(name=key,docstatus=1,is_cancelled=0,voucher_type=typ,voucher_no=voucher,
                        voucher_detail_no=line,item_code='S',type_of_transaction='Outward' if outward else 'Inward',
                        warehouse='WH', entries=[dict(batch_no='B',qty=-2 if outward else 8,warehouse='WH')])
        records={'erp':{'Stock Ledger Entry':[dict(name='SLE',company='C',posting_date='2027-01-03',docstatus=1,is_cancelled=0,voucher_type='Delivery Note',voucher_no='D',voucher_detail_no='L',item_code='S',warehouse='WH',stock_uom='ea',serial_and_batch_bundle='SB',actual_qty=-2)],'Delivery Note':[dict(name='D',company='C',posting_date='2027-01-03',docstatus=1,customer='C',
                    items=[dict(name='L',item_code='S',stock_qty=2,stock_uom='ea',warehouse='WH',serial_and_batch_bundle='SB')],
                    quality_releases=[dict(batch='B',qty=2,disposition=dict(item='S',batch='B',status='released',id='I',version='v'))])],
                'Serial and Batch Bundle':[bundle('SB','Delivery Note','D','L',True),
                                           bundle('PB','Stock Entry','E','O',False)],
                'Stock Entry':[dict(name='E',docstatus=1,stock_entry_type='Manufacture',work_order='W',
                            completion=dict(completion_id='M',version='t',mes_source='plant',output_lot_id='plant:stock.lot:FG'), fg_completed_qty=8,
                            items=[dict(name='O',is_finished_item=1,item_code='S',serial_and_batch_bundle='PB')])],
                'Work Order':[dict(name='W',definition=dict(id='P',version='h'))]},
                'qa':{'Quality Inspection':[dict(name='I',workflow_state='Released',docstatus=1,status='Accepted',batch_no='B',item_code='S',modified='v')]},
                'plant':{'mrp.production':[dict(name='M',completion_ref='plant:M',modified='t',erp_work_ref='W',inputs=[{'lot_id':'RAW'}],output=dict(lot_id='FG',item_code='S',quantity=8))]},
                'plm':{'InvenTree Publication':[dict(name='P',version='h')]}}
        records['erp']['Work Order'][0].update(docstatus=1, creation='2027-01-02', production_item='S', bom_no='EB')
        records['erp']['Work Order'][0]['definition'].update(item='S', erp_bom='EB', effective_from='2027-01-01', approval_id='approval')
        records['plant']['mrp.production'][0]['bom_id'] = 'MB'
        records['plm']['InvenTree Publication'][0].update(product='S', approved_on='2027-01-01', effective_on='2027-01-01',
            approval=dict(on='2027-01-01', evidence='approval', sha256='h'), acknowledgments={
                system: dict(native_id=receiver, on='2027-01-01', plm_native_id='P', plm_source_version='h')
                for system, receiver in [('erp','EB'),('mes','MB')]})
        records['qa']['Quality Inspection'][0].update(custom_lot_quantity=8, custom_lot_uom='ea')
        rows=[dict(source=s,entity=e,native_id=d['name'],ingested_at='t',payload=json.dumps(d))
              for s,tables in records.items() for e,docs in tables.items() for d in docs]
        newer = {**records['plant']['mrp.production'][0], 'modified':'later', 'inputs':[]}
        rows.append(dict(source='plant', entity='mrp.production', native_id='M', ingested_at='z',
                         payload=json.dumps(newer)))
        result=shipment(SimpleNamespace(_rows=lambda:rows),'erp','D')
        self.assertEqual(result['unresolved'],[])
        lot,=result['lots']
        self.assertEqual((lot['quantity'],lot['quality_release'],lot['inputs'],lot['publication']),
                         (2,['qa','Quality Inspection','I','v'],[{'lot_id':'RAW'}],['plm','InvenTree Publication','P']))
        inspection_row, = [r for r in rows if r['entity'] == 'Quality Inspection']
        original = inspection_row['payload']
        for change in (dict(custom_lot_quantity=1), dict(custom_lot_uom='kg'), dict(custom_lot_quantity=None)):
            with self.subTest(scope=change):
                inspection_row['payload'] = json.dumps(dict(json.loads(original), **change))
                invalid = shipment(SimpleNamespace(_rows=lambda: rows), 'erp', 'D')
                self.assertNotIn('quality_release', invalid['lots'][0])
        inspection_row['payload'] = original
        # The collector first sees the inspection after revocation. The retained
        # submitted QMS receipt independently identifies the version used at dispatch.
        note = records['erp']['Delivery Note'][0]
        note['quality_releases'][0]['qms_lease'] = 'lease'
        note['quality_releases'][0]['disposition'].update(qms_source='qa', qty=8)
        inspection = records['qa']['Quality Inspection'][0]
        inspection.update(workflow_state='Revoked', modified='later')
        scope = dict(batch='B',item='S',inspection='I',version='v',revision='spec',uom='ea',
                     qty=8,dispatch_qty=2,approver='QA')
        request = dict(id='lease',delivery='D',scopes=[scope])
        receipt = project('Norvale Dispatch Lease', dict(name='lease', lease_state='submitted',
                                                        request_json=json.dumps(request)))
        records['qa']['Norvale Dispatch Lease'] = [receipt]
        def inspect():
            archive = [dict(source=s,entity=e,native_id=d['name'],ingested_at='t',payload=json.dumps(d))
                       for s,tables in records.items() for e,docs in tables.items() for d in docs]
            return shipment(SimpleNamespace(_rows=lambda:archive), 'erp','D')
        publication = records['plm']['InvenTree Publication'][0]
        for receipt_name, field, bad in [('mes', 'native_id', 'wrong'), ('erp', 'on', '2027-01-03')]:
            ack = publication['acknowledgments'][receipt_name]
            saved = ack[field]
            ack[field] = bad
            self.assertIn(['B','PLM revision'], inspect()['unresolved'])
            ack[field] = saved
        report = records['plant']['mrp.production'][0]
        erp_marker = '11111111111141118111111111111111'
        mes_marker = 'a' * 64
        markers = {'erp': erp_marker, 'mes': mes_marker}
        work = records['erp']['Work Order'][0]
        work['definition']['receiver_markers'] = markers
        for system, marker in markers.items():
            publication['acknowledgments'][system]['receiver_marker'] = marker
        records['erp']['BOM'] = [project('BOM', dict(name='EB', custom_norvale_receiver_marker=erp_marker))]
        report['receiver_marker'] = mes_marker
        self.assertEqual(inspect()['lots'][0]['publication_receiver_markers'], markers)
        work['company'] = records['erp']['Stock Entry'][0]['company'] = 'Company'
        records['erp']['BOM'][0].update(owner='planner', modified='v2', company='Company', item='S', docstatus=1)
        self.assertEqual(inspect()['lots'][0]['erp_bom'], {'record': ['erp', 'BOM', 'EB', 'v2'], 'owner': 'planner'})
        records['erp']['BOM'][0]['company'] = 'other'
        self.assertNotIn('erp_bom', inspect()['lots'][0])
        # A different receiver can have the same BOM ID and identical contents.
        for row, wrong in ((records['erp']['BOM'][0], '22222222222242228222222222222222'),
                           (report, 'b' * 64)):
            marker = row.pop('receiver_marker')
            self.assertIn(['B', 'PLM revision'], inspect()['unresolved'])
            row['receiver_marker'] = wrong
            self.assertIn(['B', 'PLM revision'], inspect()['unresolved'])
            row['receiver_marker'] = marker
        for field, value in [('output', dict(report['output'], quantity=7)),
                             ('output', dict(report['output'], lot_id='another')),
                             ('erp_work_ref', 'other work order')]:
            original = report[field]
            report[field] = value
            self.assertIn(['B', 'MES output lot, work and production quantity'], inspect()['unresolved'])
            report[field] = original
        records['plant']['mrp.production'].append(dict(report, inputs=[]))
        self.assertNotIn('mes', inspect()['lots'][0])
        records['plant']['mrp.production'].pop()
        checked = inspect()
        self.assertEqual(checked['unresolved'], [])
        self.assertEqual(checked['lots'][0]['dispatch_lease'], ['qa','Norvale Dispatch Lease','lease'])
        note['items'][0]['stock_qty'] = 3
        self.assertIn(['L', 'delivery line stock quantity'], inspect()['unresolved'])
        note['items'][0]['stock_qty'] = 2
        receipt['scopes'][0]['qty'] = 7
        self.assertIn(['B','submitted QMS dispatch lease'], inspect()['unresolved'])
        receipt['scopes'][0]['qty'] = 8
        inspection['item_code'] = 'another item'
        self.assertNotIn('quality_release', inspect()['lots'][0])
        inspection['item_code'] = 'S'
        receipt['scopes'][0]['dispatch_qty'] = 1
        self.assertIn(['B','submitted QMS dispatch lease'], inspect()['unresolved'])
        self.assertNotIn('quality_release', inspect()['lots'][0])
        # Even an archived historical release cannot stand in for an invalid cited lease.
        inspection.update(workflow_state='Released', modified='v')
        bad = inspect()
        self.assertNotIn('quality_release', bad['lots'][0])
        inspection.update(workflow_state='Revoked', modified='later')
        # A packing posting pins the upstream bulk movement in both applications.
        receipt['scopes'][0]['dispatch_qty'] = 2
        pack = records['erp']['Stock Entry'][0]
        pack['intermediate_inputs'] = [dict(batch='BULK-B',mes_lot_id='plant:stock.lot:91',
                mes_move={'native_id':51},erp_manufacture='BULK-E',qms_release='BULK-Q',qms_version='q1',qms_source='qa')]
        pack['items'].append(dict(name='CONSUME',item_code='BULK',s_warehouse='RM',serial_and_batch_bundle='TAKE'))
        bulk_post = dict(name='BULK-E',docstatus=1,stock_entry_type='Manufacture',
                         completion=dict(completion_id='plant:MO:4',version='x'),
                         items=[dict(name='MADE',item_code='BULK',is_finished_item=1,serial_and_batch_bundle='MAKE')])
        records['erp']['Stock Entry'].append(bulk_post)
        for ref,entry,line,outward in [('TAKE','E','CONSUME',True),('MAKE','BULK-E','MADE',False)]:
            move = bundle(ref,'Stock Entry',entry,line,outward)
            move.update(item_code='BULK',entries=[dict(qty=-0.75 if outward else 0.75,batch_no='BULK-B')])
            records['erp']['Serial and Batch Bundle'].append(move)
        pack_input = dict(lot_id='91',line_id='51',item_code='BULK',unit='kg',unit_id='1',quantity=.75)
        records['plant']['mrp.production'][0]['inputs']=[pack_input]
        records['plant']['mrp.production'].append(dict(name='4',modified='x',completion_ref='plant:MO:4',
                                            inputs=[{'lot_id':'OIL'}],output=pack_input))
        records['qa']['Quality Inspection'].append(dict(name='BULK-Q',modified='q1',batch_no='BULK-B',
                                                  item_code='BULK',workflow_state='Released'))
        verified = inspect()
        self.assertEqual(verified['unresolved'], [])
        self.assertEqual(verified['lots'][0]['bulk_inputs'][0]['inputs'], [{'lot_id':'OIL'}])
        records['plant']['mrp.production'][0]['inputs'] = [{**pack_input,'lot_id':'WRONG'}]
        self.assertIn(['BULK-B','bulk manufacture, release and packing consumption'],inspect()['unresolved'])

    def test_trace_rejects_release_allocation_quantity_mismatch(self):
        from genealogy import shipment
        from types import SimpleNamespace
        note = dict(name='D', docstatus=1, items=[dict(name='L', item_code='S', serial_and_batch_bundle='SB')],
                    quality_releases=[dict(batch='B', qty=1, disposition=dict(item='S', batch='B', status='released', id='I', version='v'))])
        bundle = dict(name='SB', docstatus=1, voucher_type='Delivery Note', voucher_no='D', voucher_detail_no='L',
                      item_code='S', type_of_transaction='Outward', entries=[dict(batch_no='B', qty=-2)])
        inspection = dict(name='I', workflow_state='Released',docstatus=1,status='Accepted', batch_no='B', item_code='S', modified='v')
        rows = [dict(source=s, entity=e, native_id=d['name'], ingested_at='t', payload=json.dumps(d))
                for s, e, d in [('erp', 'Delivery Note', note), ('erp', 'Serial and Batch Bundle', bundle),
                                ('qa', 'Quality Inspection', inspection)]]
        result = shipment(SimpleNamespace(_rows=lambda: rows), 'erp', 'D')
        self.assertIn(['B', 'shipment release version'], result['unresolved'])
