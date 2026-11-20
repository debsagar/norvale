"""Recurring purchasing with offline ERP, inbox and provider transports."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.policy import SMTP as WIRE, default
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from native.company import DEFAULT_CONFIG, ERP_ROLES, Engine
from native.supplier_amendments import SUBJECT, sender_identities
from norvale.agents.comms import Comms
from walkthrough.engine import ROLES

EPOCH = datetime(2027, 1, 1, tzinfo=timezone.utc)
BEFORE_REVIEW = EPOCH + timedelta(days=1, hours=7)
MAIL = {"mail": {"host": "offline.invalid", "smtp": 25, "imap": 143}}
SUPPLIER = "brenner-quotes@norvale.test"
OTHER = "kemix-quotes@norvale.test"
BUYER, MANAGER = [next(row["email"].replace(".example", ".test")
                       for row in ROLES if row["role_id"] == role)
                  for role in ("BUYER_CHEM", "SC_MGR")]
PO = "d0000-purchase-BRENNER"
QUOTE = "quote:" + PO
REVIEW = "d0001:approvals"


class ERP:
    """Native-shaped documents and a workflow fixture driven by Norvale's setup."""

    def __init__(self):
        self.docs = {}
        self.gets = []
        self.transitions = []

    def client(self, role, day):
        user = role.lower() + "@norvale.test"

        def get(kind, name):
            self.gets.append([role, kind, name])
            record = self.docs[kind, name]
            if kind == "ToDo" and role == "BUYER" and record["allocated_to"] != user:
                raise PermissionError("Fixture: ToDo is assigned to a different user")
            return deepcopy(record)

        def list_records(kind, filters, fields=None, **_):
            if kind == "Custom Field":
                return [{"name": "operation-key-fixture"}]

            def matches(record):
                for field, value in filters.items():
                    if isinstance(value, list):
                        operator, bound = value
                        actual = record[field]
                        if not {"in": lambda: actual in bound,
                                "not in": lambda: actual not in bound,
                                "<=": lambda: actual <= bound, "<": lambda: actual < bound}[operator]():
                            return False
                    elif record.get(field) != value:
                        return False
                return True

            return [{field: row.get(field) for field in fields or ["name"]}
                    for (doctype, _), row in self.docs.items() if doctype == kind and matches(row)]

        def create(kind, body):
            if kind in ("Supplier Quotation", "Purchase Order"):
                assert role == "BUYER"
            if kind == "ToDo":
                assert role in ("PLANNER", "SCM_MGR")
            serial = 1 + sum(dt == kind for dt, _ in self.docs)
            prefix = {"Supplier Quotation": "SQ", "Purchase Order": "PO"}.get(kind, kind)
            name = body.get("workflow_name", f"{prefix}-{serial:04}")
            today = day().date().isoformat()
            doc = dict(deepcopy(body), name=name, doctype=kind, owner=user, docstatus=0,
                       creation=today, modified=today, modified_by=user)
            if kind in ("Supplier Quotation", "Purchase Order"):
                doc.setdefault("transaction_date", today)
                for i, line in enumerate(doc["items"], 1):
                    line.update(name=f"{name}-{i}", uom="Kg", received_qty=0,
                                amount=round(line["qty"] * line["rate"], 2))
                doc["grand_total"] = round(sum(line["amount"] for line in doc["items"]), 2)
                doc["currency"] = "EUR"
            if kind == "Purchase Order":
                doc.update(workflow_state="Pending Approval", status="Draft")
            self.docs[kind, name] = doc
            return deepcopy(doc)

        def update(kind, name, fields):
            assert (role, kind, fields) == ("BUYER", "Supplier Quotation", {"docstatus": 1})
            self.docs[kind, name].update(fields)
            return get(kind, name)

        def call(method, args):
            assert method == "frappe.model.workflow.apply_workflow"
            name = json.loads(args["doc"])["name"]
            doc = self.docs["Purchase Order", name]
            workflow = self.docs["Workflow", "Norvale purchasing approval"]
            transition = next(row for row in workflow["transitions"]
                              if row["state"] == doc["workflow_state"] and row["action"] == args["action"])
            if (transition["allowed"] not in ERP_ROLES[role]
                    or (doc["owner"] == user and not transition["allow_self_approval"])):
                self.transitions.append([role, args["action"], name, "denied"])
                raise PermissionError("Fixture: Norvale configured workflow denies transition")
            state = next(row for row in workflow["states"] if row["state"] == transition["next_state"])
            doc.update(workflow_state=state["state"], docstatus=int(state["doc_status"]),
                       status="To Receive and Bill", modified=day().date().isoformat(), modified_by=user)
            self.transitions.append([role, args["action"], name, "accepted"])

        return SimpleNamespace(get=get, list=list_records, create=create, update=update, call=call)


class MailTransport:
    """SMTP/IMAP protocol doubles with independent address-scoped inboxes."""

    def __init__(self, boxes, clock, *_args, **_kwargs):
        self.boxes, self.clock = boxes, clock

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def login(self, address, _password):
        self.address = address

    def send_message(self, message):
        assert message["From"] == self.address
        message["Return-Path"] = f"<{self.address}>"
        self.boxes.setdefault(str(message["To"]), []).append((self.clock[0], message.as_bytes(policy=WIRE)))

    def select(self, folder, readonly=False):
        assert folder == "INBOX"
        return "OK", []

    def _quote(self, value):
        return '"' + value + '"'

    def uid(self, command, *args):
        box = self.boxes.get(self.address, [])
        if command == "search":
            return "OK", [b" ".join(str(i).encode() for i in range(1, len(box) + 1))]
        uid, query = args
        received, wire = box[int(uid) - 1]
        attrs = b"FLAGS ()"
        if query == "(INTERNALDATE BODY.PEEK[])":
            attrs += b' INTERNALDATE "' + received.strftime("%d-%b-%Y %H:%M:%S %z").encode() + b'"'
        elif query != "(RFC822)":
            wire = wire.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        return "OK", [(attrs, wire)]


class SupplierAmendmentsOffline(TestCase):
    def setUp(self):
        def transport(*args, **kwargs):
            return MailTransport(self.boxes, self.clock)

        fixtures = (
            patch("norvale.agents.comms.smtplib.SMTP", transport),
            patch("norvale.agents.comms.imaplib.IMAP4", transport),
            patch.object(socket.socket, "connect", side_effect=AssertionError("Network disabled")),
            patch.dict(os.environ, NORVALE_OFFLINE="0", OPENAI_API_KEY="fixture", TYPESAFE_API_KEY="fixture"),
            patch("native.company.Stack", return_value=SimpleNamespace(
                endpoints=lambda: MAIL, settle=lambda: None)),
        )
        for fixture in fixtures:
            fixture.start()
            self.addCleanup(fixture.stop)

    def opening(self, root):
        root.mkdir()
        self.boxes = {}
        self.clock = [EPOCH]
        self.erp = ERP()
        self.requests = []
        self.root = root
        config = dict(DEFAULT_CONFIG, seed=42,
                      supplier_amendment_senders={"BRENNER": SUPPLIER, "KEMIX": OTHER})
        (root / "config.json").write_text(json.dumps(config))
        (root / "env").mkdir()
        (root / "env/secrets.json").write_text(json.dumps({
            role: {"password": "fixture"} for role in ("BUYER_CHEM", "SC_MGR")
        }))
        e = self.reopen()
        e.state["ops"] = dict(purchases={}, changes={})
        e.state["standing_policies"] = [
            dict(role="SCM_MGR", suppliers=["Supplier-0001"], items=["SLES"],
                 approval_limit=200, reorder_target=0, effective_date="2027-01-01",
                 review_date="2027-02-01")
        ]
        self.erp.docs["Item", "SLES"] = {"name": "SLES"}
        self.erp.docs["Stock Ledger Entry", "SLE-0001"] = {
            "name": "SLE-0001", "posting_date": e.date(), "actual_qty": -10,
            "warehouse": e.wh["rm"], "item_code": "SLES",
        }
        e.write("vendor:BRENNER", "Supplier", {"supplier_name": "Brenner Tensid AG"})
        e.workflow_setup()
        e._step((0, "d0000:replenishment", e.replenishment))
        self.draft = e.get(PO, "BUYER")
        self.quote = e.get(QUOTE, "BUYER")
        self.terms = dict(supplier=self.draft["supplier"], quote=self.quote["name"],
                          quote_line=self.quote["items"][0]["name"], po=self.draft["name"],
                          po_line=self.draft["items"][0]["name"], currency="EUR", uom="Kg",
                          unit_price="1.85", effective_at=BEFORE_REVIEW.isoformat())

    def reopen(self):
        e = Engine(self.root)
        e.api = {role: self.erp.client(role, lambda: self.clock[0])
                 for role in ("BUYER", "SCM_MGR", "PLANNER")}
        e.admin = self.erp.client("ADMIN", lambda: self.clock[0])
        e.run_additions = Mock()
        e.advance_clock = lambda time: self.clock.__setitem__(0, time)
        return e

    def send(self, *, sender=SUPPLIER, received=BEFORE_REVIEW, message_id="<change-1@norvale.test>",
             other_envelope=False, **changes):
        self.clock[0] = received
        Comms({"mail": sender, "chat": "fixture"}, {"password": "fixture"}, MAIL).send_mail(
            [BUYER], SUBJECT, json.dumps({**self.terms, **changes}), BEFORE_REVIEW, message_id=message_id)
        if other_envelope:
            at, wire = self.boxes[BUYER][-1]
            self.boxes[BUYER][-1] = (at, wire.replace(b"Return-Path: <" + sender.encode(),
                                                    b"Return-Path: <" + OTHER.encode()))

    def review(self, *, action="approve", interrupt=False, day=1, classifier=1):
        def response(req, _on_attempt):
            if "typesafe" in req.full_url:
                return {"answers": {"judgment": {"noul": classifier}}}
            question = json.loads(json.loads(req.data)["messages"][0]["content"])
            self.requests.append(question)
            if interrupt:
                raise RuntimeError("Injected interruption after buyer referral")
            output = dict(action=action, reason="Fixture outcome", message="Fixture outcome")
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}]}

        e = self.reopen()
        with patch("router.request_json", side_effect=response):
            e._step((day, f"d{day:04}:approvals", e.approvals))
        return e

    def referrals(self):
        return [wire for _, wire in self.boxes[MANAGER] if b"Supplier amendment referred for review" in wire]

    def test_same_opening_offline_and_manager_receives_effective_price(self):
        with TemporaryDirectory() as tmp:
            opened = []
            outcomes = []
            for amended in (False, True):
                with self.subTest(amended=amended):
                    self.opening(Path(tmp) / str(amended))
                    opened.append((self.draft, self.quote))
                    if amended:
                        self.send()
                    with self.assertRaises(PermissionError):
                        self.reopen().api["BUYER"].call("frappe.model.workflow.apply_workflow",
                                                       {"doc": json.dumps(self.draft), "action": "Approve"})
                    # The provider supplies the same action in both cases.
                    e = self.review()
                    evidence = self.requests[0]["evidence"]["observations"]
                    effective = evidence.get("supplier_amendments", [])
                    self.assertEqual(len(effective), int(amended))
                    self.assertEqual(evidence["items"][0]["rate"], 1.42)
                    self.assertEqual(evidence["supplier_quote"]["items"][0]["rate"], 1.42)
                    if amended:
                        self.assertEqual(effective[0]["terms"], self.terms)
                        self.assertEqual(effective[0]["draft_unit_price"], 1.42)
                        self.assertTrue(effective[0]["current_for_line"])
                        self.assertIn(effective[0]["buyer_response_message_id"].encode(), self.referrals()[0])
                        referral = message_from_bytes(self.referrals()[0], policy=default)
                        self.assertEqual(referral["From"], BUYER)
                        self.assertEqual(referral["To"], MANAGER)
                        self.assertEqual(json.loads(referral.get_content())["message_id"], "<change-1@norvale.test>")
                        self.assertEqual(self.requests[0]["role"], "SCM_MGR")
                    self.assertEqual(len(self.referrals()), int(amended))
                    po = e.get(PO, "SCM_MGR")
                    outcomes.append((po["workflow_state"], po["items"][0]["rate"]))
            self.assertEqual(*opened)
            self.assertEqual(outcomes, [("Approved", 1.42), ("Approved", 1.42)])

    def test_wrong_binding_and_as_of_delivery_offline(self):
        cases = {
            "wrong quote row": {"quote_line": "SQ-not-this-row"},
            "wrong PO row": {"po_line": "PO-not-this-row"},
            "wrong UOM": {"uom": "Nos"},
            "wrong currency": {"currency": "USD"},
            "wrong supplier mailbox": {"sender": OTHER},
            "spoofed header and different envelope": {"other_envelope": True},
            "different supplier in terms": {"supplier": "another supplier"},
            "future delivery with backdated header": {"received": EPOCH + timedelta(days=1, hours=13)},
            "future effective terms": {"effective_at": (EPOCH + timedelta(days=2)).isoformat()},
        }
        with TemporaryDirectory() as tmp:
            for index, (scenario, changes) in enumerate(cases.items()):
                with self.subTest(scenario=scenario):
                    self.opening(Path(tmp) / str(index))
                    if scenario == "wrong quote row":
                        # This is a real row with the same item and UOM, but the PO points elsewhere.
                        quote = self.erp.docs["Supplier Quotation", self.quote["name"]]
                        quote["items"].append(dict(quote["items"][0], name=changes["quote_line"]))
                    self.send(**changes)
                    self.review()
                    evidence = self.requests[0]["evidence"]["observations"]
                    self.assertNotIn("supplier_amendments", evidence)
                    self.assertEqual(self.referrals(), [])

    def test_duplicate_and_reopen_offline_bypass_standing_policy(self):
        with TemporaryDirectory() as tmp:
            self.opening(Path(tmp) / "retry")
            self.send()
            self.send(received=BEFORE_REVIEW + timedelta(minutes=1))
            with self.assertRaisesRegex(RuntimeError, "Injected interruption"):
                self.review(interrupt=True, classifier=0)
            self.assertEqual(len(self.referrals()), 1)
            self.assertEqual(self.reopen().get(PO, "BUYER")["docstatus"], 0)
            e = self.review(action="investigate", classifier=0)
            self.assertEqual(e.get(PO, "SCM_MGR")["docstatus"], 0)
            self.assertEqual(len(self.referrals()), 1)
            self.assertEqual(self.requests[0], self.requests[1])
            self.assertEqual(len(self.requests[1]["evidence"]["observations"]["supplier_amendments"]), 1)
            self.assertEqual(self.erp.transitions, [])
            pending = [row for (kind, _), row in self.erp.docs.items()
                       if kind == "ToDo" and row.get("reference_name") == self.draft["name"]]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["allocated_to"], "buyer@norvale.test")

    def test_latest_effective_terms_offline(self):
        with TemporaryDirectory() as tmp:
            self.opening(Path(tmp) / "current")
            later = BEFORE_REVIEW + timedelta(hours=1)
            self.send(message_id="<later@norvale.test>", unit_price="1.95", effective_at=later.isoformat())
            self.send()
            self.review(action="defer")
            evidence = self.requests[0]["evidence"]["observations"]["supplier_amendments"]
            self.assertEqual([(row["terms"]["unit_price"], row["current_for_line"]) for row in evidence],
                             [("1.85", False), ("1.95", True)])
            self.assertEqual(len(self.referrals()), 2)

    def test_sender_configuration_offline(self):
        self.assertEqual(sender_identities({"BRENNER": SUPPLIER})[0]["id"], "SUPPLIER_BRENNER")
        for senders in ({"BRENNER": BUYER}, {"OTHER": SUPPLIER}, {"BRENNER": "spoof@example.org"},
                        {"BRENNER": SUPPLIER, "KEMIX": SUPPLIER}):
            with self.assertRaises(ValueError):
                sender_identities(senders)
