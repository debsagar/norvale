"""Cutoff, identity and evidence controls around the Milestone 6 seam."""

from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest

from consumers.norvale import Consumer
from engine import Engine
from native.build_evidence import sales_facts
from test_engine_goals import Native, PowerLoss, Providers


class EvidenceConsumer(Consumer):
    """Write controlled readbacks through the real journal and checkpoint."""

    def handle(self, engine, kind, payload):
        for inspect in (engine.private_truth, engine.extractor_questions):
            try:
                inspect()
            except PermissionError:
                continue
            raise AssertionError("Handler accessed the evaluator")

        if kind == "approval_request":
            return super().handle(engine, kind, payload)
        engine.write(payload["instance"], kind, payload["fields"])


class ScopedNative(Native):
    def send(self, system, retry_id, document):
        rows = self.records()
        native_id = document.get("target", retry_id)
        key = system, document["kind"], native_id
        self.path.write_text(json.dumps([
            *[row for row in rows if (row["system"], row["kind"], row["native_id"]) != key],
            {**document, "system": system, "native_id": native_id, "retry_id": retry_id},
        ]))
        return native_id


class Extraction(unittest.TestCase):
    def test_cutoff_versions_scope_incomplete_evidence_and_interrupted_confirmation(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        native = ScopedNative(root / "records.json")
        providers = Providers()

        def open_run(boundary=None):
            return Engine(root / "run", EvidenceConsumer({}), seed=0, native=native,
                          providers=providers, monotonic=monotonic, boundary=boundary)

        run = open_run()

        def add(key, at, kind, instance="east", case="blue", target="D", **fields):
            run.schedule(key, kind, at, role="finance", payload={
                "instance": instance, "fields": dict(case=case, target=target, **fields),
            })

        add("ship", 1, "shipment", quantity=12.34)
        add("wrong-scope", 1, "shipment", "west", "red", quantity=99)
        add("bill", 2, "invoice", target="I", shipment_id="D", amount=0.30,
            currency="EUR", outstanding=0.30)
        add("bill-other", 2, "invoice", "west", "red", "I", shipment_id="missing",
            amount=25, currency="USD")
        add("pay-one", 3, "payment", target="P", invoice_id="I", amount=0.10,
            currency="EUR", outstanding=0.20, roles=["finance"])
        add("other-pay", 3, "payment", "west", "red", "P", invoice_id="I",
            amount=11, currency="USD")
        add("wrong-join", 3, "payment", "west", "red", "Q", invoice_id="I",
            invoice_system="east", amount=25, currency="USD")
        add("precise-bill", 3, "invoice", "east", "precise", "I2",
            amount="9007199254740992.01", currency="EUR", shipment_id="unknown")
        # Correct the unrelated payment's foreign reference before grading the
        # final cutoff. At the earlier cutoff the conflicting evidence is unknown.
        add("correct-join", 4, "payment", "west", "red", "Q", invoice_id="unknown",
            amount=25, currency="USD")
        add("bill-revised", 4, "invoice", target="I", shipment_id="D", amount=0.30,
            currency="EUR", outstanding=0.10)
        add("pay-one-revised", 4, "payment", target="P", invoice_id="I", amount=0.10,
            currency="EUR", outstanding=0, roles=["finance"])
        add("pay-two", 5, "payment", target="P2", invoice_id="I", amount=0.20,
            currency="EUR", outstanding=0)

        def interrupt(stage, operation):
            if stage == "result_persisted" and operation["event"]["id"] == "pay-two":
                raise PowerLoss()

        run = open_run(interrupt)
        with self.assertRaises(PowerLoss):
            run.run(until=5)

        run = open_run()
        self.assertEqual(run.private_truth()["answers"], {
            "blue-invoiced": 0.30, "blue-billed-units": 12.34, "blue-paid": 0.10,
            "red-invoiced": 25, "red-billed-units": None, "red-paid": 11,
            "precise-invoiced": "9007199254740992.01", "precise-billed-units": None,
            "precise-paid": None,
        })
        self.assertIsNone(run.private_truth(as_of=3)["answers"]["blue-paid"])
        self.assertIsNone(run.private_truth(as_of=2)["answers"]["blue-paid"])
        self.assertEqual(run.private_truth(as_of=1)["answers"], {})
        self.assertTrue(all(set(q) == {"id", "question"} for q in run.extractor_questions()))
        self.assertNotIn("answers", json.dumps(run.export()))
        self.assertNotIn("&quot;answers&quot;", run.viewer())
        self.assertNotIn("answers", json.dumps(run.observe("finance", as_of=4)))
        with self.assertRaises(ValueError):
            run.extractor_questions(as_of=6)

        run.run(until=5)
        self.assertEqual(open_run().private_truth()["answers"]["blue-paid"], 0.30)
        self.assertEqual(open_run().private_truth(as_of=4)["answers"]["blue-paid"], 0.10)
        run.schedule("approval", "approval_request", 6, role="manager", payload={
            "case": "approval", "quote": "terms", "amount": 1, "open_commitments": 0,
        })
        run.run(until=6)
        self.assertTrue(providers.calls)
        for _, prompt, _ in providers.calls:
            self.assertNotIn("answers", prompt)
            self.assertNotIn("blue-paid", prompt)
            observations = json.loads(prompt)["observations"]
            self.assertFalse(any(row["system"] == "east" and row["native_id"] == "P"
                                 for row in observations))

    def test_frozen_norvale_native_links_and_missing_payment_evidence(self):
        root = Path(os.environ.get("NORVALE_RELEASE_DATA", Path(__file__).resolve().parents[1] / "data"))
        manifest = json.loads((root / "manifest.json").read_text())
        with (root / "observable" / "native-records.json").open() as stream:
            docs = json.load(stream, parse_float=Decimal)
        consumer = Consumer({})
        cutoff = manifest["business_period"][1]
        questions, answers = consumer.grade_extraction(sales_facts(docs), cutoff)
        case = "SAL-ORD-2027-00051"
        # Independently reconstruct this split order from the unprojected documents.
        invoices = [doc for doc in docs["Sales Invoice"].values()
                    if doc["items"][0]["sales_order"] == case]
        invoice_ids = {doc["name"] for doc in invoices}
        deliveries = [docs["Delivery Note"][doc["items"][0]["delivery_note"]] for doc in invoices]
        paid = sum(ref["allocated_amount"] for doc in docs["Payment Entry"].values()
                   for ref in doc["references"]
                   if ref["reference_doctype"] == "Sales Invoice" and ref["reference_name"] in invoice_ids)
        self.assertEqual(len(invoices), 4)
        self.assertEqual(Decimal(str(answers[case + "-invoiced"])),
                         sum(doc["grand_total"] for doc in invoices))
        self.assertEqual(answers[case + "-billed-units"],
                         sum(doc["items"][0]["stock_qty"] for doc in deliveries))
        self.assertEqual(Decimal(str(answers[case + "-paid"])), paid)
        self.assertTrue(all(set(q) == {"id", "question"} for q in questions))
        self.assertIsNone(answers["SAL-ORD-2027-00238-paid"])
        _, alternate = consumer.grade_extraction(sales_facts(docs, system="other"), cutoff)
        self.assertEqual(alternate["other/" + case + "-paid"], answers[case + "-paid"])

        # A forged balance and a lost delivery still cannot create evidence.
        invoices[0]["outstanding_amount"] = 999
        del docs["Delivery Note"][deliveries[0]["name"]]
        _, changed = consumer.grade_extraction(sales_facts(docs), cutoff)
        self.assertIsNone(changed[case + "-billed-units"])
        self.assertEqual(changed[case + "-paid"], answers[case + "-paid"])

        linked_payment = next(doc for doc in docs["Payment Entry"].values()
                              if any(ref["reference_name"] in invoice_ids for ref in doc["references"]))
        linked_payment["paid_from_account_currency"] = "GBP"
        _, changed = consumer.grade_extraction(sales_facts(docs), cutoff)
        self.assertIsNone(changed[case + "-paid"])
