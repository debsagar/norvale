"""Exercise staffed maintenance across shifts, including a failed functional test."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from integrations.company_run.maintenance import MaintenanceAuthority
from integrations.company_run.maintenance_staff import ACTORS, Staff, staffing, usage_fault
from native.company_erp import Pending, digest


class StaffTests(unittest.TestCase):
    def test_only_usage_trigger_matches(self):
        case = {'center': 1, 'meter': 30., 'reason': 'Usage limit reached at 30.0 units'}
        expected = 'eq-' + digest(['run', 1, 'usage:30.0'])[:32]
        self.assertTrue(usage_fault('run', expected, case))
        self.assertFalse(usage_fault('run', 'external', case))
        self.assertFalse(usage_fault('run', expected, dict(case, reason='Unexplained vibration')))

    def test_shift_limit_and_retry_preserve_slot(self):
        slot = staffing({}, {}, 8, 'diagnosis')
        self.assertEqual(slot['minute'] + slot['duration'], 480)
        assigned = {'staff_turn': slot}
        self.assertEqual(staffing(assigned, {}, 9, 'diagnosis'), slot)
        with self.assertRaisesRegex(Pending, 'already assigned'):
            staffing({}, {'assigned': assigned}, 8, 'service')

    def test_staffed_evidence_does_not_replace_manual_verifier(self):
        authority = MaintenanceAuthority.__new__(MaintenanceAuthority)
        authority.settings = Mock(return_value={
            'restart_verifier': 'manual', 'maintenance_ticket_reader': 'manual-reader',
            'maintenance_staff': {'reader': 'staff-reader'},
        })
        ticket = {'number': 2, 'html_url': '/issues/2', 'created_at': '2026-11-19T10:00:00Z'}
        case = {'fault': {'source_url': 'mes:3'}}
        payload = 'Verified restart: mes:3\nDiagnosis: observed\nRepair: service\nTest: measured\nResult: PASS'
        authority.tickets = Mock()
        authority.tickets.return_value.comments.return_value = [
            dict(id=i, user={'login': actor}, body=payload,
                 created_at='2026-11-19T10:01:00Z', updated_at='2026-11-19T10:01:00Z')
            for i, actor in enumerate(('manual', ACTORS['verifier']), start=1)
        ]
        self.assertEqual(authority.restart_evidence(ticket, case, role='MAINTENANCE')['id'], 1)
        authority.tickets.assert_called_with('MAINTENANCE', token='manual-reader')
        case['staff_history'] = [{'stage': 'verification'}]
        self.assertEqual(authority.restart_evidence(ticket, case, role='MAINTENANCE')['id'], 2)
        authority.tickets.assert_called_with('MAINTENANCE', token='staff-reader')

    def test_failed_test_requires_new_performed_service_before_verification(self):
        case = dict(day=0, center=1, meter=30, fault={'source_url': 'mes:3'})
        cfg = {'maintenance_staff': {}}
        e = SimpleNamespace(day=0, config={'maintenance_days': 1, 'seed': 42},
                            persist=Mock(), log=Mock())
        equipment = SimpleNamespace(e=e, authority=Mock(), cases={'case': case})
        equipment.authority.settings.return_value = cfg
        staff = Staff(equipment, 'case', case, {'number': 2})
        staff.blocked = Mock()
        staff.comment = Mock(side_effect=lambda role, body, **kwargs: {'ref': role + ' ' + body})
        staff.record_service = Mock(side_effect=lambda reader, text, turn, **kwargs: {
            'ref': text.splitlines()[0], 'adjustment': int(text.splitlines()[-1].split()[-1]),
        })
        with patch('integrations.company_run.maintenance_staff.vibration', return_value=410):
            with self.assertRaisesRegex(Pending, 'shift'):
                staff.advance()
            for day, pending in enumerate(('next staffed', 'next staffed', 'test failed',
                                           'next staffed', 'next staffed'), start=1):
                e.day = day
                with self.assertRaisesRegex(Pending, pending):
                    staff.advance()
                self.assertFalse(any(call.args[0] == 'verifier' for call in staff.comment.call_args_list))
            e.day = 6
            verification = staff.advance()
            self.assertEqual(staff.advance(), verification)
        self.assertEqual([row['stage'] for row in case['staff_history']],
                         ['diagnosis', 'service', 'test', 'service', 'test', 'verification'])
        self.assertEqual([row['passed'] for row in case['staff_history'] if row['stage'] == 'test'],
                         [False, True])


if __name__ == '__main__':
    unittest.main()
