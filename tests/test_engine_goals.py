"""Offline acceptance contract for the engine and its two consumer plug-ins.

Run from the integration root: python3 -m unittest discover -s tests -p test_engine_goals.py -v
Use -k Milestone0 or -k Milestone1 for the first handoffs. Times are integer
minutes from Monday 2027-01-04 00:00 UTC, with half-open shifts/windows.
run(until=...) includes the stated minute.

Public integration seam (the production entry point with injected adapters):
  engine.Engine(directory, consumer, seed=..., native=..., providers=...,
                monotonic=..., boundary=...)
  schedule(id, kind, at, *, payload, priority=0, duration=0, role=None)
  run(until=...), observe(role, *, as_of), export(), history()
  consumers.norvale.Consumer(config), consumers.managed_services.Consumer(config)

Schedule commands through the queue. Lower priority values execute first;
equal time/priority uses insertion sequence. history() exposes ordered business
event edges as dictionaries with id, phase ("start"/"complete") and at. It
excludes diagnostic wall time. Queue and state belong under directory, and a
fresh Engine on that directory resumes with no re-enqueue. Fixture command
payloads and projected native record fields are specified by their use below.
Built-in queue command "noop" is diagnostic. Business command names are
consumer input events. The native adapter is append-only per test, and
records() represents the transport's query surface for role observations.
Native writes carry their business time in "at". Providers receive a text
prompt and a list of tool names. export() returns {"records": [...]}
with business records and native links. Providers and adapters are required
in this entry point, so tests can inject failures without service setup.

Milestones 2–6 add viewer(), trace(), metrics(), private_truth() and
extractor_questions() as exercised below. The primary entry points stay shared.
Adapters stand in for network transports. The Norvale Milestone 5/6 fixture
uses the recurring sales entry and ordinary company methods against independent
ERP, MES and QMS transport fixtures. No service or provider account is needed.
"""

import ast
import importlib
from html.parser import HTMLParser
import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote


DAY = 1440
OPEN = 9 * 60
PRIVATE = "engine-goal-hidden-7ef369"
CONSUMERS = ("norvale", "managed_services")


class PowerLoss(BaseException):
    """A process death must escape ordinary retry/error handling."""


class Native:
    """Disk-backed transport double, deliberately gives every POST a new ID."""

    def __init__(self, path):
        self.path = path
        self.calls = []
        self.lose_reply_for = set()

    def records(self):
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def send(self, system, retry_id, document):
        self.calls.append(("send", system, retry_id))
        rows = self.records()
        record = {**document, "system": system, "retry_id": retry_id,
                  "native_id": f"{system}-{len(rows) + 1}"}
        rows.append(record)
        self.path.write_text(json.dumps(rows))
        if system in self.lose_reply_for:
            self.lose_reply_for.remove(system)
            raise TimeoutError("fixture committed before reply was lost")
        return record["native_id"]

    def find(self, system, retry_id):
        self.calls.append(("find", system, retry_id))
        return next((row["native_id"] for row in self.records()
                     if (row["system"], row["retry_id"]) == (system, retry_id)), None)

    def read(self, system, native_id):
        self.calls.append(("read", system, native_id))
        return next(row for row in self.records()
                    if (row["system"], row["native_id"]) == (system, native_id))


class Providers:
    """Provider contract: call(tier, prompt, tools) returns raw text and usage.

    The injected monotonic clock is in seconds. A timeout still advances it.
    """

    def __init__(self):
        self.seconds = 0
        self.delay = 0
        self.fail = set()
        self.calls = []
        self.replies = {"jev": '{"judgment":true}', "model": '{"action":"approve"}'}
        self.cost = {"jev": None, "model": 0.002}
        self.first_timeout = set()

    def monotonic(self):
        return self.seconds

    def call(self, tier, prompt, tools):
        self.calls.append((tier, prompt, tools))
        self.seconds += self.delay
        if tier in self.first_timeout:
            self.first_timeout.remove(tier)
            raise TimeoutError(f"{tier} transient fixture failure")
        if tier in self.fail:
            raise TimeoutError(f"{tier} unavailable")
        return {"raw": self.replies[tier], "tokens": 17, "cost": self.cost[tier]}


class Goals(unittest.TestCase):
    def fixture(self, company, *, config=None, seed=41, boundary=None):
        # Import inside the test so discovery still identifies all red goals.
        engine = importlib.import_module("engine")
        consumer = importlib.import_module(f"consumers.{company}").Consumer
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        native = Native(root / "native.json")
        providers = Providers()

        def start(hook=boundary, transport=native):
            return engine.Engine(
                root / "run", consumer(config or {}), seed=seed, native=transport,
                providers=providers, monotonic=providers.monotonic, boundary=hook,
            )

        return start(), native, providers, start

    def schedule(self, engine, kind, at=OPEN, *, key=None, priority=0, duration=0,
                 role=None, **payload):
        engine.schedule(key or kind, kind, at, payload=payload, priority=priority,
                        duration=duration, role=role)

    def rows(self, native, kind, **match):
        return [row for row in native.records() if row["kind"] == kind
                and all(row.get(key) == value for key, value in match.items())]

    def one(self, native, kind, **match):
        rows = self.rows(native, kind, **match)
        self.assertEqual(len(rows), 1, (kind, match, rows))
        return rows[0]

    def review(self, engine):
        self.schedule(engine, "management_review", case="R1")

    def decision(self, engine, key="D1", at=OPEN, **evidence):
        defaults = {"quote": "quoted terms", "open_commitments": 2, "amount": 100}
        self.schedule(engine, "approval_request", at, key=key, case=key,
                      duration=15, role="manager", **{**defaults, **evidence})

    def workflow(self, engine, company, *, quantity=6):
        if company == "norvale":
            self.schedule(engine, "order", case="C1", quantity=quantity, rate=25)
        else:
            self.schedule(engine, "contract", OPEN, case="C1", response_minutes=60, rate=75)
            self.schedule(engine, "ticket", OPEN + 5, case="C1", ticket="T1")
            self.schedule(engine, "respond", OPEN + 25, case="C1", ticket="T1")
            self.schedule(engine, "timesheet", OPEN + 90, case="C1", ticket="T1",
                          hours=2, approved=True)
            self.schedule(engine, "timesheet", OPEN + 100, key="unapproved",
                          case="C1", ticket="T1", hours=9, approved=False)
            self.schedule(engine, "bill_period", 7 * DAY + OPEN, case="C1")

    def norvale_fixture(self, quantity, *, boundary=None):
        from norvale_goals_fixture import CompanyFixture, schedule_order, start_queue

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        company = CompanyFixture(root / "company", capacity=3)
        from types import SimpleNamespace
        from unittest.mock import patch

        authority = company.authority
        qms = SimpleNamespace(release=authority.release, get=authority.get, dispatch_lease=authority.qms_lease)
        self.enterContext(patch("integrations.company_run.authority.QMS", return_value=qms))
        providers = Providers()

        def resume(hook=None):
            company.reload()
            return start_queue(root / "queue", company, providers, boundary=hook)

        run = resume(boundary)
        schedule_order(self, run, quantity)
        return run, company, resume

    def norvale_phase(self, run, phase, at):
        self.schedule(run, "company-native", at, key=f"{phase}:{at}", phase=phase)
        if phase in ("dispatch", "settle"):
            self.schedule(run, "company-sales-readback", at, key=f"readback:{phase}:{at}", role="FINANCE")
        run.run(until=at)

    def norvale_supply(self, company, quantity):
        batch = company.receipt("supplier-receipt", quantity)
        company.inspect(batch, 5.5)
        company.company_erp.inspections()
        return batch

    def norvale_produce(self, run, company, at, material):
        self.norvale_phase(run, "plan", at)
        company.mes_observed(material_batch=material, quantity=3)
        self.norvale_phase(run, "post", at + 1)
        lot, = (key for key, lot in company.company_erp.state["lots"].items() if lot["kind"] == "finished")
        return lot

    def norvale_to_cash(self):
        run, company, resume = self.norvale_fixture(6)
        material = self.norvale_supply(company, 6)
        for day in range(2):
            at = day * DAY + OPEN
            if day == 0:
                company.erp.lost.update(("Manufacture", "Delivery Note", "Payment Entry"))
            self.norvale_phase(run, "plan", at + 1)
            report = company.mes_observed(material_batch=material, quantity=3)
            if day == 1:
                report["output"]["lot"]["source_id"] = "mes-a"
            self.norvale_phase(run, "post", at + 2)
            if day == 0:
                self.assertFalse(company.state["ops"]["meter"])
                run = resume()
            else:
                self.assertEqual(company.state["ops"]["meter"], 3)
                self.assertTrue(any("outside the pinned instance" in row["last_error"]
                                    for row in company.company_erp.state["pending"].values()))
                report["output"]["lot"]["source_id"] = "mes-b"
            self.norvale_phase(run, "post", at + 3)
            batch = next(reversed(company.company_erp.state["lots"]))
            company.inspect(batch, 5.5)
            self.norvale_phase(run, "inspect", at + 4)
            self.norvale_phase(run, "dispatch", at + 5)
            run = resume()
            self.norvale_phase(run, "dispatch", at + 6)
            self.assertEqual(len(self.rows(company, "shipment")), day + 1)
        invoices = self.rows(company, "invoice", case="C1")
        self.norvale_phase(run, "settle", 7 * DAY)
        self.assertFalse(self.rows(company, "payment"))
        self.assertTrue(all(company.erp.get("Sales Invoice", row["native_id"])["outstanding_amount"] == 75
                            for row in invoices))
        for day, invoice in enumerate(invoices, 10):
            company.remittance(f"bank-{day}", invoice["native_id"], day=day, amount=75)
        self.norvale_phase(run, "settle", 9 * DAY)
        self.assertFalse(self.rows(company, "payment"))
        self.norvale_phase(run, "settle", 10 * DAY)
        self.assertFalse(self.rows(company, "payment"))
        first, = company.erp.list("Payment Entry", {})
        self.assertEqual(first["docstatus"], 1)

        def interrupt(stage, operation):
            if stage == "result_persisted" and operation["action"]["kind"] == "payment":
                raise PowerLoss()

        run = resume(interrupt)
        with self.assertRaises(PowerLoss):
            self.norvale_phase(run, "settle", 11 * DAY)
        run = resume()
        run.run(until=11 * DAY)
        self.assertEqual(len(self.rows(company, "payment")), 2)
        self.norvale_phase(run, "settle", 14 * DAY)
        return run, company

    def service_receipt(self, engine):
        self.schedule(engine, "payer_observed", 12 * DAY + OPEN, role="payer",
                      case="C1", reference="bank-C1", amount=150, currency="USD")


class Milestone0(Goals):
    def test_shared_core_has_no_consumer_vocabulary_or_imports(self):
        engine = importlib.import_module("engine")
        root = Path(engine.__file__)
        files = sorted(root.parent.rglob("*.py")) if root.name == "__init__.py" else [root]
        words = re.compile(
            r"\b(?:batch(?:es)?|skus?|filler|shampoo|inventory|norvale|managed services|"
            r"timesheets?|bom|gitea|erpnext)\b", re.IGNORECASE)
        for source in files:
            with self.subTest(source=source):
                content = source.read_text()
                self.assertIsNone(words.search(content.replace("_", " ")),
                                  f"Consumer vocabulary in {source}")
                for node in ast.walk(ast.parse(content)):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        names = [alias.name for alias in node.names]
                        if isinstance(node, ast.ImportFrom):
                            names.append(node.module or "")
                        self.assertFalse(any("consumers" in name.split(".") for name in names))

    def test_same_engine_accepts_two_consumers_with_native_readback(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                run, native, _, _ = self.fixture(company)
                self.workflow(run, company)
                run.run(until=OPEN + 100)
                kind = "order" if company == "norvale" else "contract"
                row = self.one(native, kind, case="C1")
                self.assertIn(("read", row["system"], row["native_id"]), native.calls)
                if company == "managed_services":
                    ticket = self.one(native, "ticket", ticket="T1", contract_id=row["native_id"])
                    response = self.one(native, "ticket_response", ticket_id=ticket["native_id"], at=OPEN + 25)
                    for record in (ticket, response):
                        self.assertIn(("read", "tickets", record["native_id"]), native.calls)
                    sheets = self.rows(native, "timesheet", ticket_id=ticket["native_id"])
                    self.assertEqual([(r["hours"], r["approved"]) for r in sheets], [(2, True), (9, False)])

    def test_invalid_inputs_never_reach_transport(self):
        for company, kind, payload in [
            ("norvale", "order", dict(case="C", quantity=float("nan"), rate=2)),
            ("norvale", "order", dict(case="C", quantity=1, rate=float("inf"))),
            ("norvale", "order", dict(case="  ", quantity=1, rate=2)),
            ("managed_services", "contract", dict(case="C", response_minutes=1, rate=float("nan"))),
            ("managed_services", "contract", dict(case=True, response_minutes=1, rate=2)),
            ("managed_services", "ticket", dict(case=1, ticket="T")),
            ("managed_services", "ticket", dict(case="C", ticket="  ")),
        ]:
            with self.subTest(company=company, kind=kind, payload=payload):
                run, native, _, _ = self.fixture(company)
                with self.assertRaises(ValueError):
                    self.schedule(run, kind, **payload)
                    run.run(until=OPEN)
                self.assertFalse(native.records())
                self.assertFalse(any(call[0] == "send" for call in native.calls))

    def test_corrupt_readback_fails(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                run, native, _, _ = self.fixture(company)
                read = native.read
                native.read = lambda system, key: {**read(system, key), "case": "wrong"}
                self.workflow(run, company)
                with self.assertRaises(RuntimeError):
                    run.run(until=OPEN)


class Milestone1(Goals):
    def test_staffed_work_queues_across_a_shift_and_controller_restart(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                run, _, _, resume = self.fixture(company)
                run.extend("add-reviewers", roles={"reviewer": {"staff":2, "shifts":[[60,90]]}})
                for key, priority in [('third',1),('first',0),('second',0)]:
                    run.schedule(key,"noop",60,payload={},role="reviewer",duration=20,priority=priority)
                run.schedule("unattended","noop",60,payload={},role="reviewer",duration=90,elapsed=True)
                run.run(until=61)
                run = resume()
                run.run(until=DAY+90)
                history = {(r['id'],r['phase']):r['at'] for r in run.history()}
                self.assertEqual([history[k,'start'] for k in ('first','second','third')], [60,60,80])
                self.assertEqual(history['third','complete'], DAY+70)
                self.assertEqual(history['unattended','complete'],150)

    def test_queue_orders_by_time_priority_sequence_and_honors_shift_duration(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                run, _, _, resume = self.fixture(
                    company, config={"shifts": {"operator": [(OPEN, OPEN + 60)]}})
                for key, at, priority in [
                    ("first-inserted", OPEN + 10, 2),
                    ("early", OPEN, 2),
                    ("priority", OPEN + 10, 1),
                    ("second-inserted", OPEN + 10, 2),
                ]:
                    self.schedule(run, "noop", at, key=key, priority=priority)
                self.schedule(run, "noop", OPEN + 70, key="shifted", role="operator", duration=35)
                run.run(until=OPEN + 9)
                run = resume()
                run.run(until=DAY + OPEN + 34)
                self.assertEqual(
                    [(e["id"], e["at"]) for e in run.history() if e["phase"] == "start"],
                    [("early", OPEN), ("priority", OPEN + 10),
                     ("first-inserted", OPEN + 10), ("second-inserted", OPEN + 10),
                     ("shifted", DAY + OPEN)],
                )
                self.assertFalse(any(e["id"] == "shifted" and e["phase"] == "complete"
                                     for e in run.history()))
                run = resume()
                run.run(until=DAY + OPEN + 35)
                self.assertEqual([e["at"] for e in run.history()
                                  if e["id"] == "shifted" and e["phase"] == "complete"],
                                 [DAY + OPEN + 35])

    def test_fresh_replay_and_provider_latency_preserve_business_history(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                outcomes = []
                for latency in (0, 7200):
                    run, native, providers, _ = self.fixture(company)
                    providers.delay = latency
                    self.workflow(run, company)
                    self.decision(run)
                    run.run(until=OPEN + 14)
                    self.assertFalse(self.rows(native, "approval", case="D1"))
                    run.run(until=7 * DAY)
                    self.one(native, "approval", case="D1", at=OPEN + 15)
                    self.assertEqual([call[0] for call in providers.calls], ["jev", "model"])
                    self.assertEqual(providers.seconds, 2 * latency)
                    outcomes.append((run.history(), native.records(), run.export()))
                self.assertEqual(*outcomes)

    def test_norvale_production_completes_after_modeled_work_and_resume(self):
        run, native, _, resume = self.fixture("norvale", config={
            "opening_material": 3, "daily_capacity": 3, "production_minutes": 120,
            "shifts": {"operator": [(OPEN, OPEN + 240)]},
        })
        self.workflow(run, "norvale", quantity=3)
        run.run(until=OPEN + 119)
        self.one(native, "order", case="C1", at=OPEN)
        self.assertFalse(self.rows(native, "production", case="C1"))
        run = resume()
        run.run(until=OPEN + 120)
        produced = self.one(native, "production", case="C1", quantity=3)
        self.assertEqual((produced["started_at"], produced["at"]), (OPEN, OPEN + 120))

    def test_as_of_and_permissions_apply_inside_actual_decision(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                run, native, providers, _ = self.fixture(company)
                for token, at, roles in [
                    ("visible-past-110", OPEN - 1, ["manager"]),
                    ("visible-now-120", OPEN, ["manager"]),
                    ("future-130", OPEN + 1, ["manager"]),
                    ("restricted-140", OPEN - 1, ["auditor"]),
                ]:
                    native.send("erp", token, {"kind": "evidence", "at": at,
                                                "roles": roles, "text": token})
                self.decision(run)
                run.run(until=OPEN + 15)
                for evidence in (run.observe("manager", as_of=OPEN),
                                 *(call[1] for call in providers.calls)):
                    serialized = json.dumps(evidence)
                    self.assertIn("visible-past-110", serialized)
                    self.assertIn("visible-now-120", serialized)
                    self.assertNotIn("future-130", serialized)
                    self.assertNotIn("restricted-140", serialized)
                self.one(native, "approval", case="D1")

    def test_changed_quote_or_open_commitments_invalidates_cache(self):
        config = {"policies": [{"scope": "approval_request", "version": 1,
                                 "effective_at": 0, "review_at": DAY, "limit": 150,
                                 "approver": "director", "evidence": ["signed-authority"]}]}
        for company in CONSUMERS:
            for changed in ({"quote": "mismatched quote"}, {"open_commitments": 19}):
                with self.subTest(company=company, changed=changed):
                    run, native, providers, _ = self.fixture(company, config=config)
                    providers.replies["jev"] = '{"judgment":false}'
                    baseline = {"quote": "original quote", "open_commitments": 2, "amount": 100}
                    for index, evidence in enumerate((baseline, baseline, {**baseline, **changed})):
                        key = f"D{index}"
                        self.schedule(run, "approval_request", OPEN + index * 20, key=key,
                                      role="manager", case=key, **evidence)
                        if index == 2:
                            providers.replies.update(jev='{"judgment":true}', model='{"action":"defer"}')
                        run.run(until=OPEN + index * 20 + 15)
                    self.one(native, "approval", case="D0")
                    self.one(native, "approval", case="D1")
                    self.assertFalse(self.rows(native, "approval", case="D2"))
                    self.assertEqual([call[0] for call in providers.calls], ["jev", "jev", "model"])

    def test_provider_failure_cannot_approve(self):
        for company in CONSUMERS:
            for tier in ("jev", "model"):
                with self.subTest(company=company, tier=tier):
                    run, native, providers, _ = self.fixture(company)
                    providers.fail.add(tier)
                    self.decision(run)
                    run.run(until=OPEN + 30)
                    self.assertIn(tier, [call[0] for call in providers.calls])
                    self.assertFalse(self.rows(native, "approval", case="D1"))
                    pending = self.one(native, "decision_pending", case="D1")
                    self.assertIn(tier, pending["reason"].lower())

    def test_committed_timeout_reconciles_ticket_and_mail_before_resend(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                run, native, _, _ = self.fixture(company)
                native.lose_reply_for.update(("tickets", "mail"))
                self.review(run)
                run.run(until=2 * DAY)
                for system, kind in (("tickets", "review_followup"), ("mail", "review_notice")):
                    row = self.one(native, kind, case="R1", system=system)
                    retry = row["retry_id"]
                    send = ("send", system, retry)
                    self.assertEqual(native.calls.count(send), 1)
                    self.assertIn(("find", system, retry), native.calls[native.calls.index(send) + 1:])
                    self.assertIn(("read", system, row["native_id"]), native.calls)
                self.assertEqual(native.lose_reply_for, set())

    def test_every_operation_boundary_recovers_review_and_followups(self):
        """boundary(stage, operation) fires at each durable/write transition.

        operation exposes retry_id, source_version, evidence, action, target_id,
        status and next_attempt. Recovered writes must match no-crash readback.
        """
        stages = {"intent_persisted", "before_send", "after_send",
                  "after_readback", "result_persisted"}
        for company in CONSUMERS:
            with self.subTest(company=company):
                boundaries = []
                def record(stage, operation):
                    boundaries.append((stage, json.loads(json.dumps(operation))))
                run, native, _, _ = self.fixture(company, boundary=record)
                self.review(run)
                run.run(until=2 * DAY)
                baseline = native.records()
                for kind in ("management_review", "review_followup", "review_notice"):
                    row = self.one(native, kind, case="R1")
                    operations = [(stage, op) for stage, op in boundaries
                                  if op["retry_id"] == row["retry_id"]]
                    self.assertEqual({stage for stage, _ in operations}, stages)
                    intent = next(op for stage, op in operations if stage == "intent_persisted")
                    self.assertTrue(intent["evidence"])
                    self.assertTrue(intent["source_version"])
                    self.assertEqual(intent["action"]["kind"], kind)
                    self.assertIsNotNone(intent["next_attempt"])
                    result = next(op for stage, op in operations if stage == "result_persisted")
                    self.assertEqual(result["target_id"], row["native_id"])
                    self.assertEqual(result["status"], "confirmed")
                self.assertEqual(len(boundaries), len({(s, o["retry_id"]) for s, o in boundaries}))
                for index, (stage, _) in enumerate(boundaries):
                    with self.subTest(boundary=index, stage=stage):
                        visited = []
                        def interrupt(current_stage, operation):
                            visited.append(current_stage)
                            if len(visited) == index + 1:
                                raise PowerLoss(current_stage)
                        run, native, _, resume = self.fixture(company, boundary=interrupt)
                        self.review(run)
                        with self.assertRaisesRegex(PowerLoss, f"^{stage}$"):
                            run.run(until=2 * DAY)
                        recovered = Native(native.path)
                        run = resume(None, recovered)
                        run.run(until=2 * DAY)
                        run.run(until=3 * DAY)
                        self.assertEqual(recovered.records(), baseline)


class Page(HTMLParser):
    """Read rendered anchors and text by section, including the timeline root."""

    def __init__(self, html):
        super().__init__()
        self.sections = {}
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section":
            self.current = attrs["id"]
            self.sections[self.current] = {"text": [], "links": {}}
        if tag == "a" and self.current is not None:
            self.sections[self.current]["links"][attrs["data-link"]] = attrs["href"]

    def handle_endtag(self, tag):
        if tag == "section":
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.sections[self.current]["text"].append(data)

    def click(self, section, label):
        href = self.sections[section]["links"][label]
        if not href.startswith("#"):
            raise ValueError(f"Expected a local viewer link, got {href}")
        return unquote(href[1:])

    def text(self, section):
        return " ".join(" ".join(self.sections[section]["text"]).split())


class Milestone2(Goals):
    def test_all_native_records_have_a_three_click_evidence_path(self):
        """viewer(include_private=False) returns offline HTML.

        Sections are flat, id anchors. data-link identifies the native record
        on timeline, then "decision" and "call". Rule decisions also explain
        their input and action. trace() returns one dictionary per attempt.
        """
        for company in CONSUMERS:
            with self.subTest(company=company):
                run, native, providers, _ = self.fixture(company)
                providers.delay = 3
                providers.first_timeout.add("model")
                providers.replies["model"] = '{"action":"approve","note":"response-unique"}'
                native.send("erp", "known-evidence", {
                    "kind": "evidence", "at": OPEN - 1, "roles": ["manager"],
                    "text": "evidence-unique",
                })
                self.decision(run)
                self.review(run)
                run.run(until=2 * DAY)
                approval = self.one(native, "approval", case="D1")
                page = Page(run.viewer())
                for row in native.records():
                    if row["retry_id"] == "known-evidence":
                        continue
                    with self.subTest(record=row["native_id"]):
                        record = page.click("timeline", row["native_id"])
                        decision = page.click(record, "decision")
                        explanation = page.click(decision, "call")
                        self.assertIn(row["native_id"], page.text(record))
                        self.assertIn(row["kind"], page.text(record))
                        self.assertIn("readback", page.text(record).lower())
                        self.assertIn("event", page.text(decision).lower())
                        self.assertIn("action", page.text(decision).lower())
                        if row == approval:
                            self.assertIn("evidence-unique", page.text(decision))
                            self.assertIn("model", page.text(decision).lower())
                            self.assertIn("response-unique", page.text(explanation))
                            for tier, prompt, tools in providers.calls:
                                self.assertIn(" ".join(prompt.split()), page.text(explanation))
                                for tool in tools:
                                    self.assertIn(tool, page.text(explanation))

                trace = run.trace()
                calls = [row for row in trace if row["tier"] in {"jev", "model"}]
                self.assertEqual([row["tier"] for row in calls], ["jev", "model", "model"])
                for index, (row, (tier, prompt, tools)) in enumerate(zip(calls, providers.calls)):
                    self.assertEqual(row["role"], "manager")
                    self.assertEqual(row["prompt"], prompt)
                    self.assertEqual(row["tools"], tools)
                    self.assertEqual(row["latency_seconds"], 3)
                    self.assertEqual(row["attempt"], 2 if index == 2 else 1)
                    if index == 1:
                        self.assertIn("timeout", row["error"].lower())
                    else:
                        self.assertEqual(row["raw"], providers.replies[tier])
                        self.assertEqual(row["cost"], providers.cost[tier])
                        self.assertEqual(row["tokens"], 17)
                self.assertIn(approval["native_id"], calls[-1]["native_ids"])
                api = [row for row in trace if row["tier"] == "api"]
                self.assertEqual([(row["method"], row["system"], row["key"]) for row in api],
                                 native.calls[1:])

    def test_scale_report_counts_real_records_and_calls(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                run, native, providers, _ = self.fixture(company)
                providers.delay = 3
                self.decision(run)
                self.review(run)
                run.run(until=DAY)
                report = run.metrics()
                for system in {row["system"] for row in native.records()}:
                    self.assertEqual(report["records_by_system"][system],
                                     sum(row["system"] == system for row in native.records()))
                self.assertEqual(report["calls_by_tier"]["jev"], 1)
                self.assertEqual(report["calls_by_tier"]["model"], 1)
                self.assertEqual(report["provider_cost"], {"jev": None, "model": 0.002})
                self.assertEqual(report["tokens"], 34)
                self.assertEqual(report["call_latency_seconds"]["model"], {"median": 3, "p95": 3})
                self.assertEqual(report["wall_seconds_by_day"]["0"], 6)
                self.assertEqual(report["records_by_day"]["0"]["approval"], 1)


class Milestone3(Goals):
    def test_seeded_world_is_independent_of_company_actions(self):
        """private_truth() exposes weekly samples to the grader only.

        Config world=True enables seeded disruption and supplier reliability.
        Changing consumer actions must not consume either process's draws.
        """
        for company in CONSUMERS:
            with self.subTest(company=company):
                paths = []
                for seed, actions in ((41, False), (41, True), (42, True)):
                    run, native, _, _ = self.fixture(company, seed=seed, config={"world": True})
                    if actions:
                        self.workflow(run, company)
                    run.run(until=28 * DAY - 1)
                    truth = run.private_truth()["world"]
                    self.assertEqual([row["at"] for row in truth], [0, 7 * DAY, 14 * DAY, 21 * DAY])
                    self.assertTrue(all(row["disruption"] and row["supplier_reliability"]
                                        for row in truth))
                    paths.append(truth)
                    if actions:
                        self.assertTrue(native.records())
                self.assertEqual(paths[0], paths[1])
                self.assertNotEqual(paths[1], paths[2])

    def test_hidden_truth_stays_private_and_noisy_signal_reaches_role(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                run, native, providers, _ = self.fixture(company, config={
                    "world": True, "private_marker": PRIVATE,
                })
                self.decision(run, at=DAY + OPEN)
                run.run(until=2 * DAY)
                truth = run.private_truth()
                self.assertIn(PRIVATE, json.dumps(truth))
                signal = self.one(native, "world_bulletin")["text"]
                self.assertTrue(signal)
                private_view = run.viewer(include_private=True)
                self.assertIn(PRIVATE, private_view)
                self.assertIn("private", private_view.lower())
                self.assertTrue(providers.calls)
                for value in (run.observe("manager", as_of=DAY + OPEN), run.export(),
                              native.records(), providers.calls, run.viewer()):
                    public = json.dumps(value)
                    self.assertIn(signal, public)
                    for hidden in (PRIVATE, "supplier_reliability", "disruption_type",
                                   "actual_arrival_at", "private_truth"):
                        self.assertNotIn(hidden, public)

    def test_same_warning_can_describe_distinct_hidden_world_states(self):
        for company in CONSUMERS:
            with self.subTest(company=company):
                snapshots = []
                for state in ("false_alarm", "short_onset", "long_onset"):
                    run, native, providers, _ = self.fixture(company, config={
                        "world": True, "world_fixture": {"initial_disruption": state},
                    })
                    self.decision(run)
                    run.run(until=DAY)
                    hidden = run.private_truth()["world"][0]["disruption"]
                    warning = self.one(native, "world_bulletin")["text"]
                    self.assertIn(warning, json.dumps(run.observe("manager", as_of=OPEN)))
                    self.assertTrue(providers.calls)
                    self.assertIn(warning, providers.calls[0][1])
                    self.assertNotIn(state, providers.calls[0][1])
                    snapshots.append((hidden, warning))
                self.assertEqual(len({json.dumps(hidden, sort_keys=True)
                                      for hidden, _ in snapshots}), 3)
                self.assertEqual(len({warning for _, warning in snapshots}), 1)

    def test_delay_notice_precedes_physical_arrival(self):
        """The logistics case belongs to Norvale. A disruption crosses weeks."""
        run, native, _, resume = self.fixture("norvale", config={
            "world": True, "world_fixture": {"transit_delay_minutes": 7 * DAY},
        })
        self.schedule(run, "dispatch_supply", case="S1", eta=14 * DAY, quantity=30)
        run.run(until=14 * DAY)
        notice = self.one(native, "delay_notice", case="S1")
        self.assertLess(notice["at"], 21 * DAY)
        self.assertEqual(notice["previous_eta"], 14 * DAY)
        self.assertFalse(self.rows(native, "receipt", case="S1"))
        run = resume()
        run.run(until=21 * DAY)
        self.one(native, "receipt", case="S1", at=21 * DAY, quantity=30)


class Milestone4(Goals):
    def test_trigger_approval_visibility_and_effective_version(self):
        initial = {"scope": "approval_request", "version": 1, "effective_at": 0,
                   "review_at": 7 * DAY, "limit": 80, "approver": "director",
                   "evidence": ["opening-mandate"]}
        for company in CONSUMERS:
            with self.subTest(company=company):
                run, native, providers, _ = self.fixture(company, config={"policies": [initial]})
                providers.replies.update(jev='{"judgment":false}',
                                         model='{"limit":150,"action":"propose"}')
                effective, review = 3 * DAY + OPEN, 5 * DAY + OPEN
                self.schedule(run, "policy_review", OPEN, key="unauthorized",
                              role="operator", trigger="incident", evidence=["incident-A"],
                              effective_at=effective, review_at=review)
                self.schedule(run, "policy_review", DAY + OPEN, key="missing-trigger",
                              role="director", evidence=["incident-B"],
                              effective_at=effective, review_at=review)
                self.schedule(run, "policy_review", 2 * DAY + OPEN, key="authorized",
                              role="director", trigger="audit", evidence=["audit-C"],
                              effective_at=effective, review_at=review)
                self.decision(run, key="initial", at=OPEN + 30, amount=75)
                self.decision(run, key="before", at=effective - 16)
                self.decision(run, key="old-limit", at=effective + 30, amount=75)
                self.decision(run, key="effective", at=effective)
                self.decision(run, key="expired", at=review)
                self.decision(run, key="no-fallback", at=review + 15, amount=75)
                run.run(until=2 * DAY)
                self.assertFalse(self.rows(native, "policy_note", version=2))
                run.run(until=review + 30)
                note = self.one(native, "policy_note", system="erp", version=2,
                                scope="approval_request", approver="director",
                                effective_at=effective, review_at=review,
                                evidence=["audit-C"])
                self.one(native, "policy_setting", version=2, system="erp", limit=150,
                         note_id=note["native_id"])
                self.assertIn(note["native_id"], json.dumps(run.export()))
                self.one(native, "approval", case="initial", policy_version=1)
                self.assertFalse(self.rows(native, "approval", case="before"))
                self.one(native, "approval", case="old-limit", policy_version=2)
                self.one(native, "approval", case="effective", policy_version=2)
                self.assertFalse(self.rows(native, "approval", case="expired"))
                self.assertFalse(self.rows(native, "approval", case="no-fallback"))
                self.assertEqual(len(self.rows(native, "policy_note", version=2)), 1)


class Milestone5(Goals):
    def test_norvale_partial_shipment_to_cash_and_conserving_ledgers(self):
        run, company = self.norvale_to_cash()
        erp = company.erp
        bills = self.rows(company, "invoice", case="C1")
        shipments = self.rows(company, "shipment", case="C1")
        self.assertEqual([row["quantity"] for row in shipments], [3, 3])
        self.assertEqual({row["shipment_id"] for row in bills}, {row["native_id"] for row in shipments})
        self.assertEqual(sum(row["amount"] for row in bills), 150)
        payments = self.rows(company, "payment", case="C1")
        self.assertEqual({row["invoice_id"] for row in payments}, {row["native_id"] for row in bills})
        self.assertEqual(sum(row["amount"] for row in payments), 150)
        movements = company.erp.list("Stock Ledger Entry", {})
        self.assertEqual(sum(row["actual_qty"] for row in movements if row["item_code"] == "FG"), 0)
        opening, = erp.list("Purchase Receipt", {})
        self.assertEqual(sum(row["actual_qty"] for row in movements if row["item_code"] == "RM"
                             and row["voucher_no"] != opening["name"]), -6)
        ledger = erp.list("GL Entry", {})
        self.assertEqual(sum(row["debit"] - row["credit"] for row in ledger), 0)
        self.assertEqual(sum(row["debit"] - row["credit"] for row in ledger
                             if row["account"] == "Bank GL"), 150)
        for shipment in shipments:
            delivery = erp.get("Delivery Note", shipment["native_id"])
            batch = delivery["items"][0]["batch_no"]
            inspection = company.authority.inspections[batch]
            self.assertEqual(inspection["workflow_state"], "Released")
            self.assertLessEqual(inspection["minute"], delivery["minute"])
            self.assertIn(inspection["name"], delivery["custom_norvale_quality_releases"])
        for kind in ("Delivery Note", "Sales Invoice", "Payment Entry"):
            self.assertEqual(erp.counters[kind], 2)
            self.assertEqual(sum(call[:2] == ("submit", kind) for call in erp.calls), 2)
        completions = [row["report"] for row in company.company_erp.state["completions"].values()]
        self.assertEqual({report["source"] for report in completions}, {"mes-a", "mes-b"})
        self.assertEqual({report["id"] for report in completions},
                         {"mes-a:mrp.production:1", "mes-b:mrp.production:1"})
        self.assertEqual({report["output"]["lot_id"] for report in completions},
                         {"mes-a:stock.lot:1", "mes-b:stock.lot:1"})
        self.assertEqual(len({row["items"][-1]["batch_no"] for row in erp.list(
            "Stock Entry", {"stock_entry_type": "Manufacture"})}), 2)
        self.assertEqual(company.state["ops"]["meter"], 6)
        self.assertEqual(len(run.export()["records"]), 6)

    def test_norvale_failed_or_missing_qa_stays_held_through_dispatch_attempt(self):
        for reading in (4.5, None, 5.5):
            with self.subTest(reading=reading):
                run, company, _ = self.norvale_fixture(3)
                material = self.norvale_supply(company, 3)
                batch = self.norvale_produce(run, company, OPEN + 1, material)
                company.inspect(batch, reading)
                self.norvale_phase(run, "inspect", OPEN + 3)
                self.norvale_phase(run, "dispatch", OPEN + 4)
                flow = company.company_erp
                if reading == 5.5:
                    shipment = self.one(company, "shipment", case="C1", quantity=3)
                    doc = company.erp.get("Delivery Note", shipment["native_id"])
                    self.assertEqual(doc["items"][0]["batch_no"], batch)
                else:
                    self.assertEqual(flow.balance(flow.state["lots"][batch], "Hold", "QA"), 3)
                    self.assertEqual(flow.balance(flow.state["lots"][batch], "FG", "QA"), 0)
                    self.assertFalse(self.rows(company, "shipment"))
                    self.assertFalse(self.rows(company, "invoice"))
                    blocked, = [row for key, row in flow.state["pending"].items() if key.startswith("dispatch:")]
                    self.assertIn("QMS release", blocked["last_error"])
                if reading is None:
                    self.assertNotIn(batch, company.authority.inspections)
                else:
                    observed = company.authority.inspections[batch]
                    self.assertEqual(float(observed["readings"][0]["reading_1"]), reading)
                    self.assertEqual(observed["workflow_state"], "Released" if reading == 5.5 else "Rejected")

    def test_norvale_maintenance_and_shortage_each_block_then_resume(self):
        for obstacle in ("maintenance", "shortage"):
            with self.subTest(obstacle=obstacle):
                run, company, resume = self.norvale_fixture(3)
                flow = company.company_erp
                if obstacle == "maintenance":
                    material = self.norvale_supply(company, 3)
                    fault = flow.equipment.observe(1, "observed-fault", "Guard fault", 0)
                    self.norvale_phase(run, "service", OPEN - 1)
                self.norvale_phase(run, "plan", OPEN + 1)
                self.assertFalse(flow.state["work"])
                self.assertFalse(company.erp.list("Stock Entry", {"stock_entry_type": "Manufacture"}))
                if obstacle == "shortage":
                    self.assertEqual(company.state["ops"]["shortage_days"], {"RM": [0]})
                else:
                    self.assertIn("verified restart", flow.state["pending"]["equipment:FG"]["last_error"])
                run = resume()
                if obstacle == "shortage":
                    material = self.norvale_supply(company, 3)
                    self.assertEqual(len(company.erp.list("Purchase Receipt", {})), 1)
                else:
                    company.authority.restart = dict(id="restart-check", version="1", ref="tickets-a:1")
                    self.norvale_phase(run, "service", DAY + OPEN)
                    case = company.company_erp.equipment.cases[fault]
                    self.assertEqual(case["restart"], "RESTART-1")
                    self.assertEqual(case["verification"]["ref"],
                                     f"{case['ticket']['source_id']}:{case['ticket']['number']}")
                batch = self.norvale_produce(run, company, DAY + OPEN + 1, material)
                company.inspect(batch, 5.5)
                self.norvale_phase(run, "inspect", DAY + OPEN + 3)
                self.norvale_phase(run, "dispatch", DAY + OPEN + 4)
                run = resume()
                self.norvale_phase(run, "dispatch", 14 * DAY)
                self.one(company, "shipment", case="C1", quantity=3)
                output, = company.erp.list("Stock Entry", {"stock_entry_type": "Manufacture"})
                self.assertEqual(output["fg_completed_qty"], 3)
                movement, = company.erp.list("Stock Ledger Entry",
                    {"voucher_no": output["name"], "item_code": "RM"})
                self.assertEqual(movement["actual_qty"], -3)

    def test_services_sla_deadline_and_approved_hours_to_cash_without_stock(self):
        run, native, _, resume = self.fixture("managed_services")
        self.workflow(run, "managed_services")
        self.schedule(run, "ticket", OPEN + 6, key="late", case="C1", ticket="T2")
        self.schedule(run, "respond", OPEN + 67, key="late-response", case="C1", ticket="T2")
        self.service_receipt(run)
        run.run(until=OPEN + 65)
        self.assertFalse(self.rows(native, "sla_breach"))
        self.assertFalse(self.rows(native, "invoice"))
        run = resume()
        run.run(until=14 * DAY)
        self.one(native, "sla_breach", ticket="T2", at=OPEN + 66)
        self.one(native, "ticket_response", ticket="T1", at=OPEN + 25)
        self.one(native, "ticket_response", ticket="T2", at=OPEN + 67)
        approved = self.one(native, "timesheet", ticket="T1", approved=True, hours=2)
        self.one(native, "timesheet", ticket="T1", approved=False, hours=9)
        invoice = self.one(native, "invoice", case="C1", amount=150)
        self.assertEqual(invoice["timesheet_ids"], [approved["native_id"]])
        self.one(native, "payment", invoice_id=invoice["native_id"], amount=150)
        self.assertFalse(self.rows(native, "stock_movement"))

    def test_services_outage_blocks_work_then_reassignment_resumes_once(self):
        run, native, _, resume = self.fixture("managed_services")
        self.schedule(run, "contract", case="C1", response_minutes=60, rate=75)
        self.schedule(run, "staff_unavailable", OPEN + 1, case="C1", technician="A")
        self.schedule(run, "ticket", OPEN + 5, case="C1", ticket="T1")
        self.schedule(run, "respond", OPEN + 10, case="C1", ticket="T1", technician="A")
        self.schedule(run, "reassign", OPEN + 30, case="C1", ticket="T1", technician="B")
        run.run(until=OPEN + 29)
        self.one(native, "response_blocked", ticket="T1", reason="staff_unavailable")
        self.assertFalse(self.rows(native, "ticket_response"))
        run = resume()
        run.run(until=2 * DAY)
        self.one(native, "ticket_response", ticket="T1", technician="B")
        self.assertFalse(self.rows(native, "sla_breach", ticket="T1"))

    def test_services_duplicate_billing_and_partial_observed_payment_after_restart(self):
        def boundary(stage, operation):
            if stage == "result_persisted" and operation["action"]["kind"] == "invoice":
                raise PowerLoss()

        run, native, providers, resume = self.fixture("managed_services", boundary=boundary)
        self.schedule(run, "contract", case="C1", response_minutes=60, rate=33.33)
        self.schedule(run, "contract", key="other-contract", case="C2", response_minutes=60, rate=99)
        self.schedule(run, "ticket", OPEN + 1, case="C1", ticket="T")
        self.schedule(run, "ticket", OPEN + 2, key="other-ticket", case="C2", ticket="T")
        self.schedule(run, "timesheet", OPEN + 5, case="C1", ticket="T", hours=0.5, approved=True)
        self.schedule(run, "timesheet", OPEN + 6, key="other-sheet", case="C2", ticket="T",
                      hours=1, approved=True)
        bill_at = DAY + OPEN
        self.schedule(run, "bill_period", bill_at, case="C1")
        self.schedule(run, "bill_period", bill_at + 1, key="duplicate-bill", case="C1")
        with self.assertRaises(PowerLoss):
            run.run(until=bill_at)
        run = resume(None)
        run.run(until=5 * DAY)
        invoice = self.one(native, "invoice", case="C1", currency="USD", amount=16.67)
        approved = self.one(native, "timesheet", case="C1")
        self.assertEqual(invoice["timesheet_ids"], [approved["native_id"]])
        self.assertLess(invoice["due"], 5 * DAY)
        self.assertFalse(self.rows(native, "payment"))
        receipt = dict(case="C1", reference="bank-partial", amount=5.03, currency="USD")
        native.lose_reply_for.add("erp")
        self.schedule(run, "payer_observed", 5 * DAY, role="payer", **receipt)
        self.schedule(run, "timesheet", 5 * DAY + 2, key="new-sheet", case="C1", ticket="T",
                      hours=1, approved=True)
        self.schedule(run, "bill_period", 5 * DAY + 3, key="new-bill", case="C1")
        self.schedule(run, "payer_observed", 5 * DAY + 4, key="duplicate-receipt", role="payer",
                      **receipt)
        run = resume(None)
        run.run(until=6 * DAY)
        payment = self.one(native, "payment", amount=5.03, outstanding=11.64, invoice_id=invoice["native_id"])
        self.assertEqual(native.calls.count(("send", "erp", payment["retry_id"])), 1)
        self.assertIn(("find", "erp", payment["retry_id"]), native.calls)
        second = self.one(native, "invoice", amount=33.33)
        self.assertEqual(len(second["timesheet_ids"]), 1)
        self.assertNotIn(approved["native_id"], second["timesheet_ids"])
        self.assertFalse(providers.calls)


class Milestone6(Goals):
    def test_extractor_answers_are_private_and_reconstructible_from_native_links(self):
        """extractor_questions() exposes id and question. Grader uses private_truth()."""
        for company in CONSUMERS:
            with self.subTest(company=company):
                if company == "norvale":
                    run, native = self.norvale_to_cash()
                else:
                    run, native, _, _ = self.fixture(company)
                    self.workflow(run, company)
                    self.service_receipt(run)
                    run.run(until=14 * DAY)
                records = run.export()["records"]
                invoice = [row for row in records if row["kind"] == "invoice" and row["case"] == "C1"]
                self.assertTrue(invoice)
                native_ids = {row["native_id"] for row in native.records()}
                self.assertTrue({row["native_id"] for row in records} <= native_ids)
                answers = {"C1-invoiced": sum(row["amount"] for row in invoice)}
                if company == "norvale":
                    shipped = {row["native_id"]: row for row in records if row["kind"] == "shipment"}
                    answers["C1-billed-units"] = sum(shipped[row["shipment_id"]]["quantity"] for row in invoice)
                    self.assertEqual(answers["C1-billed-units"], 6)
                else:
                    sheets = {row["native_id"]: row for row in records if row["kind"] == "timesheet"}
                    answers["C1-billed-hours"] = sum(sheets[key]["hours"] for row in invoice
                                                     for key in row["timesheet_ids"])
                    self.assertEqual(answers["C1-billed-hours"], 2)
                payments = [row for row in records if row["kind"] == "payment"
                            and row["invoice_id"] in {bill["native_id"] for bill in invoice}]
                answers["C1-paid"] = sum(row["amount"] for row in payments)
                self.assertEqual(answers["C1-paid"], 150)
                questions = run.extractor_questions()
                self.assertEqual({row["id"] for row in questions}, set(answers))
                for question in questions:
                    self.assertEqual(set(question), {"id", "question"})
                    prompt = question["question"].lower()
                    self.assertIn("c1", prompt)
                    metric = question["id"].removeprefix("C1-").split("-")[-1]
                    self.assertIn(metric, prompt)
                self.assertEqual(run.private_truth()["answers"], answers)
                earlier = run.private_truth(as_of=10 * DAY)["answers"]
                self.assertIsNone(earlier["C1-paid"])
                self.assertEqual(earlier["C1-invoiced"], 150)
                self.assertNotIn("answers", run.export())
                self.assertTrue(set(answers).isdisjoint(run.export()))
                self.assertNotIn('"answers"', json.dumps(run.viewer()))
