"""Offline engine regression tests and optional whole-checkpoint benchmark."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import monotonic, perf_counter
import unittest

from engine import DAY, Engine


class Consumer:
    def __init__(self, config=None, handle=None, identity=None):
        self.config = {} if config is None else config
        self.handle = handle
        self.resume_identity = identity


class OtherConsumer(Consumer):
    pass


def open_engine(directory, consumer, *, native=None, boundary=None, seed=41):
    return Engine(directory, consumer, seed=seed, native=native, providers=None,
                  monotonic=monotonic, boundary=boundary)


class Native:
    """Independent fake native records survive each Engine reconstruction."""

    def __init__(self):
        self.rows = {}
        self.sends = 0
        self.lookups = []

    def send(self, system, retry_id, fields):
        self.sends += 1
        if retry_id in self.rows:
            raise AssertionError("Duplicate native send")
        self.rows[retry_id] = fields
        return retry_id

    def find(self, system, retry_id):
        self.lookups.append(retry_id)
        return retry_id if retry_id in self.rows else None

    def read(self, system, native_id):
        return self.rows[native_id]

    def records(self):
        return self.rows.values()


class PowerLoss(BaseException):
    pass


class EngineResumeShifts(unittest.TestCase):
    def test_resume_binds_type_identity_seed_and_config(self):
        with TemporaryDirectory() as directory:
            config = {"shifts": {"operator": [(4, 8)]}}
            run = open_engine(directory, Consumer(config, identity="v1"))
            run.schedule("queued", "noop", 4, payload={})
            for consumer, seed, error in [
                (OtherConsumer(config, identity="v1"), 41, "consumer identity"),
                (Consumer(config, identity="v2"), 41, "consumer identity"),
                (Consumer(config, identity="v1"), 42, "seed and configuration"),
                (Consumer(identity="v1"), 41, "seed and configuration"),
            ]:
                with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                    open_engine(directory, consumer, seed=seed)
            resumed = open_engine(directory, Consumer(config, identity="v1"))
            resumed.run(until=4)
            self.assertEqual(resumed.history()[-1], {"id": "queued", "phase": "complete", "at": 4})

    def test_legacy_checkpoint_without_provenance_fails_closed(self):
        with TemporaryDirectory() as directory:
            open_engine(directory, Consumer())
            with closing(sqlite3.connect(Path(directory) / "engine.sqlite3")) as db, db:
                checkpoint = json.loads(db.execute("SELECT data FROM checkpoint").fetchone()[0])
                del checkpoint["consumer"]
                db.execute("DROP TABLE decisions")
                db.execute("UPDATE checkpoint SET data = ?", (json.dumps(checkpoint),))
            with self.assertRaisesRegex(ValueError, "Legacy checkpoint"):
                open_engine(directory, OtherConsumer())
            with closing(sqlite3.connect(Path(directory) / "engine.sqlite3")) as db:
                self.assertIsNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'decisions'"
                ).fetchone())

    def test_all_calendars_are_validated_before_creating_a_run(self):
        invalid = [None, [], {"": [(0, 1)]}, {"unused": []}, {"unused": "0-1"},
                   {"unused": [(0,)]}, {"unused": [(0, True)]},
                   {"unused": [(0, 1.5)]}, {"unused": [(-1, 1)]},
                   {"unused": [(1, 1)]}, {"unused": [(0, DAY + 1)]},
                   {"unused": [(5, 8), (4, 6)]}, {"unused": [(1, 2), (1, 2)]}]
        with TemporaryDirectory() as directory:
            for shifts in invalid:
                with self.subTest(shifts=shifts), self.assertRaises(ValueError):
                    open_engine(directory, Consumer({"shifts": shifts}))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_shift_work_pauses_and_resume_preserves_start_context(self):
        context = {}

        def handle(run, kind, payload):
            if run.event["id"] == "work":
                self.assertEqual(run.event["sequence"], 0)
            context[run.event["id"]] = (run.event_started_at, run.event["at"],
                                        run.now, run.event["role"])

        config = {"shifts": {"operator": [(560, 570), (540, 550), (570, 575)]}}
        with TemporaryDirectory() as directory:
            run = open_engine(directory, Consumer(config, handle))
            self.assertEqual(run.shift_start(550, "operator"), 560)
            self.assertEqual(run.shift_start(575, "operator"), DAY + 540)
            self.assertEqual(run.history(), [])
            for event_id, at, duration, role, elapsed in [
                ("work", 548, 45, "operator", False),
                ("boundary", 548, 2, "operator", False),
                ("handoff", 550, 11, "operator", True),
                ("unattended", 548, 45, None, False),
                ("unconfigured", 548, 45, "on-call", False),
                ("zero", 575, 0, "operator", False),
                ("shift-then-pause", 575, 11, "operator", False),
            ]:
                sequence = run.schedule(event_id, "work", at, payload={}, duration=duration,
                                        role=role, elapsed=elapsed)
                if event_id == "work":
                    self.assertEqual(sequence, 0)
            for at in (548, 550, 571, 593, DAY + 540, 2 * DAY + 542):
                run.run(until=at)
                self.assertNotIn("work", context)
                run = open_engine(directory, Consumer(config, handle))
            run.run(until=2 * DAY + 543)
            self.assertEqual(context, {
                "boundary": (548, 548, 550, "operator"),
                "handoff": (560, 560, 571, "operator"),
                "unattended": (548, 548, 593, None),
                "unconfigured": (548, 548, 593, "on-call"),
                "zero": (DAY + 540, DAY + 540, DAY + 540, "operator"),
                "shift-then-pause": (DAY + 540, DAY + 540, DAY + 561, "operator"),
                "work": (548, 548, 2 * DAY + 543, "operator"),
            })

    def test_decision_and_native_write_recover_at_each_boundary(self):
        for crash_stage in ("decision_committed", "intent_persisted", "before_send",
                            "after_send", "after_readback", "result_persisted"):
            with self.subTest(crash_stage=crash_stage), TemporaryDirectory() as directory:
                native = Native()
                calls = []
                original = {"input": {"quote": "original"}, "action": "approve"}

                def crash(stage, operation=None):
                    if stage == crash_stage:
                        raise PowerLoss(stage)

                def handle(run, kind, payload):
                    event_id = run.event["id"]
                    with self.assertRaisesRegex(ValueError, "active event"):
                        run.load_consumer_decision("another-event")
                    decision = run.load_consumer_decision(event_id)
                    if decision is None:
                        calls.append(event_id)
                        with self.assertRaisesRegex(ValueError, "complete input"):
                            run.save_consumer_decision(event_id, {"action": "approve"})
                        decision = run.save_consumer_decision(event_id, original)
                        crash("decision_committed")
                    conflict = {"input": {"quote": "changed"}, "action": "reject"}
                    self.assertEqual(run.save_consumer_decision(event_id, conflict), original)
                    decision["input"]["quote"] = "mutated returned copy"
                    self.assertEqual(run.load_consumer_decision(event_id), original)
                    run.state["cache"] = run.load_consumer_decision(event_id)["input"]
                    run.write("erp", "approval", {"action": decision["action"]})

                run = open_engine(directory, Consumer(handle=handle), native=native, boundary=crash)
                with self.assertRaisesRegex(RuntimeError, "active event"):
                    run.load_consumer_decision("first")
                run.schedule("first", "approve", 0, payload={})
                with self.assertRaisesRegex(ValueError, "Duplicate event"):
                    run.schedule("first", "approve", 1, payload={})
                with self.assertRaisesRegex(PowerLoss, crash_stage):
                    run.run(until=0)
                self.assertEqual(run.state, {})
                run = open_engine(directory, Consumer(handle=handle), native=native)
                run.run(until=0)
                self.assertEqual(calls, ["first"])
                self.assertEqual(native.sends, 1)
                if crash_stage in ("after_send", "after_readback"):
                    self.assertEqual(native.lookups, ["write-0-1"])
                self.assertEqual(run.export()["records"][0]["action"], "approve")
                run = open_engine(directory, Consumer(handle=handle), native=native)
                self.assertEqual(run.state["cache"], original["input"])
                with self.assertRaisesRegex(ValueError, "Duplicate event"):
                    run.schedule("first", "approve", 1, payload={})

    def test_cannot_backfill_decision_after_durable_native_output(self):
        with TemporaryDirectory() as directory:
            native = Native()

            def write_then_die(run, kind, payload):
                run.write("erp", "output", {})
                with self.assertRaisesRegex(RuntimeError, "before native writes"):
                    run.save_consumer_decision("first", {"input": {}})
                raise PowerLoss()

            run = open_engine(directory, Consumer(handle=write_then_die), native=native)
            run.schedule("first", "output", 0, payload={})
            with self.assertRaises(PowerLoss):
                run.run(until=0)

            def backfill(run, kind, payload):
                run.save_consumer_decision("first", {"input": {}})

            run = open_engine(directory, Consumer(handle=backfill), native=native)
            with self.assertRaisesRegex(RuntimeError, "before native writes"):
                run.run(until=0)
            self.assertEqual(native.sends, 1)

    def test_handler_observes_only_scheduled_role_and_current_or_past_records(self):
        native = Native()
        native.send("erp", "public", {"at": 1})
        native.send("erp", "manager", {"at": 1, "roles": ["manager"]})
        native.send("erp", "auditor", {"at": 1, "roles": ["auditor"]})
        native.send("erp", "malformed", {"at": 1, "roles": "not-manager"})
        native.send("erp", "future", {"at": 3, "roles": ["manager"]})

        def handle(run, kind, payload):
            run.event["role"] = payload["role"]
            with self.assertRaisesRegex(ValueError, "active event role"):
                run.observe(payload["role"], as_of=run.now)
            with self.assertRaisesRegex(ValueError, "future"):
                run.observe("manager", as_of=3)
            self.assertEqual(run.observe("manager", as_of=1), [
                {"at": 1}, {"at": 1, "roles": ["manager"]},
            ])

        with TemporaryDirectory() as directory:
            run = open_engine(directory, Consumer(handle=handle), native=native)
            run.schedule("review", "review", 1, payload={"role": "auditor"}, role="manager", duration=1)
            run.run(until=2)


def benchmark(sizes):
    print("events  edges  run_s  edges/s  sqlite_bytes  reopen_ms")
    for size in sizes:
        with TemporaryDirectory() as directory:
            def enqueue(run, kind, payload):
                for index in range(size):
                    run.schedule(f"benchmark-{index}", "noop", index + 1, payload={})

            consumer = Consumer(handle=enqueue)
            run = open_engine(directory, consumer)
            run.schedule("seed-queue", "populate", 0, payload={})
            run.run(until=0)
            start = perf_counter()
            run.run(until=size)
            seconds = perf_counter() - start
            checkpoint_bytes = (Path(directory) / "engine.sqlite3").stat().st_size
            start = perf_counter()
            resumed = open_engine(directory, consumer)
            reopen = perf_counter() - start
            edges = 2 * size
            assert len(resumed.history()) == edges + 2
            print(f"{size:6} {edges:6} {seconds:6.3f} {edges / seconds:8.1f} "
                  f"{checkpoint_bytes:12} {1000 * reopen:10.2f}", flush=True)


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["--benchmark"]:
        benchmark([int(value) for value in sys.argv[2:]])
    else:
        unittest.main()
