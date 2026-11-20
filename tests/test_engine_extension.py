"""Offline extension acceptance against both integrated consumers."""
import importlib
import json
from pathlib import Path
import sqlite3
import fcntl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import unittest

from engine import Engine
from test_engine_goals import Native, PowerLoss, Providers


def event(id, at, kind="noop", role=None, **payload):
    return dict(id=id, at=at, kind=kind, role=role, payload=payload)


def sales_system():
    return {"adapter": "erpnext-sales", "token_env": "ISOLATED_SALES_TOKEN",
            "erp": {"url": "http://isolated.test", "site": "isolated.test",
                    "company": "Company", "customer": "Customer",
                    "item": "SKU", "warehouse": "Warehouse"}}


class Extension(unittest.TestCase):
    def fixture(self, name, boundary=None):
        root = Path(self.enterContext(TemporaryDirectory()))
        consumer = importlib.import_module(f"consumers.{name}").Consumer({})
        native = Native(root / "native.json")
        providers = Providers()
        directory = root / "run"

        def open_run(hook=None):
            return Engine(directory, consumer, seed=41, native=native, providers=providers,
                          monotonic=monotonic, boundary=hook)

        return directory, open_run(boundary), open_run, native, providers

    def check_consumer(self, name, new_kind, fields):
        def interrupt(stage, operation):
            if stage == "result_persisted" and operation["action"].get("case") == "D":
                raise PowerLoss

        directory, run, reopen, native, providers = self.fixture(name, interrupt)
        run.schedule(**event("prior-completed", 0))
        run.schedule(**event("decision", 1, "approval_request", "manager", case="D",
                             quote="terms", open_commitments=0, amount=10))
        run.schedule(**event("prior", 15))
        with self.assertRaises(PowerLoss):
            run.run(until=1)
        with sqlite3.connect(directory / "engine.sqlite3") as db:
            original_decision = db.execute("SELECT * FROM decisions").fetchone()
        self.assertEqual(original_decision[:2], (1, "decision"))
        old_history = run.history()
        old_records = run.export()["records"]
        self.assertTrue(old_records)
        calls = list(providers.calls)

        command = dict(systems={"sales": sales_system()},
                       roles={"late": {"shifts": [[20, 40]],
                                       "permissions": [f"write:erp:{new_kind}"]}},
                       products={"offer": {"system": "sales", "item": "SKU"}},
                       events=[event("added", 10, new_kind, "late", **fields)])
        run.extend("launch", **command)
        snapshot = run.catalog
        snapshot["products"]["offer"]["item"] = "changed"
        stale = reopen()
        run.extend("other", events=[event("followup", 30)])
        with self.assertRaisesRegex(RuntimeError, "Checkpoint changed"):
            stale.schedule(**event("stale-write", 9))
        stale.extend("launch", **command)
        with self.assertRaises(ValueError):
            stale.extend("launch", **{**command, "events": []})
        with self.assertRaises(ValueError):
            stale.extend("bad", systems={"sales": sales_system()},
                         roles={"bad": {"shifts": [[0, 1]]}})
        with self.assertRaises(ValueError):
            stale.extend("rollback", roles={"bad": {"shifts": [[0, 1]]}},
                         events=[event("valid-first", 5), event("prior", 15)])
        reopened = reopen()
        self.assertEqual(reopened.history(), old_history)
        self.assertEqual(reopened.export()["records"], old_records)
        self.assertEqual(reopened.catalog["products"]["offer"]["item"], "SKU")
        self.assertEqual(set(reopened.catalog["roles"]), {"late"})
        self.assertEqual(reopened.shift_start(10, "late"), 20)
        reopened.run(until=30)
        self.assertEqual(reopened.history()[:len(old_history)], old_history)
        self.assertEqual(reopened.export()["records"][:len(old_records)], old_records)
        self.assertEqual(providers.calls, calls)
        with sqlite3.connect(directory / "engine.sqlite3") as db:
            self.assertEqual(db.execute("SELECT * FROM decisions").fetchall(), [original_decision])
        added = reopened.export()["records"][-1]
        self.assertEqual((added["kind"], added["at"]), (new_kind, 20))
        self.assertEqual([row["retry_id"] for row in native.records()],
                         ["write-1-1", "write-3-1"])
        self.assertEqual([row["id"] for row in reopened.history() if row["phase"] == "complete"],
                         ["prior-completed", "decision", "prior", "added", "followup"])

    def test_extension_preserves_systems_with_prior_pending_writes_after_upgrade(self):
        for name in ("norvale", "managed_services"):
            with self.subTest(consumer=name):
                directory, run, reopen, _, _ = self.fixture(name)
                with sqlite3.connect(directory / "engine.sqlite3") as db:
                    db.execute("DROP INDEX writes_by_system")
                    db.execute("INSERT INTO writes VALUES (?, ?)", ("write-0-1", json.dumps(
                        {"system": "old-site", "status": "pending"})))
                run = reopen()
                with self.assertRaisesRegex(ValueError, "system used by prior writes"):
                    run.extend("add", systems={"old-site": sales_system()})
                run.extend("add", systems={"new-site": sales_system()})
                self.assertEqual(set(reopen().catalog["systems"]), {"new-site"})

    def test_norvale_restart(self):
        self.check_consumer("norvale", "order", {"case": "new", "quantity": 1, "rate": 2})

    def test_managed_services_restart(self):
        self.check_consumer("managed_services", "contract",
                            {"case": "new", "response_minutes": 60, "rate": 2})

    def test_cli_during_controller_and_same_object_dispatch(self):
        entered, release = Event(), Event()

        def boundary(stage, _operation):
            if stage == "intent_persisted" and not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release controller")

        directory, run, _, _, _ = self.fixture("norvale", boundary)
        manifest = directory / "incremental.json"
        manifest.write_text('{"base":"immutable"}\n')
        run.schedule(**event("order", 1, "order", case="original", quantity=2, rate=3))
        run.schedule(**event("later", 100))
        errors = []

        def dispatch():
            try:
                run.run(until=100)
            except BaseException as exc:
                errors.append(exc)

        worker = Thread(target=dispatch)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            path = directory / "extension.json"
            path.write_text(json.dumps({"id": "hot", "events": [event("hot", 10)]}))
            with subprocess.Popen(
                [sys.executable, "-m", "engine", "--run", str(directory), "--extend", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ) as cli:
                try:
                    # The CLI must reach the boundary gate before releasing the event.
                    gate = directory / "engine-extension.lock"
                    deadline = monotonic() + 5
                    gate_held = False
                    while monotonic() < deadline:
                        with gate.open("a") as lock:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                gate_held = True
                                break
                        if cli.poll() is not None:
                            break
                        sleep(0.01)
                finally:
                    release.set()
                self.assertTrue(gate_held, "CLI did not reserve the event boundary")
                _, stderr = cli.communicate(timeout=10)
                self.assertEqual(cli.returncode, 0, stderr)
        finally:
            release.set()
            worker.join(10)
        self.assertEqual(errors, [])
        self.assertEqual([row["id"] for row in run.history() if row["phase"] == "complete"],
                         ["order", "hot", "later"])
        self.assertEqual(manifest.read_text(), '{"base":"immutable"}\n')


    def test_trusted_adapter_and_default_deny(self):
        class Consumer:
            config = {"systems": {"primary": {}}, "roles": {"legacy": {"staff": 2}},
                      "shifts": {"legacy": [[1, 9]]}}

            def handle(self, engine, kind, payload):
                engine.state["visible"] = engine.observe(engine.event["role"], as_of=engine.now)
                if kind == "write":
                    engine.write("secondary", "entry", payload)

        root = Path(self.enterContext(TemporaryDirectory()))
        primary = Native(root / "primary.json")
        secondary = Native(root / "secondary.json")
        primary.send("primary", "seed", {"at": 0, "kind": "entry"})
        routed = []

        def adapter(system, spec):
            routed.append((system, spec))
            return secondary

        run = Engine(root / "run", Consumer(), seed=41, native=primary, providers=None,
                     monotonic=monotonic, adapters={"fixture": adapter})
        with self.assertRaises(ValueError):
            run.extend("unknown", systems={"secondary": {"adapter": "untrusted"}})
        with self.assertRaises(ValueError):
            run.extend("old-role", roles={"legacy": {"shifts": [[10, 15]]}})
        with self.assertRaises(ValueError):
            run.extend("old-system", systems={"primary": {"adapter": "fixture"}})
        run.extend("setup", systems={"secondary": {"adapter": "fixture"}},
                   roles={"reader": {"shifts": [[1, 9]]},
                          "writer": {"shifts": [[1, 9]], "permissions": [
                              "observe:secondary", "write:secondary:entry"]}},
                   events=[event("hidden", 1, "observe", "reader"),
                           event("allowed", 2, "write", "writer", value=7),
                           event("seen", 3, "observe", "writer"),
                           event("blocked", 4, "write", "reader", value=8)])
        self.assertEqual(run.catalog["systems"]["primary"], {})
        self.assertEqual(run.catalog["roles"]["legacy"], {"staff": 2, "shifts": [[1, 9]]})
        run.run(until=1)
        self.assertEqual(run.state["visible"], [])
        self.assertEqual(routed, [])
        run.run(until=3)
        self.assertEqual(run.state["visible"][0]["native_id"], "secondary-1")
        self.assertEqual(routed, [("secondary", {"adapter": "fixture"})])
        with self.assertRaises(PermissionError):
            run.run(until=4)
        self.assertEqual([row["value"] for row in secondary.records()], [7])

    def test_concurrent_writers_merge_and_conflicts_do_not_commit(self):
        directory, _, reopen, _, _ = self.fixture("norvale")
        processes = []
        for index, id in enumerate(("one", "two", "one")):
            path = directory / f"extension-{index}.json"
            path.write_text(json.dumps({"id": id, "events": [event(f"event-{index}", 10)]}))
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "engine", "--run", str(directory), "--extend", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        results = [process.communicate(timeout=10) for process in processes]
        self.assertEqual(sorted(process.returncode for process in processes), [0, 0, 1], results)
        self.assertTrue(any("Conflicting extension ID" in stderr for _, stderr in results))
        run = reopen()
        run.run(until=10)
        completed = {row["id"] for row in run.history() if row["phase"] == "complete"}
        self.assertIn("event-1", completed)
        self.assertEqual(len(completed), 2)
