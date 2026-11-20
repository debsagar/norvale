"""Offline replay, privilege, and recurring company procurement checks."""
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from engine import DAY, Engine, world
from consumers import norvale
from native.company import Engine as Company
from native.company_world import check_binding, evidence
from native.recurring_extensions import MIDNIGHT, open_catalog
from test_engine_goals import CONSUMERS, OPEN, PowerLoss, PRIVATE, Goals


class WorldProof(Goals):
    def test_independent_factor_streams_over_diverse_seeds(self):
        paths = set()
        for seed in range(16):
            suppliers = []
            for initial in ("false_alarm", "long_onset"):
                lane = supplier = None
                trajectory = []
                for at in range(0, 26 * world.WEEK, world.WEEK):
                    lane = world.disruption(lane, world.stream(seed, "disruption", at), initial)
                    supplier = world.reliability(supplier, world.stream(seed, "supplier", at))
                    trajectory.append(supplier["state"])
                suppliers.append(tuple(trajectory))
            self.assertEqual(*suppliers)
            paths.add(suppliers[0])
        self.assertGreater(len(paths), 8)

    def test_replay_after_bulletin_write_with_cost_free_providers(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                def fault(stage, operation):
                    if stage == "after_send" and operation["action"]["kind"] == "world_bulletin":
                        raise PowerLoss()

                config = {"world": True, "private_marker": PRIVATE}
                paths = []
                for hook in (None, fault):
                    run, native, providers, resume = self.fixture(company, config=config, boundary=hook)
                    providers.cost = {"jev": 0, "model": 0}
                    self.decision(run, at=DAY + OPEN)
                    if hook:
                        with self.assertRaises(PowerLoss):
                            run.run(until=DAY)
                        run = resume(hook=None)
                    run.run(until=7 * DAY)
                    snapshot = run.private_truth()
                    snapshot["world"][0].clear()
                    signal = self.one(native, "world_bulletin", at=0)["text"]
                    self.assertEqual(run.observe("sales", as_of=OPEN), [])
                    self.assertTrue(run.private_truth()["world"][0])
                    self.assertEqual(run.metrics()["provider_cost"], {"jev": 0, "model": 0})
                    public = (native.records(), run.observe("manager", as_of=OPEN),
                              providers.calls, run.export())
                    for artifact in public:
                        serialized = json.dumps(artifact)
                        self.assertIn(signal, serialized)
                        for secret in (PRIVATE, "supplier_reliability", "disruption_type", "arrival_due"):
                            self.assertNotIn(secret, serialized)
                    self.assertIn(PRIVATE, run.viewer(include_private=True))
                    self.assertNotIn(PRIVATE, run.viewer())
                    paths.append((run.private_truth(), run.history(), run.export()))
                self.assertEqual(paths[0], paths[1])

    def test_same_warning_can_have_different_physical_consequences(self):
        warnings = set()
        for initial, expected in (("false_alarm", OPEN), ("short_onset", OPEN + 7 * DAY),
                                  ("long_onset", OPEN + 7 * DAY)):
            run, native, providers, _ = self.fixture("norvale", config={
                "world": True, "world_fixture": {"initial_disruption": initial},
            })
            self.schedule(run, "dispatch_supply", at=0, case="S1", eta=OPEN, quantity=30)
            if expected > OPEN:
                self.decision(run, at=OPEN + 1)
            run.run(until=expected)
            warnings.add(self.one(native, "world_bulletin", at=0)["text"])
            self.one(native, "receipt", at=expected, case="S1")
            self.assertEqual(run.private_truth()["deliveries"]["S1"]["actual_arrival_at"], expected)
            self.assertNotIn("actual_arrival_at", json.dumps(run.export()))
            if expected > OPEN:
                notice = self.one(native, "delay_notice", case="S1", at=OPEN, previous_eta=OPEN)
                self.assertNotIn(str(expected), json.dumps(notice))
                for _, prompt, _ in providers.calls:
                    self.assertIn(notice["text"], prompt)
                    self.assertNotIn("actual_arrival_at", prompt)
                    self.assertNotIn(str(expected), prompt)
        self.assertEqual(len(warnings), 1)

    def test_private_view_escapes_evaluator_payload(self):
        run, _, _, resume = self.fixture("norvale", config={"world": True, "private_marker": "<script>"})
        run.run(until=0)
        self.assertIn("&lt;script&gt;", resume().viewer(include_private=True))
        self.assertNotIn("&lt;script&gt;", run.viewer())

    def test_rejects_mid_run_world_switch_and_seed_change(self):
        run, _, _, _ = self.fixture("norvale")
        for config, seed in (({"world": True}, 41), ({}, 42)):
            with self.assertRaisesRegex(ValueError, "original seed and configuration"):
                Engine(run._directory, norvale.Consumer(config), seed=seed, native=None,
                       providers=None, monotonic=None)
        with self.assertRaisesRegex(ValueError, "original fresh-run configuration"):
            check_binding({"world": True, "seed": 41}, {"completed": ["seed"]})
        with self.assertRaisesRegex(ValueError, "original fresh-run configuration"):
            check_binding({"world": True, "seed": 42}, {
                "completed": ["seed"], "world_binding": {"seed": 41, "fixture": {}},
            })


class RecurringWorldProof(TestCase):
    def test_approved_native_promise_queues_role_notices_and_receipt(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        company = Company.__new__(Company)
        company.run = root
        company.day, company.event = 0, "d0000:approvals"
        company.config = {"seed": 41, "world": True, "receipt_delay": 1,
                          "world_fixture": {"transit_delay_minutes": 7 * DAY}}
        company.state = {"ops": {"purchases": {"p": {"day": 0, "due": 2, "items": []}},
                                 "changes": {}}, "physical": {}}
        company.log = Mock()
        company.advance_clock = Mock()
        company.additions = None
        company.wh = {"hold": "Quarantine"}
        submitted = {"name": "PO-1", "docstatus": 1,
                     "items": [{"schedule_date": company.dated(4), "qty": 2, "received_qty": 2},
                               {"schedule_date": company.dated(14), "qty": 1, "received_qty": 0}]}
        company.get = Mock(return_value=submitted)
        mail = {}

        def confirmed(key, sender, recipient, subject, body):
            row = (sender, recipient, subject, body)
            self.assertEqual(mail.setdefault(key, row), row)
            company.state.setdefault("mail_operations", {})[key] = {"message_id": f"<{key}@test>"}

        company.mail = Mock(side_effect=confirmed)
        company.mapped = Mock(return_value={"items": [{"item_code": "X"}]})
        company.write = Mock()
        company.run_additions(720)
        company.approvals()
        company.approvals()
        self.assertNotIn("arrival_due", company.state["ops"]["purchases"]["p"])
        company.day = 14
        def interrupted(key, sender, recipient, subject, body):
            if subject == "Supplier delay notice" and recipient == "WAREHOUSE":
                raise PowerLoss()
            confirmed(key, sender, recipient, subject, body)

        company.mail.side_effect = interrupted
        with self.assertRaises(PowerLoss):
            company.run_additions(14 * DAY + 720)
        company.state = json.loads((root / "state.json").read_text())
        company.additions = open_catalog(root, company.config, company.state,
                                         company.advance_clock, company=company)
        company.mail.side_effect = confirmed
        company.run_additions(14 * DAY + 720)
        company.arrivals()
        self.assertFalse(company.state["physical"])
        company.write.assert_not_called()
        self.assertEqual({entry[1] for entry in mail.values() if entry[2] == "Supplier delay notice"},
                         {"SCM_MGR", "WAREHOUSE"})
        self.assertEqual(sum(entry[2] == "Supplier delay notice" for entry in mail.values()), 2)
        self.assertEqual(evidence(company, "SALES"), [])
        visible = json.dumps(evidence(company, "SCM_MGR"))
        self.assertIn(company.dated(14), visible)
        self.assertIn("PO-1", visible)
        self.assertNotIn(company.dated(21), visible)
        self.assertNotIn("arrival_due", visible)
        self.assertIn("world_bulletin", visible)
        company.day = 13
        self.assertNotIn("PO-1", json.dumps(evidence(company, "SCM_MGR")))
        company.day = 14
        company.advance_clock.assert_any_call(MIDNIGHT + timedelta(minutes=14 * DAY + 720))

        saved = json.loads((root / "state.json").read_text())
        company.state = saved
        company.additions = open_catalog(root, company.config, saved, company.advance_clock, company=company)
        company.day = 21
        company.run_additions(21 * DAY + 720)
        self.assertEqual(company.state["physical"]["p"]["arrived_day"], 21)
        company.arrivals()
        company.write.assert_not_called()
        company.day = 22
        company.run_additions(22 * DAY + 720)
        company.arrivals()
        company.arrivals()
        company.write.assert_called_once_with("receipt:p", "Purchase Receipt",
                                               {"items": [{"item_code": "X", "warehouse": "Quarantine"}]},
                                               "WAREHOUSE", True)
