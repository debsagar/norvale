"""Compatibility and transaction regressions for the incremental clock."""
from contextlib import closing
import importlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import monotonic
import unittest

from engine import Engine
from engine.__main__ import main
from native.production_profiles import ADAPTER, registered
from test_engine_goals import Native
from test_engine_resume_shifts import Consumer, open_engine


class Storage(unittest.TestCase):
    def test_legacy_inflight_native_write_and_catalog_for_both_consumers(self):
        for name, kind, fields in (
            ("norvale", "order", {"quantity": 1, "rate": 2}),
            ("managed_services", "contract", {"response_minutes": 5, "rate": 2}),
        ):
            with self.subTest(consumer=name), TemporaryDirectory() as tmp:
                root = Path(tmp)
                directory = root / "scheduled-additions"
                directory.mkdir()
                database = directory / "engine.sqlite3"
                consumer = importlib.import_module(f"consumers.{name}").Consumer({})
                native = Native(root / "native.json")

                def reopen():
                    return Engine(directory, consumer, seed=41, native=native, providers=None,
                                  monotonic=monotonic, adapters={ADAPTER: None})

                spec = {"adapter": ADAPTER, "connection": "approved-profile"}
                command = {
                    "systems": {"production-SKU": spec},
                    "roles": {"operator": {"shifts": [[3, 5]], "staff": 1,
                                           "permissions": [f"write:erp:{kind}"]}},
                    "products": {"sku": {"system": "production-SKU", "item": "SKU"}},
                    "events": [{"id": "booked", "kind": "noop", "at": 3,
                                "role": "operator", "duration": 3, "payload": {}}],
                }
                history = [{"id": id, "phase": phase, "at": at}
                           for id, phase, at in [
                               ("past", "start", 0), ("past", "complete", 0),
                               ("booked", "start", 3), ("pending", "start", 3),
                           ]]
                checkpoint = {
                    "consumer": [consumer.__class__.__module__, "Consumer", None],
                    "seed": 41, "config": {}, "now": 3, "sequence": 3,
                    "history": history, "state": {"prior": ["value"]},
                    "staff_available": {"operator": [1444]},
                    "extensions": {"legacy": command},
                    "queue": [[3, 0, 2, 1, "pending", kind, {"case": "resume", **fields},
                               0, "operator", 3],
                              [1444, 0, 1, 1, "booked", "noop", {}, 3, "operator", 3]],
                }
                action = {"kind": kind, "at": 3, "case": "resume", **fields}
                retry_id = "write-2-1"
                native.send("erp", retry_id, action)
                operation = {"retry_id": retry_id, "system": "erp", "action": action,
                             "status": "pending", "target_id": None}
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute("CREATE TABLE checkpoint (id INTEGER PRIMARY KEY, data TEXT)")
                    db.execute("CREATE TABLE writes (id TEXT PRIMARY KEY, data TEXT)")
                    db.execute("INSERT INTO checkpoint VALUES (1, ?)", (json.dumps(checkpoint),))
                    db.execute("INSERT INTO writes VALUES (?, ?)", (retry_id, json.dumps(operation)))

                self.assertEqual(registered(root, "SKU"), {"production-SKU": spec})
                if name == "managed_services":
                    # Exercise migration by the offline entry point before any Engine opens.
                    path = root / "extension.json"
                    path.write_text(json.dumps({"id": "legacy", **command}))
                    main(["--run", str(directory), "--extend", str(path)], adapters={ADAPTER: None})
                else:
                    with closing(sqlite3.connect(database)) as db, db:
                        db.execute("CREATE TRIGGER fail_migration BEFORE UPDATE ON checkpoint "
                                   "BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END")
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "injected migration failure"):
                        reopen()
                    with closing(sqlite3.connect(database)) as db, db:
                        self.assertEqual(json.loads(db.execute(
                            "SELECT data FROM checkpoint").fetchone()[0]), checkpoint)
                        self.assertIsNone(db.execute(
                            "SELECT name FROM sqlite_master WHERE name = 'queue'").fetchone())
                        db.execute("DROP TRIGGER fail_migration")
                run = reopen()
                self.assertEqual(run.history(), history)
                self.assertEqual(run.state, {"prior": ["value"]})
                self.assertEqual(run.catalog["products"], command["products"])
                self.assertEqual(registered(root, "SKU"), {"production-SKU": spec})
                self.assertEqual(run.available_start(3, "operator"), 1444)
                self.assertTrue(run.has_event("past"))
                self.assertTrue(run.has_event("booked"))
                run.extend("legacy", **command)
                with self.assertRaisesRegex(ValueError, "Conflicting extension"):
                    run.extend("legacy", **{**command, "events": []})
                run.run(until=1444)
                run = reopen()
                self.assertEqual([row["id"] for row in run.history()[len(history):]],
                                 ["pending", "booked"])
                self.assertEqual(run.state["prior"], ["value"])
                self.assertEqual([call for call in native.calls if call[0] == "send"],
                                 [("send", "erp", retry_id)])
                self.assertEqual(len(run.export()["records"]), 1)

    def test_failed_edge_commit_rolls_back_history_state_followup_and_booking(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            native = Native(root / "native.json")

            def handle(run, kind, payload):
                run.state["done"] = True
                run.schedule("booked", "noop", run.now, payload={}, duration=2, role="operator",
                             book_staff=True)
                self.assertTrue(run.has_event("booked"))
                run.write("erp", "order", {})

            consumer = Consumer({"roles": {"operator": {"staff": 1}}}, handle)
            run = open_engine(root, consumer, native=native)
            run.schedule("order", "order", 0, payload={}, duration=1, elapsed=True)
            run.run(until=0)
            database = root / "engine.sqlite3"
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TRIGGER fail_commit BEFORE UPDATE ON checkpoint "
                           "BEGIN SELECT RAISE(ABORT, 'injected commit failure'); END")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected commit failure"):
                run.run(until=1)
            self.assertEqual(run.history(), [{"id": "order", "phase": "start", "at": 0}])
            self.assertEqual(run.state, {})
            self.assertFalse(run.has_event("booked"))
            self.assertEqual(run.available_start(1, "operator"), 1)
            self.assertEqual(len(run.export()["records"]), 1)
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("DROP TRIGGER fail_commit")
            run = open_engine(root, consumer, native=native)
            run.run(until=1)
            self.assertEqual(run.available_start(1, "operator"), 3)
            self.assertEqual(run.state, {"done": True})
            self.assertEqual(run.history()[-1], {"id": "booked", "phase": "start", "at": 1})
            self.assertEqual([call for call in native.calls if call[0] == "send"],
                             [("send", "erp", "write-0-1")])

    def test_minutes_and_priorities_keep_python_integer_ordering(self):
        with TemporaryDirectory() as tmp:
            run = open_engine(tmp, Consumer())
            minute = 10**30
            for id, priority in enumerate([0, -1, -(10**30), 10**30, -257, -256, 256, 257]):
                run.schedule(str(id), "noop", minute, payload={}, priority=priority)
            run.schedule("later", "noop", minute + 1, payload={}, priority=-(10**40))
            run.run(until=minute)
            self.assertEqual([row["id"] for row in run.history() if row["phase"] == "start"],
                             ["2", "4", "5", "1", "0", "6", "7", "3"])
