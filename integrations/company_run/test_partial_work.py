import copy
import unittest
from types import SimpleNamespace
from integrations.company_run.partial_work import partial_work


class PartialWorkTests(unittest.TestCase):
    def setUp(self):
        self.order = dict(name='WO', docstatus=1, status='In Process', qty=10, produced_qty=4,
                          production_item='SKU', stock_uom='Nos', bom_no='EB-1')
        self.work = dict(qty=10, produced=4, request=dict(work_order='WO', item='SKU', uom='Nos', qty=10),
                         dispatch=dict(id='1', version='rev', source='east'))
        base = dict(origin='WO', procurement_group_id=[9, 'group'], product_id=[3, 'SKU'],
                    product_uom_id=[4, 'Units'], bom_id=[5, 'BOM'], company_id=[6, 'Co'],
                    norvale_revision='rev')
        self.rows = {1: dict(base, id=1, state='done', product_qty=4, backorder_sequence=1),
                     2: dict(base, id=2, state='confirmed', product_qty=6, backorder_sequence=2)}
        self.mes = SimpleNamespace(source_id='east', read=self.read, call=self.call, completion=self.completion)
        authority = SimpleNamespace(route=lambda *a, **k: ({'units': {'Units': 'Nos'}}, {}),
                                    execution_reader=lambda *a: self.mes)
        self.engine = SimpleNamespace(api={'PLANNER': SimpleNamespace(get=lambda *a: self.order)},
                                      company_erp=SimpleNamespace(authority=authority))

    def read(self, model, identifier):
        return {'default_code': 'SKU'} if model == 'product.product' else self.rows[identifier]

    def call(self, *args):
        return list(self.rows)

    def completion(self, identifier):
        row = self.rows[identifier]
        return dict(work={'native_id': identifier, 'source_id': 'east'}, erp_work_ref='WO', revision='rev', bom={'native_id': 5},
                    output={'item_code': 'SKU', 'uom_name': 'Units', 'quantity': row['product_qty']})

    def check(self, **options):
        return partial_work(self.engine, [self.work], {'WO': {'mes_source': 'east', 'mes': 5, 'erp': 'EB-1'}}, **options)

    def test_native_partial_and_later_completion(self):
        self.assertEqual(self.check()['WO']['remaining'], 6)
        self.rows[2].update(state='done', product_qty=2)
        self.rows[3] = dict(self.rows[2], id=3, state='confirmed', product_qty=4, backorder_sequence=3)
        self.order['produced_qty'] = 6
        self.assertEqual(self.check()['WO']['completed'], [1, 2])
        self.rows[3]['state'] = 'done'
        self.order['produced_qty'] = 10
        self.assertFalse(self.check())
        finished = self.check(include_finished=True)['WO']
        self.assertEqual((finished['completed'], finished['posted'], finished['remaining'], finished['backorder']),
                         ([1, 2, 3], 10, 0, None))
        self.rows[3]['origin'] = 'OTHER-WO'
        self.assertFalse(self.check(include_finished=True))

    def test_rejects_unsupported_or_fabricated_evidence(self):
        original = copy.deepcopy((self.order, self.work, self.rows))
        for target, key, value in [(self.order, 'docstatus', 2), (self.order, 'bom_no', 'OTHER-BOM'), (self.order, 'produced_qty', 3),
                (self.order, 'stock_uom', 'kg'), (self.order, 'production_item', 'OTHER'),
                (self.work['dispatch'], 'source', 'west'), (self.work['dispatch'], 'version', 'fake'),
                (self.rows[2], 'state', 'cancel'), (self.rows[2], 'product_qty', 5),
                (self.rows[2], 'origin', 'OTHER'), (self.rows[2], 'norvale_revision', 'fake'),
                (self.rows[2], 'backorder_sequence', 3)]:
            old = target[key]
            target[key] = value
            self.assertFalse(self.check(), (key, value))
            target[key] = old
        self.assertEqual((self.order, self.work, self.rows), original)
        completion = self.mes.completion
        self.mes.completion = lambda identifier: dict(completion(identifier), output={'item_code': 'FAKE', 'uom_name': 'Units', 'quantity': 4})
        self.assertFalse(self.check())
        self.mes.completion = completion
        self.mes.completion = lambda identifier: dict(completion(identifier), bom={'native_id': 9})
        for row in self.rows.values():
            row['bom_id'] = [9, 'Different BOM']
        self.assertFalse(self.check())
        for row in self.rows.values():
            row['bom_id'] = [5, 'BOM']
        self.mes.completion = completion
        self.rows.pop(2)
        self.assertFalse(self.check())


if __name__ == '__main__':
    unittest.main()
