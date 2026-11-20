from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
import json
from adapter import Lake
from boundary import trace
from mes_reader import scan, scan_staging


class MesData(TestCase):
    def test_native_movements_preserved_on_restart(self):
        def ref(model, key):
            return dict(system="mes", source_id="plant", entity=model, native_id=key)
        def move(key, qty, unit):
            return dict(record=ref("stock.move.line", key), lot=ref("stock.lot", key+20),
                        product=ref("product.product", key), lot_name=str(key), item_code=str(key),
                        quantity=qty, uom=ref("uom.uom", key), uom_name=unit, private="omit")
        report = dict(work=ref("mrp.production",1), recorded_at_utc="2027-01-08T15:00:00",
                      erp_work_ref="WO-11", revision="v3", bom=ref("mrp.bom",2),
                      completion_ref="plant:mrp.production:1", inputs=[move(1,0.75,"kg")],
                      output=move(2,3,"ea"), work_orders=[ref("mrp.workorder",4)],
                      output_hold_location=ref("stock.location",5), backorder=ref("mrp.production",6))
        class Reader:
            source_id = "plant"
            def call(self, model, method, domain, **kwargs):
                if method == "fields_get":
                    return {"norvale_receiver_marker": {"type": "char"}} if model == "mrp.bom" else {}
                if model == "mrp.bom":
                    return [{"id": 2, "norvale_receiver_marker": "a" * 64}]
                if method == "read":
                    return [{"company_id": [1, "Company"], "norvale_rework_of": False}]
                return [1]
            def completion(self, identifier):
                return report
        with TemporaryDirectory() as directory:
            self.assertEqual(scan(directory,Reader(),["WO-11"]),1)
            self.assertEqual(scan(directory,Reader(),["WO-11"]),0)
            stored, = Lake(Path(directory)/"delta",{})._rows()
            doc = json.loads(stored["payload"])
            self.assertNotIn("private",stored["payload"])
            self.assertEqual(doc["receiver_marker"], "a" * 64)
            self.assertIn(("mrp.production","1","mrp.production","6"),trace({"mrp.production":[doc]}))
            self.assertEqual((doc["inputs"][0]["quantity"],doc["output"]["unit"]), (0.75,"ea"))
            self.assertIn(("stock.move.line","2","stock.lot","22"), trace({"mrp.production":[doc]}))
    def test_allocated_inputs_on_two_sites_with_colliding_native_ids(self):
        company = [1, "Company"]
        component = [5, "Bottle"]
        stock = [2, "Stock"]
        common = dict(state="done", company_id=company, product_id=component,
                      location_id=[3, "Adjustment"], location_dest_id=stock)
        move = dict(common, id=1, norvale_staging_order_id=[1, "MO"],
                    norvale_staging_receipt="NV-STAGE-abc", origin="NV-STAGE-abc",
                    product_uom_qty=2, quantity=2, is_inventory=True, product_uom=[9, "Units"],
                    move_line_ids=[4], write_date="2027-01-10")
        records = {
            ("mrp.production", 1): dict(id=1, origin="ERP-WO-1", company_id=company,
                                       product_id=[6, "Finished"], location_src_id=stock, move_raw_ids=[8]),
            ("stock.move.line", 4): dict(common, id=4, owner_id=False, package_id=False, result_package_id=False, move_id=[1, "Receipt"], quantity=2,
                                        lot_id=[7, "A"], product_uom_id=[9, "Units"]),
            ("stock.lot", 7): dict(id=7, name="A", company_id=company, product_id=component),
        }

        records.update({
            ("stock.move", 1): move,
            ("stock.location", 3): dict(id=3, usage="inventory", company_id=False),
            ("stock.location", 2): dict(id=2, usage="internal", company_id=company),
            ("stock.move", 8): dict(id=8, product_id=component),
            ("product.product", 5): dict(id=5, uom_id=[9, "Units"], property_stock_inventory=[3, "Adjustment"]),
        })

        class Reader:
            def call(self, model, method, domain, **kwargs):
                if method == "fields_get":
                    return {"norvale_staging_order_id": {}}
                if method == "read":
                    return [{key: records[model, identifier][key] for key in ["id", *kwargs["fields"]]}
                            for identifier in domain]
                assert ["norvale_staging_order_id.product_id", "=", 6] in domain
                assert ["company_id", "=", 1] in domain
                return [{key: move[key] for key in ["id", *kwargs["fields"]]}]

            def read(self, model, identifier):
                return records[model, identifier]

        with TemporaryDirectory() as directory:
            reader = Reader()
            for reader.source_id in ("east", "west"):
                self.assertEqual(scan_staging(directory, reader, product=6, company=1), 1)
                self.assertEqual(scan_staging(directory, reader, product=6, company=1), 0)
            rows = Lake(Path(directory)/"delta", {})._rows()
            self.assertEqual({r["source"] for r in rows}, {"east", "west"})
            docs = [json.loads(r["payload"]) for r in rows]
            self.assertEqual(sum(doc["quantity"] for doc in docs), 4)
            self.assertIn(("stock.move", "1", "mrp.production", "1"), trace({"stock.move": docs}))
            for native, field, invalid in [
                (("stock.location", 3), "usage", "internal"),
                (("product.product", 5), "property_stock_inventory", [7, "Other adjustment"]),
                (("stock.move.line", 4), "quantity", float("nan")),
                (("stock.move.line", 4), "quantity", 1),
                (("stock.move", 1), "quantity", 1),
                (("stock.move", 1), "product_uom_qty", 3),
                (("stock.move", 1), "product_uom", [10, "Box"]),
                (("stock.move", 1), "is_inventory", False),
                (("stock.move", 8), "product_id", [42, "Other input"]),
                (("stock.move.line", 4), "owner_id", [8, "Consignor"]),
                (("stock.move.line", 4), "result_package_id", [8, "Package"]),
                (("stock.move.line", 4), "company_id", [8, "Other company"]),
            ]:
                with self.subTest(field=field):
                    row = records[native]
                    good = row[field]
                    row[field] = invalid
                    with self.assertRaisesRegex(ValueError, "manufacturing order"):
                        scan_staging(directory, reader, product=6, company=1)
                    row[field] = good
            records["mrp.production", 1]["product_id"] = [99, "Other"]
            with self.assertRaisesRegex(ValueError, "manufacturing order"):
                scan_staging(directory, reader, product=6, company=1)

if __name__ == "__main__":
    main()
