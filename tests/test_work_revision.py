import unittest
from native.work_revision import pin_work_revision


class Revisions(unittest.TestCase):
    def setUp(self):
        self.pins = {}
        self.rev = dict(id="plm-14", version="v2", product="lotion", approved=True,
                        effective_at=40, superseded_at=80)
        self.acks = {s: dict(id=s+"-19", source_id="plm-14", version="v2",
                             product="lotion", accepted_at=46) for s in ("erp", "mes")}

    def pin(self, minute=50):
        return pin_work_revision(self.pins, ("erp-east", "WO-8"), "lotion", minute,
                                 self.rev, self.acks)

    def test_pending_never_creates_pin(self):
        for minute in (39, 45, 80):
            with self.subTest(minute=minute), self.assertRaises(ValueError):
                self.pin(minute)
        self.acks["mes"]["version"] = "v1"
        with self.assertRaises(ValueError):
            self.pin()
        self.assertFalse(self.pins)

    def test_pinned_work_cannot_move_to_later_revision(self):
        first = self.pin()
        self.assertEqual(self.pin(84), first)
        self.rev["version"] = "v3"
        with self.assertRaises(ValueError):
            self.pin(85)
        self.assertEqual(next(iter(self.pins.values()))["version"], "v2")
