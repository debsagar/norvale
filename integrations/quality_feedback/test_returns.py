import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from integrations.quality_feedback.returns import receive
from native.company import Engine
from native.company_erp import CompanyERP


class ERP:
    def __init__(self):
        self.notes = {"DN": dict(name="DN", doctype="Delivery Note", docstatus=1, company="Company",
            customer="Customer", currency="EUR", conversion_rate=1, posting_date="2026-01-01",
            custom_norvale_quality_releases=json.dumps([
                {"batch":"LOT", "disposition":{"id":"QI", "qms_source":"qa"}}
            ]),
            items=[dict(name="ROW", item_code="Item", stock_qty=4, conversion_factor=1,
                        stock_uom="Nos", warehouse="Shipping", rate=1, serial_and_batch_bundle="bundle:DN")])}
        self.bundle = dict(name="bundle:DN", docstatus=1, item_code="Item", warehouse="Shipping",
                           voucher_type="Delivery Note", voucher_no="DN", voucher_detail_no="ROW",
                           type_of_transaction="Outward", entries=[dict(batch_no="LOT", qty=-4)])
        self.lose = None

    def get(self, doctype, name):
        if doctype == "Serial and Batch Bundle":
            return self.bundle
        if doctype == "UOM":
            return {"must_be_whole_number": name == "Nos"}
        return self.notes[name]

    def list(self, doctype, filters, fields=None, order_by=None):
        if doctype == "Custom Field":
            return [{"name":"unique"}]
        cursor = filters.get("name", [">", ""])[1]
        query = {k:v for k,v in filters.items() if k != "name"}
        return [{"name":key} for key, doc in sorted(self.notes.items()) if key > cursor
                and all(doc.get(k) == v for k,v in query.items())][:1]

    def create(self, doctype, doc):
        name = "RETURN-" + str(len(self.notes))
        row = doc["items"][0]
        row.update(name=name + "-ROW", stock_qty=row["qty"])
        doc.update(name=name, doctype=doctype, docstatus=0)
        self.notes[name] = doc
        if self.lose == "POST":
            self.lose = None
            raise TimeoutError("lost POST response")
        return doc

    def update(self, doctype, name, fields):
        self.notes[name].update(fields)
        self.notes[name]["items"][0]["serial_and_batch_bundle"] = "bundle:" + name
        if self.lose == "PUT":
            self.lose = None
            raise TimeoutError("lost PUT response")
        return self.notes[name]


def checkpoint_engine(path, api):
    e = Engine.__new__(Engine)
    e.run, e.day, e.event = path, 1, "return"
    e.date = lambda: "2026-01-10"
    e.company, e.wh = "Company", {"hold":"Quarantine"}
    e.admin, e.api = api, {"WAREHOUSE":api}
    saved = path / "state.json"
    e.state = json.loads(saved.read_text()) if saved.exists() else {"docs":{}}
    e.docs = e.state["docs"]
    flow = e.company_erp = CompanyERP(e, None)
    flow.state["shipments"]["ship"] = {"target_id":"DN", "departed_day":0}
    # The live proof exercises ERP's stock ledger and bundle endpoints.
    flow.verify_row = lambda doc, row, batch, qty, *_: (
        {"name":"ledger:" + doc["name"],"actual_qty":qty},
        {"name":row.get("serial_and_batch_bundle"),"voucher_type":"Delivery Note","entries":[{"qty":qty}]},
    )
    return e


class ReturnReplayTest(unittest.TestCase):
    def test_partial_receipts_and_lost_replies(self):
        api = ERP()
        case = dict(custom_delivery_note="DN", custom_delivery_source="erp", custom_delivery_customer="Customer",
                    custom_delivery_row="ROW", custom_qms_batch="LOT")
        release = dict(name="QI", workflow_state="Revoked")
        scope = dict(custom_delivery_batch="LOT", custom_qms_batch="LOT", custom_delivery_qms_source="qa")
        note = api.notes["DN"]
        line, = note["items"]
        receipt = dict(observation="receipt-a", quantity="1", observed_on="2026-01-09",
                       complaint="CASE", delivery="DN", line="ROW", batch="LOT")
        with TemporaryDirectory(dir=os.getenv("TMPDIR", "/data")) as path:
            def run(**overrides):
                return receive(checkpoint_engine(Path(path), api),
                               SimpleNamespace(**{**receipt, **overrides}), case, note, line, release, scope, "erp")
            with self.assertRaisesRegex(ValueError, "native withdrawal"):
                run()
            case.update(custom_withdrawal_release="QI", custom_withdrawal_approver="approver")
            release["workflow_state"] = "Released"
            with self.assertRaisesRegex(ValueError, "Withdrawal evidence"):
                run()
            release["workflow_state"] = "Revoked"
            for edits in (dict(quantity="NaN"),
                          dict(quantity="0"), dict(quantity="1.5"), dict(observation=""),
                          dict(observed_on="2026-01-11"), dict(quantity="5")):
                with self.assertRaises(ValueError):
                    run(**edits)
            self.assertEqual(len(api.notes), 1)
            api.lose = "POST"
            with self.assertRaisesRegex(TimeoutError, "POST"):
                run()
            with self.assertRaisesRegex(ValueError, "pending receipt"):
                run(observation="later")
            with self.assertRaisesRegex(ValueError, "intent changed"):
                run(quantity="2")
            api.lose = "PUT"
            with self.assertRaisesRegex(TimeoutError, "PUT"):
                run()
            first = run()
            self.assertEqual(first, run())
            self.assertEqual(len(api.notes), 2)
            self.assertEqual(api.notes[first["return_note"]]["custom_norvale_operation"],
                             hashlib.sha256(b'customer-return:["erp","receipt-a"]').hexdigest())
            with self.assertRaisesRegex(ValueError, "exceed"):
                run(observation="receipt-b", quantity="4")
            second = run(observation="receipt-b", quantity="3")
            self.assertNotEqual(first["return_note"], second["return_note"])
            with self.assertRaisesRegex(ValueError, "exceed"):
                run(observation="receipt-c")
            self.assertEqual(len(api.notes), 3)
            api.notes[second["return_note"]]["items"][0]["warehouse"] = "Saleable"
            with self.assertRaisesRegex(ValueError, "differs from observed"):
                run(observation="receipt-b", quantity="3")

    def test_fractional_stock_uom_and_native_manual_return_count(self):
        api = ERP()
        api.notes["DN"]["items"][0]["stock_uom"] = "Kg"
        api.notes["MANUAL"] = dict(name="MANUAL", return_against="DN", docstatus=1, is_return=1,
                                 items=[dict(dn_detail="ROW", item_code="Item", stock_qty=-3.75)])
        case = dict(custom_delivery_note="DN", custom_delivery_source="erp", custom_delivery_customer="Customer",
                    custom_delivery_row="ROW", custom_qms_batch="LOT", custom_withdrawal_release="QI",
                    custom_withdrawal_approver="approver")
        release = dict(name="QI", workflow_state="Revoked")
        scope = dict(custom_delivery_batch="LOT", custom_qms_batch="LOT", custom_delivery_qms_source="qa")
        note = api.notes["DN"]
        line, = note["items"]
        receipt = SimpleNamespace(observation="receipt-kg", quantity="0.5", observed_on="2026-01-09",
                                  complaint="CASE", delivery="DN", line="ROW", batch="LOT")
        with TemporaryDirectory(dir=os.getenv("TMPDIR", "/data")) as path:
            e = checkpoint_engine(Path(path), api)
            with self.assertRaisesRegex(ValueError, "exceed"):
                receive(e, receipt, case, note, line, release, scope, "erp")
            receipt.quantity = "0.25"
            result = receive(e, receipt, case, note, line, release, scope, "erp")
            self.assertEqual(api.notes[result["return_note"]]["items"][0]["stock_qty"], -0.25)


if __name__ == "__main__":
    unittest.main()
