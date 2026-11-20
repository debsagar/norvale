import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from simulate import ROOT, load_scenario


class ScenarioContract(unittest.TestCase):
    def test_native_q1_plan_and_invalid_opening_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "company"
            output = subprocess.check_output([sys.executable, ROOT / "simulate.py",
                                              "--run", run, "--plan"], text=True)
            plan = json.loads(output)
            self.assertFalse(run.exists())
            cfg = plan["scenario"]
            self.assertEqual(cfg["days"], 90)
            self.assertEqual(sum(order["quantity"] for order in cfg["opening_orders"]), 720)
            cfg["opening_orders"][0]["item"] = "NO-SUCH-SKU"
            bad = Path(tmp) / "invalid.json"
            bad.write_text(json.dumps(cfg))
            with self.assertRaisesRegex(ValueError, "Unknown customer or item"):
                load_scenario(bad)

    def test_opening_order_recovers_before_planning_and_rejects_changed_readback(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock
        sys.path.insert(0, str(ROOT / "native"))
        from company import Engine

        order = {"customer":"CUST-5001", "item":"SKU-30001", "quantity":240, "due_day":2}
        saved = {}
        e = SimpleNamespace(config={"opening_orders":[order]}, catalog=SimpleNamespace(
            skus=[{"sku_id":order["item"],"unit_price":3}]), state={"ops":{"orders":{}}},
            day=0, company="Norvale", wh={"fg":"Finished"}, get=lambda _:{"name":"Buyer"},
            date=lambda:"2027-01-01", dated=lambda n:f"2027-01-{n+1:02d}", persist=Mock())

        def record(key, doctype, fields, role, submit):
            self.assertEqual((doctype,role,submit), ("Sales Order","SALES",True))
            return saved.setdefault(key, dict(fields, docstatus=1))

        e.write = record
        Engine.seed_opening_orders(e)
        Engine.seed_opening_orders(e)
        self.assertEqual(list(saved),["opening-order-1"])
        self.assertEqual(e.state["ops"]["orders"]["opening-order-1"]["qty"],240)
        saved["opening-order-1"]["items"][0]["qty"] = 241
        with self.assertRaisesRegex(ValueError,"readback differs"):
            Engine.seed_opening_orders(e)
