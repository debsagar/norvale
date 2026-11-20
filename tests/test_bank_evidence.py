import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from native.build_evidence import build


class BankEvidenceTests(unittest.TestCase):
    def test_reference_requires_dated_allocation_in_bank_currency_and_direction(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'config.json').write_text('{"days": 3}')
            (run / 'state.json').write_text('{"day": 2, "completed": ["settlement"]}')
            for kind, bank_side, amount, currency in [('Receive', 'deposit', 9, 'USD'), ('Pay', 'withdrawal', 6, 'EUR')]:
                pay = dict(doctype='Payment Entry', name='P', docstatus=1, clearance_date='2027-01-02',
                           payment_type=kind, paid_from_account_currency='EUR', paid_to_account_currency='USD',
                           received_amount=9, paid_amount=6)
                bank = dict(doctype='Bank Transaction', name='B', docstatus=1, status='Reconciled',
                            date=pay['clearance_date'], currency=currency, unallocated_amount=0,
                            deposit=0, withdrawal=0, payment_entries=[dict(payment_document='Payment Entry',
                            payment_entry='P', allocated_amount=amount)])
                bank[bank_side] = amount
                for change in [{}, {'currency': 'GBP'}, {bank_side: 0}, {'date': '2027-01-03'}]:
                    with self.subTest(kind=kind, change=change):
                        docs = (pay, {**bank, **change})
                        (run / 'trace.jsonl').write_text('\n'.join(json.dumps(dict(day=i, doc=d,
                            kind='bank_clearance_readback')) for i, d in enumerate(docs)))
                        for cutoff in (0, 1):
                            question, = [q for q in build(run, cutoff)['questions']
                                         if q['domain'] == 'financial-assurance']
                            if cutoff and not change:
                                self.assertEqual(question['reference_answer'], dict(bank_transaction='B',
                                    cleared_on='2027-01-02', allocated_amount=amount, currency=currency))
                            else:
                                self.assertEqual(question['answerability'], 'insufficient')
