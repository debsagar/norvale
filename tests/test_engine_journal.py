"""Cross-consumer replay and offline inspection of genuine call evidence."""
import sqlite3
from unittest.mock import patch

from engine.__main__ import main
from test_engine_goals import CONSUMERS, Goals, OPEN, Page, PowerLoss


class Journal(Goals):
    def test_resume_before_decision_and_after_mail_send(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                def boundary(stage, operation):
                    if stage == "after_send" and operation["system"] == "mail":
                        raise PowerLoss

                run, native, providers, reopen = self.fixture(company, boundary=boundary)
                visible = '<script>alert("evidence")</script>'
                native.send("erp", "visible", {"kind": "evidence", "at": OPEN,
                                              "roles": ["manager"], "text": visible})
                native.send("erp", "restricted", {"kind": "evidence", "at": OPEN,
                                                 "roles": ["auditor"], "text": "secret-auditor"})
                providers.first_timeout.add("model")
                self.decision(run, hidden_state="private-event-input")
                self.review(run)
                with self.assertRaises(PowerLoss):
                    run.run(until=OPEN)

                # Resume the review after its ambiguous commit, then interrupt
                # the approval after provider responses but before its decision.
                run = reopen(None)
                with patch.object(run, "save_consumer_decision", side_effect=PowerLoss):
                    with self.assertRaises(PowerLoss):
                        run.run(until=OPEN + 15)
                self.assertEqual([tier for tier, _, _ in providers.calls], ["jev", "model", "model"])
                providers.replies["model"] = '{"action":"reject"}'
                native.send("erp", "late", {"kind": "evidence", "at": OPEN,
                                           "roles": ["manager"], "text": "late-backdated-evidence"})
                run = reopen(None)
                run.providers = None
                run.run(until=OPEN + 15)
                self.assertEqual(len(providers.calls), 3)
                approval = self.one(native, "approval", case="D1")
                mail = self.one(native, "review_notice", case="R1")
                trace = run.trace()
                run.run(until=OPEN + 15)
                self.assertEqual(run.trace(), trace)
                self.assertEqual([row["method"] for row in trace if row["tier"] == "api"
                                  and mail["native_id"] in row["native_ids"]],
                                 ["send", "find", "read"])
                model = [row for row in trace if row["tier"] == "model"]
                self.assertEqual([row["status"] for row in model], ["failed", "returned"])
                self.assertIn(approval["native_id"], model[1]["native_ids"])
                self.assertEqual(run.now, OPEN + 15)

                directory = native.path.parent / "run"
                output = directory / "inspection.html"
                database = directory / "engine.sqlite3"
                original = database.read_bytes()
                main(["--run", str(directory), "--viewer", str(output)])
                self.assertEqual(database.read_bytes(), original)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                html = output.read_text()
                self.assertNotIn("<script>", html)
                self.assertNotIn("secret-auditor", html)
                self.assertNotIn("private-event-input", html)
                self.assertNotIn("private-event-input", str(providers.calls))
                self.assertNotIn("late-backdated-evidence", html)
                page = Page(html)
                record = page.click("timeline", approval["native_id"])
                decision = page.click(record, "decision")
                self.assertIn(visible, page.text(decision).replace('\\"', '"'))
                self.assertIn("TimeoutError", page.text(page.click(decision, "call")))
                with sqlite3.connect(database) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM call_journal").fetchone()[0],
                                     len(trace))
                database.chmod(0o644)
                with self.assertRaises(PermissionError):
                    run.viewer()
                database.chmod(0o600)
