"""Offline CLI checks using temporary stores and the actual Frappe REST client."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import generate
from test_engine_goals import Native
from norvale.loader.erp_client import ErpClient


class FrappeFixture:
    """Local HTTP-boundary double. Each create persists before replying."""

    def __init__(self, path):
        self.path = path
        self.posts = 0
        self.fail_read = False
        self.lose_reply = False
        self.omit_remarks = False
        self.user = "sales@norvale.test"

    def rows(self):
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def open(self, request, timeout):
        assert request.get_header("Authorization") == "token test-only"
        assert request.get_header("X-frappe-site-name") == "isolated.test"
        url = urlsplit(request.full_url)
        rows = self.rows()
        if url.path == "/api/method/frappe.auth.get_logged_user":
            return io.BytesIO(json.dumps({"message": self.user}).encode())
        if url.path == "/api/resource/Sales%20Order":
            if request.method == "POST":
                self.posts += 1
                row = json.loads(request.data)
                if self.omit_remarks:
                    row.pop("remarks", None)
                    row["items"][0]["qty"] = float(row["items"][0]["qty"])
                assert "docstatus" not in row
                row.update(name=f"SO-{len(rows) + 1}", owner=self.user, docstatus=0)
                self.path.write_text(json.dumps([*rows, row]))
                if self.lose_reply:
                    self.lose_reply = False
                    raise TimeoutError("committed before POST reply")
                response = row
            else:
                filters = json.loads(parse_qs(url.query)["filters"][0])
                prefix = filters["custom_norvale_operation"][1].removesuffix("%")
                after = filters.get("name", [None, ""])[1]
                response = [{"name": row["name"]} for row in rows if row["name"] > after
                            and row.get("custom_norvale_operation", "").startswith(prefix)]
        else:
            assert request.method == "GET" and url.path.startswith("/api/resource/Sales%20Order/")
            if self.fail_read:
                self.fail_read = False
                raise TimeoutError("committed, request interrupted before readback")
            response = next(row for row in rows if row["name"] == url.path.rsplit("/", 1)[-1])
        return io.BytesIO(json.dumps({"data": response}).encode())


class CLI(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.plan_path = self.root / "plan.json"
        self.run_path = self.root / "run"
        self.service = FrappeFixture(self.root / "erp.json")

        def client(url, site, token):
            api = ErpClient(url, site, token)
            api.opener = self.service
            return api

        # Import the native command (generate adds native/ to sys.path).
        self.enterContext(patch("native.incremental_erp.ErpClient", side_effect=client))
        self.enterContext(patch.dict(os.environ, {"NORVALE_INCREMENTAL_SALES_TOKEN": "test-only"}))
        self.enterContext(patch("urllib.request.OpenerDirector.open",
                                side_effect=AssertionError("offline test forbids network")))

    def cli(self, cutoff, plan=None, *, factory=None, directory=None):
        directory = directory or self.run_path
        args = ["incremental", "--run", str(directory), "--until", str(cutoff)]
        if plan is not None:
            self.plan_path.write_text(json.dumps(plan))
            args += ["--plan", str(self.plan_path)]
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            generate.main(args, transport_factory=factory)
        return json.loads(output.getvalue())["records"]

    def sales_plan(self):
        return {"consumer": "norvale", "seed": 41, "config": {},
                "erp": {"url": "http://isolated.test", "site": "isolated.test",
                        "company": "Norvale Consumer Care BV", "customer": "Demo Retail",
                        "item": "SKU-1", "warehouse": "Finished Goods - NCC"},
                "events": [{"id": "retail-order", "kind": "order", "at": 540, "role": "sales",
                            "duration": 10, "payload": {"case": "PO-812", "quantity": 8, "rate": 2}}]}

    def test_invalid_final_delivery_date_rejects_whole_plan(self):
        from datetime import date
        from native.incremental_erp import EPOCH
        plan = self.sales_plan()
        plan["events"].append({**plan["events"][0], "id": "later",
                               "at": (date.max - EPOCH).days * 1440})
        with self.assertRaises(SystemExit):
            self.cli(10**12, plan)
        self.assertEqual(self.service.posts, 0)
        self.assertFalse(self.run_path.exists())

    def test_erp_redirect_origin_boundary(self):
        from urllib.request import Request
        from norvale.loader.erp_client import SameOriginRedirect, ErpError
        client = ErpClient("https://erp.test", "fixture")
        handler = next(h for h in client.opener.handlers if isinstance(h, SameOriginRedirect))
        request = Request("https://erp.test/start")
        for target in ("http://erp.test/end", "https://elsewhere.test/end", "https://erp.test:444/end"):
            with self.subTest(target=target), self.assertRaises(ErpError):
                handler.redirect_request(request, None, 302, "", {}, target)
        allowed = handler.redirect_request(request, None, 302, "", {}, "https://erp.test:443/end")
        self.assertEqual(allowed.full_url, "https://erp.test:443/end")

    def test_catalog_item_survives_native_readback(self):
        from native.incremental_erp import sales_transport
        from consumers.norvale import Consumer
        from types import SimpleNamespace
        adapter = sales_transport("1" * 32, self.sales_plan())
        def write(system, kind, fields):
            self.assertEqual(system, "east")
            key = adapter.send("erp", "write-1-1", {**fields, "kind": kind, "at": 15})
            return adapter.read("erp", key)
        runtime = SimpleNamespace(event={"role": "buyer"}, catalog={
            "products": {"lotion": {"system": "east", "item": "SKU-2"}}, "roles": {}}, write=write)
        payload = dict(case="request", quantity=2, rate=3, product="lotion")
        with self.assertRaises(ValueError):
            Consumer({}).handle(runtime, "order", payload)
        self.assertEqual(self.service.posts, 0)
        runtime.catalog["roles"]["buyer"] = {"permissions": ["write:east", "observe:east"]}
        Consumer({}).handle(runtime, "order", payload)
        self.assertEqual(adapter.read("erp", "SO-1")["item"], "SKU-2")
        rows = self.service.rows()
        rows[0]["items"][0]["item_code"] = "SKU-1"
        self.service.path.write_text(json.dumps(rows))
        with self.assertRaisesRegex(RuntimeError, "native item"):
            adapter.read("erp", "SO-1")

    def test_employee_profile_requires_the_enrolled_account_and_document_owner(self):
        from native.incremental_erp import sales_transport
        employee = {"role": "desk", "user": "counter@warehouse.test"}
        adapter = sales_transport("a" * 32, self.sales_plan(), employee=employee)
        doc = dict(kind="order", at=30, case="PO", quantity=1, rate=5, order_role="desk")
        with self.assertRaisesRegex(ValueError, "authenticate"):
            adapter.send("erp", "write-1-1", doc)
        self.assertEqual(self.service.posts, 0)
        self.service.user = employee["user"]
        with self.assertRaises(PermissionError):
            adapter.send("erp", "write-1-1", dict(doc, order_role="other"))
        self.assertEqual(self.service.posts, 0)
        name = adapter.send("erp", "write-1-1", doc)
        self.assertEqual(adapter.read("erp", name)["owner"], employee["user"])
        row, = self.service.rows()
        self.service.path.write_text(json.dumps([{**row, "owner": "other@warehouse.test"}]))
        with self.assertRaisesRegex(RuntimeError, "readback"):
            adapter.read("erp", name)

    def test_extend_two_native_instances_without_replacing_run(self):
        plan = self.sales_plan()
        self.assertEqual(self.cli(5, plan), [])
        manifest = (self.run_path / "incremental.json").read_bytes()
        east = FrappeFixture(self.root / "east.json")
        def client(url, site, token):
            instance = ErpClient(url, site, token)
            instance.opener = east if url == "http://east.test" else self.service
            return instance
        registry = self.root / "host.json"
        registry.write_text(json.dumps({"east-sales": {
            "adapter": "erpnext-sales", "token_env": "TEST_EAST_TOKEN",
            "employee": {"role": "east-salesperson", "user": east.user},
            "erp": {**plan["erp"], "url": "http://east.test"}}}))
        east.omit_remarks = True
        extension = self.root / "addition.json"
        extension.write_text(json.dumps({
            "id": "open-east", "systems": {"east": {"connection": "east-sales"}},
            "roles": {"east-salesperson": {"shifts": [[0, 1440]], "permissions": ["write:east", "observe:east"]}},
            "products": {"lotion": {"system": "east", "item": "LOTION-2"}},
            "events": [{"id": "east-order", "kind": "order", "role": "east-salesperson", "at": 20,
                        "payload": {"product": "lotion", "case": "PO-E", "quantity": 2, "rate": 5}}]}))
        with patch("native.incremental_erp.ErpClient", side_effect=client), patch.dict(
                os.environ, NORVALE_CONNECTIONS_FILE=str(registry), TEST_EAST_TOKEN="test-only"):
            args = ["incremental", "--run", str(self.run_path), "--extend", str(extension), "--until", "20"]
            with redirect_stdout(io.StringIO()):
                generate.main(args)
                generate.main(args)
            from engine import Engine
            reopened = []
            def capture(*args, **kwargs):
                engine = Engine(*args, **kwargs)
                reopened.append(engine)
                return engine
            with patch("incremental.Engine", side_effect=capture):
                rows = self.cli(550)
            self.assertEqual([r["case"] for r in reopened[-1].observe("east-salesperson", as_of=550)],
                             ["PO-E"])
            self.assertEqual(reopened[-1].observe("east-salesperson", as_of=19), [])
        self.assertEqual(self.service.posts, 1)
        self.assertEqual(east.posts, 1)
        self.assertEqual([(r["system"], r["native_id"], r["case"]) for r in rows],
                         [("east", "SO-1", "PO-E"), ("erp", "SO-1", "PO-812")])
        self.assertEqual(rows[0]["item"], "LOTION-2")
        self.assertEqual(rows[0]["roles"], ["east-salesperson"])
        self.assertEqual((self.run_path / "incremental.json").read_bytes(), manifest)

    def test_legacy_alias_replays_but_cannot_authorize_new_orders(self):
        plan = self.sales_plan()
        self.cli(0, plan)
        registry = self.root / "registry.json"
        registry.write_text(json.dumps({"secondary": {
            "adapter": "erpnext-sales", "token_env": "NORVALE_INCREMENTAL_SALES_TOKEN",
            "erp": plan["erp"]}}))
        command = {"id": "old-extension", "systems": {"secondary": {"connection": "secondary"}},
                   "products": {"lotion": {"system": "secondary", "item": "LOTION"}},
                   "roles": {"desk": {"shifts": [[0, 1440]], "permissions": ["write:secondary"]}},
                   "events": [{"id": "old-order", "kind": "order", "role": "desk", "at": 10,
                               "payload": {"product": "lotion", "case": "OLD", "quantity": 1, "rate": 3}}]}
        extension = self.root / "extension.json"
        extension.write_text(json.dumps(command))
        args = ["incremental", "--run", str(self.run_path), "--extend", str(extension), "--until"]
        with patch.dict(os.environ, NORVALE_CONNECTIONS_FILE=str(registry)), redirect_stdout(io.StringIO()):
            # Seed the historical unbound descriptor and queued role using the old admission rule.
            with patch("native.software_connections.sales_binding", return_value={}), patch(
                    "native.software_connections.validate_sales_roles"):
                generate.main([*args, "0"])
            self.service.omit_remarks = True
            self.service.fail_read = True
            with self.assertRaises(SystemExit):
                generate.main([*args, "10"])
            rows = self.service.rows()
            original = rows[0]["custom_norvale_operation"]
            rows[0]["custom_norvale_operation"] = original[:-32] + "0" * 32
            self.service.path.write_text(json.dumps(rows))
            with self.assertRaises(SystemExit):
                generate.main([*args, "10"])
            rows[0]["custom_norvale_operation"] = original
            rows[0]["items"][0]["qty"] = True
            self.service.path.write_text(json.dumps(rows))
            with self.assertRaises(SystemExit):
                generate.main([*args, "10"])
            rows[0]["items"][0]["qty"] = 1.0
            self.service.path.write_text(json.dumps(rows))
            generate.main([*args, "10"])
            self.assertEqual(self.service.posts, 1)
            self.assertNotIn("remarks", self.service.rows()[0])
            event = {**command["events"][0], "id": "second-order", "at": 11}
            extension.write_text(json.dumps({"id": "new-extension", "events": [event]}))
            with self.assertRaises(SystemExit):
                generate.main([*args, "11"])
            from engine.__main__ import main as offline_command
            with self.assertRaises(SystemExit):
                offline_command(["--run", str(self.run_path), "--extend", str(extension)])
            self.cli(11)
            self.assertEqual(self.service.posts, 1)

    def test_legacy_help_and_incremental_help(self):
        for args, option in (([], "--provision"), (["incremental"], "--plan")):
            output = subprocess.check_output([sys.executable, "generate.py", *args, "--help"], text=True)
            self.assertIn(option, output)

    def test_native_orders_resume_after_readback_failure_and_isolate_runs(self):
        self.assertEqual(self.cli(545, self.sales_plan()), [])
        self.service.fail_read = True
        with self.assertRaises(SystemExit):
            self.cli(550)
        self.assertEqual(self.service.posts, 1)

        # The same pending write must verify actual stored business fields.
        rows = self.service.rows()
        rows[0]["items"][0]["qty"] = 9
        self.service.path.write_text(json.dumps(rows))
        with self.assertRaises(SystemExit):
            self.cli(550)
        rows[0]["items"][0]["qty"] = 8
        self.service.path.write_text(json.dumps(rows))
        first, = self.cli(550)
        self.assertEqual(self.service.posts, 1)
        self.assertEqual(first, {"kind": "order", "at": 550, "native_id": "SO-1", "system": "erp",
                                 "case": "PO-812", "quantity": 8, "rate": 2, "roles": ["sales"],
                                 "source_url": "http://isolated.test/app/sales-order/SO-1"})
        self.assertEqual(self.cli(551), [first])
        with self.assertRaises(SystemExit):
            self.cli(551, self.sales_plan())

        self.service.lose_reply = True
        second, = self.cli(550, self.sales_plan(), directory=self.root / "second")
        self.assertNotEqual(second["native_id"], first["native_id"])
        self.assertEqual(self.service.posts, 2)
        from native.incremental_erp import sales_transport
        manifest = json.loads((self.run_path / "incremental.json").read_text())
        adapter = sales_transport(manifest["namespace"], manifest["plan"])
        self.assertEqual(list(adapter.records()), [first])
        with self.assertRaises(RuntimeError):
            adapter.read("erp", second["native_id"])
        from consumers.norvale import Consumer
        from engine import Engine
        from time import monotonic
        checkpoint = Engine(self.run_path, Consumer({"_incremental_identity": manifest}),
                            seed=41, native=adapter, providers=None, monotonic=monotonic)
        self.assertEqual(checkpoint.observe("sales", as_of=550), [first])
        self.assertEqual(checkpoint.observe("sales", as_of=549), [])
        self.assertEqual(checkpoint.observe("warehouse", as_of=550), [])

    def test_native_wrong_identity_and_unsupported_consumer_fail_closed(self):
        self.service.user = "Administrator"
        with self.assertRaises(SystemExit):
            self.cli(550, self.sales_plan())
        unsupported = {**self.sales_plan(), "consumer": "managed_services"}
        with self.assertRaises(SystemExit):
            self.cli(550, unsupported, directory=self.root / "unsupported")
        self.assertFalse((self.root / "unsupported").exists())
        production = self.sales_plan()
        production["events"][0]["kind"] = "production"
        with self.assertRaises(SystemExit):
            self.cli(550, production, directory=self.root / "unsupported")
        self.assertFalse((self.root / "unsupported").exists())
        self.assertEqual(self.service.posts, 0)

    def test_managed_services_resumes_with_persistent_transport_double(self):
        def transport(namespace, plan):
            return Native(self.root / (namespace + "-double.json"))

        plan = {"consumer": "managed_services", "seed": 41, "config": {},
                "events": [
                    {"id": "contract", "kind": "contract", "at": 540,
                     "payload": {"case": "MS-21", "response_minutes": 60, "rate": 95}},
                    {"id": "issue", "kind": "ticket", "at": 555,
                     "payload": {"case": "MS-21", "ticket": "INC-412"}},
                    {"id": "reply", "kind": "respond", "at": 560, "duration": 5,
                     "payload": {"case": "MS-21", "ticket": "INC-412"}},
                ]}
        contract, issue = self.cli(562, plan, factory=transport)
        self.assertEqual(issue["contract_id"], contract["native_id"])
        with self.assertRaises(SystemExit):
            self.cli(565)
        resumed = self.cli(565, factory=transport)
        self.assertEqual(resumed[:2], [contract, issue])
        self.assertEqual(resumed[2]["ticket_id"], issue["native_id"])
        self.assertEqual(resumed[2]["at"], 565)
        self.assertEqual(self.cli(580, factory=transport), resumed)
        self.assertFalse(self.service.path.exists())


if __name__ == "__main__":
    unittest.main()
