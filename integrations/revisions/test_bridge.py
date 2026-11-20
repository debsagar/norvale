"""Offline regression for publication recovery, destination readback and dispatch."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from integrations.plm import Blocked, PLM

from . import Bridge, RevisionMismatch, project
from .receivers import ERP_KEY, ERP_SITE, ODOO_KEY, ODOO_SITE, ERPNext, Odoo


EARLY = "2027-01-04"
LATER = "2027-02-01"
PRODUCT = "NV-BRIDGE-SHAMPOO"


def components(pack_qty):
    return [
        {"source_id": "NV-BRIDGE-BULK", "kind": "formulation", "quantity": "250",
         "unit": "g", "specification": "Prepared base"},
        {"source_id": "NV-BRIDGE-BOTTLE", "kind": "packaging", "quantity": str(pack_qty),
         "unit": "ea", "specification": "Bottle and closure"},
    ]


def mapping(company, company_id, output_id, bulk_id, bottle_id, kg_id, unit_id, center_id):
    def entries(bulk, bottle, kg, unit):
        return {
            "NV-BRIDGE-BULK": {"source_unit": "g", "kind": "formulation",
                               "item": bulk, "unit": kg, "factor": "0.001"},
            "NV-BRIDGE-BOTTLE": {"source_unit": "ea", "kind": "packaging",
                                 "item": bottle, "unit": unit, "factor": "1"},
        }

    return {PRODUCT: {
        "erp": {"output": {"item": PRODUCT, "unit": "Nos", "qty": "1"},
                "components": entries("NV-BRIDGE-BULK", "NV-BRIDGE-BOTTLE", "Kg", "Nos"),
                "settings": {"company": company}},
        "mes": {"output": {"item": output_id, "unit": unit_id, "qty": "1"},
                "components": entries(bulk_id, bottle_id, kg_id, unit_id),
                "settings": {"company_id": company_id, "operation": {
                    "name": "Fill", "workcenter_id": center_id, "minutes": "1"}}},
    }}


class BulkProjectionTests(unittest.TestCase):
    def test_formulation_only_requires_an_approved_intermediate_definition(self):
        targets = mapping('Norvale', 1, 2, 3, 4, 5, 6, 7)
        targets[PRODUCT]['erp']['output'] = dict(item=PRODUCT, unit='Kg', qty='0.75')
        publication = {'definition': {'name': PRODUCT, 'lines': components(1)[:1]}}
        with self.assertRaises(RevisionMismatch):
            project(publication, targets, 'erp')
        publication['definition']['material_kind'] = 'intermediate'
        plan = project(publication, targets, 'erp')
        self.assertEqual(plan['output']['qty'], '0.75')
        self.assertEqual(len(plan['lines']), 1)


class SourceFixture:
    def __init__(self):
        self.receipts = {}

    def publication(self, pk):
        return {"native_id": pk, "source_version": str(pk) * 64, "approval": {"on": EARLY},
                "definition": {"name": PRODUCT, "lines": components(pk)}}

    def publish_to(self, pk, system, *, on, ensure_target, read_target):
        old = self.receipts.get((pk, system))
        native = old["native_id"] if old else ensure_target(self.publication(pk), "fixture-key")
        observed = read_target(native)
        assert observed == {"native_id": native, "plm_native_id": pk,
                            "plm_source_version": self.publication(pk)["source_version"]}
        self.receipts[pk, system] = old or {**observed, "on": on}
        return self.receipts[pk, system]

    def pin(self, base, work, *, on):
        pk = 2 if on >= LATER else 1
        if not all((pk, system) in self.receipts for system in ("erp", "mes")):
            raise RevisionMismatch("PLM acknowledgements missing")
        return {"native_id": pk, "source_version": str(pk) * 64,
                "released_on": on, "work_id": work,
                "receiver_ids": {system: self.receipts[pk, system]["native_id"]
                                 for system in ("erp", "mes")}}

    def resolve_pin(self, pin, *, work_id, on):
        assert work_id == pin["work_id"] and on >= pin["released_on"]
        return self.publication(pin["native_id"])


class NativeReceiptStore(PLM):
    """Substitute the InvenTree transport while exercising real receipt and pin logic."""

    def __init__(self):
        self.meta = {1: {"norvale_approval": {"on": EARLY, "effective": EARLY}}}

    def _publisher(self):
        pass

    def _family(self, base):
        yield {"pk": base}

    def _meta(self, pk, updates=None):
        if updates is not None:
            self.meta[pk].update(deepcopy(updates))
        return deepcopy(self.meta[pk])

    def publication(self, pk):
        result = SourceFixture().publication(pk)
        result["approval"] = self._meta(pk)["norvale_approval"]
        return result


class FrappeFixture:
    def __init__(self):
        self.boms = {}
        self.fields = {}
        self.drop_create_response = False

    def list(self, doctype, filters, *, limit):
        if doctype == "Custom Field":
            return [row for row in self.fields.values() if row["fieldname"] == filters["fieldname"]]
        return [doc for doc in self.boms.values() if doc[ERP_KEY] == filters[ERP_KEY]][:limit]

    def get(self, doctype, name):
        if doctype == "Custom Field":
            return deepcopy(self.fields[name])
        if doctype == "Company":
            return {"name": name}
        if doctype == "UOM":
            return {"name": name, "enabled": 1, "must_be_whole_number": name == "Nos"}
        if doctype == "Item":
            return {"name": name, "stock_uom": "Kg" if name.endswith("BULK") else "Nos",
                    "is_stock_item": 1, "disabled": 0}
        assert doctype == "BOM"
        return deepcopy(self.boms[name])

    def metadata(self, doctype):
        assert doctype == "BOM"
        return {"fields": list(self.fields.values())}

    def create(self, doctype, doc):
        if doctype == "Custom Field":
            self.fields["BOM-" + doc["fieldname"]] = deepcopy(doc)
            return doc
        assert doctype == "BOM"
        name = f"BOM-{len(self.boms) + 1}"
        self.boms[name] = {"name": name, "docstatus": 0, **deepcopy(doc)}
        if "BOM-" + ERP_SITE in self.fields:
            assert ERP_SITE not in doc
            self.boms[name][ERP_SITE] = self.fields["BOM-" + ERP_SITE]["default"]
        for item in self.boms[name]["items"]:
            item.update(stock_qty=item["qty"], stock_uom=item["uom"])
        if self.drop_create_response:
            self.drop_create_response = False
            raise TimeoutError("fixture: ERPNext committed and response was lost")
        return self.boms[name]

    def call(self, method, args):
        assert method == "frappe.client.submit"
        self.boms[args["doc"]["name"]]["docstatus"] = 1


class MesFixture:
    def __init__(self):
        self.boms, self.lines, self.ops = {}, {}, {}
        self.site = uuid4().hex + uuid4().hex

    def call(self, model, method, *args, **kwargs):
        if method == "fields_get":
            return {ODOO_SITE: {"type": "char"}}
        if method == "search":
            return [pk for pk, bom in self.boms.items() if bom[ODOO_KEY] == args[0][0][2]]
        if method == "create":
            doc = deepcopy(args[0])
            pk = len(self.boms) + 1
            for field, table in (("bom_line_ids", self.lines), ("operation_ids", self.ops)):
                rows = [row[2] for row in doc[field]]
                ids = list(range(len(table) + 1, len(table) + len(rows) + 1))
                if table is self.lines:
                    for row in rows:
                        row["operation_id"] = False
                table.update(zip(ids, rows))
                doc[field] = ids
            self.boms[pk] = {**doc, "active": True, "byproduct_ids": []}
            return pk
        assert method == "read"
        rows = []
        for pk in args[0]:
            row = {
                "mrp.bom": {**self.boms.get(pk, {}), ODOO_SITE: self.site},
                "mrp.bom.line": self.lines.get(pk),
                "mrp.routing.workcenter": self.ops.get(pk),
                "mrp.workcenter": {"company_id": 1, "active": True},
                "uom.uom": {"active": True, "rounding": 0.01 if pk == 4 else 1},
                "product.product": {"product_tmpl_id": pk + 8, "uom_id": 4 if pk == 2 else 5,
                                    "company_id": 1, "active": True, "tracking": "lot",
                                    "is_storable": True},
            }[model]
            rows.append({"id": pk, **{key: [row[key], ""] if key.endswith("_id") and row[key] else row[key]
                                     for key in kwargs["fields"]}})
        return rows

    def acknowledge(self, pk):
        return {"bom_id": pk, "revision": self.boms[pk]["code"] + "@" + str(pk) * 64}

    def dispatch(self, work_id, product_id, bom_id, revision, quantity, hold_location_id):
        assert product_id == 1 and revision == self.acknowledge(bom_id)["revision"]
        return {"origin": work_id, "bom_id": bom_id, "quantity": quantity,
                "hold_location_id": hold_location_id}


class BridgeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "checkpoint.sqlite3"
        self.source, self.erp, self.mes = SourceFixture(), FrappeFixture(), MesFixture()
        self.mapping = mapping("Fixture Company", 1, 1, 2, 3, 4, 5, 6)
        self.bridge = self.reopen()

    def reopen(self, *, scoped=False, erp=None, mes=None, path=None):
        return Bridge(self.source, ERPNext(erp or self.erp), Odoo(mes or self.mes), source="fixture",
                      mapping=self.mapping, checkpoint=path or self.path, scoped_receipts=scoped)

    def test_scoped_replay_and_both_receiver_collisions(self):
        alternate_erp, alternate_mes = FrappeFixture(), MesFixture()
        self.source = NativeReceiptStore()
        for client in (self.erp, alternate_erp):
            receiver = ERPNext(client)
            receiver.provision()
            once = client.metadata("BOM")
            receiver.provision()
            self.assertEqual(once, client.metadata("BOM"))

        bridge = self.reopen(scoped=True)
        self.erp.drop_create_response = True
        with self.assertRaises(TimeoutError):
            bridge.publish(1, on=EARLY)
        result = bridge.publish(1, on=EARLY)
        saved = bridge.release(1, "WO-A", on=EARLY)
        self.assertEqual(result, self.reopen().publish(1, on=LATER))
        markers = saved["pin"]["receiver_markers"]
        stripped = {k: v for k, v in saved["pin"].items() if k != "receiver_markers"}
        with self.assertRaises(Blocked):
            self.reopen().installed(stripped, self.source.publication(1))
        with self.assertRaises(Blocked):
            self.reopen(path=self.path.with_name("stripped.sqlite")).release(
                1, "WO-A", on=EARLY, installed=True, pinned=stripped)
        self.assertEqual(markers, {system: row["receipt"]["receiver_marker"]
                                   for system, row in result.items()})
        self.assertEqual(self.reopen().dispatch("WO-A", on=LATER, quantity=1, hold_location_id=7)["bom_id"], 1)
        alternate_mes.boms = deepcopy(self.mes.boms)
        alternate_mes.lines = deepcopy(self.mes.lines)
        alternate_mes.ops = deepcopy(self.mes.ops)
        alternate_erp.boms = deepcopy(self.erp.boms)
        alternate_erp.boms["BOM-1"][ERP_SITE] = ERPNext(alternate_erp)._receiver_marker()
        for system, options in (("erp", {"erp": alternate_erp}), ("mes", {"mes": alternate_mes})):
            with self.subTest(system=system):
                replacement = self.reopen(**options)
                fresh = self.reopen(path=self.path.with_name(system + ".sqlite"), **options)
                with self.assertRaises(Blocked):
                    replacement.publish(1, on=LATER)
                with self.assertRaisesRegex(RevisionMismatch, "Installed .* receiver identity differs"):
                    fresh.release(1, "WO-B", on=EARLY, installed=True)
                with self.assertRaisesRegex(RevisionMismatch, "Published receiver identity"):
                    replacement.release(1, "WO-C", on=EARLY)
                with self.assertRaisesRegex(RevisionMismatch, "Pinned receiver identity"):
                    replacement.dispatch("WO-A", on=LATER, quantity=1, hold_location_id=7)
        alternate_erp.boms["BOM-1"][ERP_SITE] = markers["erp"]
        with self.assertRaisesRegex(RevisionMismatch, "differs from the native site"):
            self.reopen(erp=alternate_erp).publish(1, on=LATER)

    def test_opt_in_preserves_legacy_receipts_and_pins_without_migration(self):
        self.source = NativeReceiptStore()
        original = self.reopen()
        result = original.publish(1, on=EARLY)
        pin = original.release(1, "WO-A", on=EARLY)
        receipts = deepcopy(self.source.meta)
        scoped = self.reopen(scoped=True)
        self.assertEqual(result, scoped.publish(1, on=LATER))
        self.assertEqual(pin, scoped.release(1, "WO-A", on=EARLY))
        self.assertNotIn("receiver_markers", pin["pin"])
        self.assertEqual(scoped.dispatch("WO-A", on=LATER, quantity=1, hold_location_id=7)["bom_id"], 1)
        self.assertEqual(self.source.meta, receipts)

    def test_opt_in_preserves_partial_legacy_publication(self):
        self.source = NativeReceiptStore()
        call = self.mes.call

        def lose_create(model, method, *args, **kwargs):
            if method == "create":
                raise TimeoutError("MES unavailable")
            return call(model, method, *args, **kwargs)

        with patch.object(self.mes, "call", side_effect=lose_create):
            with self.assertRaises(TimeoutError):
                self.reopen().publish(1, on=EARLY)
        legacy = self.source._meta(1)["norvale_ack_erp"]
        bridge = self.reopen(scoped=True)
        receipts = bridge.publish(1, on=EARLY)
        self.assertEqual(receipts["erp"]["receipt"], legacy)
        self.assertEqual(bridge.release(1, "WO-MIXED", on=EARLY)["pin"]["receiver_markers"],
                         {"mes": receipts["mes"]["receiver_marker"]})

    def test_response_loss_then_reconcile(self):
        self.erp.drop_create_response = True
        with self.assertRaises(TimeoutError):
            self.bridge.publish(1, on=EARLY)
        self.assertEqual(len(self.erp.boms), 1)
        self.assertEqual(self.erp.boms["BOM-1"]["docstatus"], 0)
        with self.assertRaises(RevisionMismatch):
            self.reopen().release(1, "WO-A", on=EARLY)
        result = self.reopen().publish(1, on=EARLY)
        self.assertEqual(len(self.erp.boms), 1)
        self.assertEqual(len(self.mes.boms), 1)
        self.assertEqual(self.erp.boms["BOM-1"]["docstatus"], 1)
        self.assertEqual(self.erp.boms["BOM-1"]["items"][0]["qty"], 0.25)
        self.assertEqual(self.mes.lines[1]["product_qty"], 0.25)
        self.assertEqual(result, self.reopen().publish(1, on=LATER))

    def test_partial_ack_and_odoo_response_loss(self):
        call = self.mes.call

        def lost_create(model, method, *args, **kwargs):
            result = call(model, method, *args, **kwargs)
            if method == "create":
                self.mes.call = call
                raise TimeoutError("fixture: Odoo committed and response was lost")
            return result

        self.mes.call = lost_create
        with self.assertRaises(TimeoutError):
            self.bridge.publish(1, on=EARLY)
        self.assertIn((1, "erp"), self.source.receipts)
        self.assertNotIn((1, "mes"), self.source.receipts)
        with self.assertRaises(RevisionMismatch):
            self.reopen().release(1, "WO-A", on=EARLY)
        result = self.reopen().publish(1, on=LATER)
        self.assertEqual(len(self.mes.boms), 1)
        self.assertEqual(result["erp"]["receipt"]["on"], EARLY)
        self.assertEqual(result["mes"]["receipt"]["on"], LATER)

    def test_later_effectivity_and_reopened_dispatch(self):
        first = self.bridge.publish(1, on=EARLY)
        saved = self.bridge.release(1, "WO-A", on=EARLY)
        second = self.bridge.publish(2, on=LATER)
        newer = self.reopen().release(1, "WO-B", on=LATER)
        self.assertEqual(newer["pin"]["native_id"], 2)
        self.assertEqual(saved, self.reopen().release(1, "WO-A", on=EARLY))
        dispatch = self.reopen().dispatch("WO-A", on=LATER, quantity="10", hold_location_id=7)
        self.assertEqual(dispatch["bom_id"], first["mes"]["bom_id"])
        self.assertNotEqual(dispatch["bom_id"], second["mes"]["bom_id"])

    def test_erp_pin_recovers_before_first_bridge_checkpoint(self):
        first = self.bridge.publish(1, on=EARLY)
        pin = self.bridge.plm.pin(1, 'WO-A', on=EARLY)
        self.bridge.publish(2, on=LATER)
        with self.assertRaises(RevisionMismatch):
            self.reopen().release(2, 'WO-A', on=EARLY, pinned=pin)
        self.assertEqual(self.reopen().release(1,'WO-A',on=EARLY,pinned=pin)['pin'],pin)
        actual = self.reopen().dispatch('WO-A',on=LATER,quantity='10',hold_location_id=7)
        self.assertEqual(actual['bom_id'],first['mes']['bom_id'])

    def test_changed_destination_quantity_blocks_ack_and_dispatch(self):
        first = self.bridge.publish(1, on=EARLY)
        self.bridge.release(1, "WO-A", on=EARLY)
        erp_row = self.erp.boms[first["erp"]["bom_id"]]["items"][0]
        erp_row.update(qty=1, stock_qty=1)
        with self.assertRaisesRegex(RevisionMismatch, "ERPNext BOM .* lines differ"):
            self.bridge.publish(1, on=LATER)
        erp_row.update(qty=0.25, stock_qty=0.25)
        self.mes.lines[1]["product_qty"] = 1  # Fingerprint fixture still returns original.
        with self.assertRaisesRegex(RevisionMismatch, "Odoo BOM .* lines differ"):
            self.reopen().dispatch("WO-A", on=LATER, quantity=1, hold_location_id=7)

    def test_matching_lines_with_changed_execution_fingerprint_block_release(self):
        self.bridge.publish(1, on=EARLY)
        self.bridge.release(1, "WO-A", on=EARLY)
        acknowledge = self.mes.acknowledge

        def replaced_operation(pk):
            result = acknowledge(pk)
            return {**result, "revision": result["revision"].split("@")[0] + "@" + "f" * 64}

        self.mes.acknowledge = replaced_operation
        with self.assertRaisesRegex(RevisionMismatch, "Published receiver identity or fingerprint changed"):
            self.reopen().release(1, "WO-B", on=EARLY)
        with self.assertRaisesRegex(RevisionMismatch, "Pinned receiver identity or fingerprint changed"):
            self.reopen().dispatch("WO-A", on=LATER, quantity=1, hold_location_id=7)

    def test_missing_or_inconsistent_mapping_prevents_writes(self):
        entry = self.mapping[PRODUCT]["mes"]["components"].pop("NV-BRIDGE-BULK")
        with self.assertRaisesRegex(RevisionMismatch, "Missing mes component"):
            self.bridge.publish(1, on=EARLY)
        self.mapping[PRODUCT]["mes"]["components"]["NV-BRIDGE-BULK"] = {**entry, "unit": 5}
        with self.assertRaisesRegex(RevisionMismatch, "Odoo product or stock UOM mismatch"):
            self.bridge.publish(1, on=EARLY)
        self.assertFalse(self.erp.boms)
        self.assertFalse(self.mes.boms)

    def test_mapping_cannot_change_across_publication_retry(self):
        self.erp.drop_create_response = True
        with self.assertRaises(TimeoutError):
            self.bridge.publish(1, on=EARLY)
        self.mapping[PRODUCT]["mes"]["components"]["NV-BRIDGE-BULK"]["factor"] = "0.002"
        with self.assertRaisesRegex(RevisionMismatch, "Conflicting destination mapping"):
            self.reopen().publish(1, on=EARLY)
        with self.assertRaisesRegex(RevisionMismatch, "no durable release pin"):
            self.reopen().dispatch("WO-A", on=EARLY, quantity=1, hold_location_id=7)


if __name__ == "__main__":
    unittest.main()
