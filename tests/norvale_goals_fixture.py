"""ERP/MES/QMS transport fixtures for the ordinary recurring methods.

The queue calls CompanyEngine and CompanyERP. This file supplies the
independent role inputs and implements the offline ERP document boundary.
"""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch

from integrations.company_run.authority import Authority as NativeAuthority
from integrations.company_run.operating import HIGH, LOW, Operating
from integrations.company_run.quality import lot_specification
from integrations.qms.adapter import INSPECTION, eligible
from native.company import Engine as Company
from native.company_bank import ACCOUNT, FEED_USER, TRANSACTION
from native.company_erp import CompanyERP
from native.company_records import SalesRecords
from native.recurring_extensions import DAY, MIDNIGHT, RecurringSales


class ERP:
    def __init__(self, company):
        self.company = company
        self.docs = {}
        self.counters = {}
        self.lost = set()
        self.calls = []

    def create(self, kind, fields):
        self.counters[kind] = count = self.counters.get(kind, 0) + 1
        name = fields.get("batch_id") if kind == "Batch" else f"{kind}-{count}"
        customers = {"Sales Order", "Delivery Note", "Sales Invoice"}
        doc = dict(deepcopy(fields), doctype=kind, name=name, docstatus=0,
                   minute=self.company.minute)
        doc.setdefault("posting_date", self.company.date())
        if kind in customers:
            doc["currency"] = "EUR"
        for index, row in enumerate(doc.get("items", []), 1):
            row.update(name=f"{name}-{index}", conversion_factor=1, stock_uom="Nos", uom="Nos")
            row["stock_qty"] = row["qty"]
            if kind == "Stock Entry":
                row["transfer_qty"] = row["qty"]
            if kind == "Sales Order":
                row["delivered_qty"] = 0
        if kind == "Sales Invoice":
            total = sum(row["qty"] * row["rate"] for row in doc["items"])
            doc.update(grand_total=total, outstanding_amount=total,
                       party_account_currency="EUR", debit_to="Receivable")
        self.docs[kind, name] = doc
        return deepcopy(doc)

    def update(self, kind, name, fields):
        doc = self.docs[kind, name]
        assert doc["docstatus"] == 0 and fields == {"docstatus": 1}
        for row in doc.get("items", []):
            if kind == "Delivery Note":
                order = self.docs["Sales Order", row["against_sales_order"]]
                order["items"][0]["delivered_qty"] += row["qty"]
                self.stock(doc, row, row["warehouse"], -1)
            elif kind in ("Stock Entry", "Purchase Receipt"):
                if row.get("s_warehouse"):
                    self.stock(doc, row, row["s_warehouse"], -1)
                if row.get("t_warehouse") or row.get("warehouse"):
                    self.stock(doc, row, row.get("t_warehouse") or row["warehouse"], 1)
        if kind == "Sales Invoice":
            self.gl(doc, "Receivable", "Sales", doc["grand_total"])
        if kind == "Payment Entry":
            ref, = doc["references"]
            invoice = self.docs[ref["reference_doctype"], ref["reference_name"]]
            invoice["outstanding_amount"] -= ref["allocated_amount"]
            self.gl(doc, doc["paid_to"], "Receivable", doc["paid_amount"])
        doc.update(fields)
        self.calls.append(("submit", kind, name))
        fault = doc.get("stock_entry_type", kind)
        if fault in self.lost:
            self.lost.remove(fault)
            raise TimeoutError(f"{fault} committed before reply was lost")
        return deepcopy(doc)

    def stock(self, doc, row, warehouse, sign):
        batch, item, qty = row["batch_no"], row["item_code"], sign * row["qty"]
        assert self.balance(batch, item, warehouse) + qty >= 0
        scope = dict(voucher_no=doc["name"], voucher_detail_no=row["name"],
                     item_code=item, warehouse=warehouse, voucher_type=doc["doctype"])
        bundle = self.create("Serial and Batch Bundle", dict(
            scope, type_of_transaction="Inward" if sign == 1 else "Outward",
            is_cancelled=0, entries=[dict(batch_no=batch, qty=qty, warehouse=warehouse)]))
        self.docs[bundle["doctype"], bundle["name"]]["docstatus"] = 1
        ledger = self.create("Stock Ledger Entry", dict(scope, stock_uom=row["stock_uom"],
            company=doc.get("company"), posting_date=doc["posting_date"],
            serial_and_batch_bundle=bundle["name"], actual_qty=qty, is_cancelled=0))
        self.docs[ledger["doctype"], ledger["name"]]["docstatus"] = 1

    def gl(self, doc, debit, credit, amount):
        for account, dr, cr in ((debit, amount, 0), (credit, 0, amount)):
            self.create("GL Entry", dict(voucher_no=doc["name"], account=account, debit=dr, credit=cr))

    def balance(self, batch, item, warehouse):
        return sum(row["actual_qty"] for row in self.docs.values()
                   if (row["doctype"], row.get("item_code"), row.get("warehouse")) ==
                   ("Stock Ledger Entry", item, warehouse)
                   and self.docs["Serial and Batch Bundle", row["serial_and_batch_bundle"]]
                   ["entries"][0]["batch_no"] == batch)

    def get(self, kind, name):
        self.calls.append(("get", kind, name))
        if kind == "Bank Account":
            assert name == ACCOUNT
            return dict(account="Bank GL", company=self.company.company, is_company_account=1)
        if kind == "Account":
            assert name == "Bank GL"
            return dict(company=self.company.company, account_type="Bank", is_group=0, account_currency="EUR")
        return deepcopy(self.docs[kind, name])

    def list(self, kind, filters, fields=None, **kwargs):
        if kind == "Custom Field":
            return [{"name": "operation"}]
        dates = filters.get("date")
        return [deepcopy(row) for (dt, _), row in self.docs.items()
                if dt == kind and all(row.get(key) == value for key, value in filters.items() if key != "date")
                and (dates is None or dates[1][0] <= row["date"] <= dates[1][1])]

    def call(self, method, args):
        method = method.rsplit(".", 1)[-1]
        if method == "get_batch_qty":
            return self.balance(args["batch_no"], args["item_code"], args["warehouse"])
        if method == "make_delivery_note":
            order = self.get("Sales Order", args["source_name"])
            line, = order["items"]
            return {**{key: order[key] for key in ("company", "customer", "currency")},
                    "items": [dict(line, against_sales_order=order["name"], so_detail=line["name"])]}
        if method == "make_sales_invoice":
            delivery = self.get("Delivery Note", args["source_name"])
            return {**{key: delivery[key] for key in ("company", "customer", "currency")},
                    "items": [dict(row, delivery_note=delivery["name"], dn_detail=row["name"])
                              for row in delivery["items"]]}
        if method == "get_payment_entry":
            invoice = self.get("Sales Invoice", args["dn"])
            amount = args["party_amount"]
            return dict(company=invoice["company"], payment_type="Receive", party_type="Customer",
                        party=invoice["customer"], paid_from="Receivable", paid_to="Bank GL",
                        paid_from_account_currency="EUR", paid_to_account_currency="EUR",
                        paid_amount=amount, received_amount=amount,
                        references=[dict(reference_doctype="Sales Invoice",
                                         reference_name=invoice["name"], allocated_amount=amount)])
        if method == "reconcile_vouchers":
            voucher, = json.loads(args["vouchers"])
            payment = self.docs["Payment Entry", voucher["payment_name"]]
            statement = self.docs[TRANSACTION, args["bank_transaction_name"]]
            payment["clearance_date"] = statement["date"]
            statement.update(status="Reconciled", unallocated_amount=0, payment_entries=[
                dict(payment_document="Payment Entry", payment_entry=payment["name"],
                     allocated_amount=payment["paid_amount"])])
            return
        raise AssertionError(method)


class Authority(NativeAuthority):
    """Independent observations returned through the ordinary authority contract."""
    def __init__(self, run):
        super().__init__(run)
        self.inspections = {}
        self.dispatches = {}
        self.reports = {}
        self.leases = {}
        self.release_leases = {}
        self.qms_batch_pointers = {}
        self.restart = None

    def settings(self):
        return dict(qms_reader={"url": "https://qms.example"},
                    qms_dispatcher={"url": "https://qms.example"},
                    qms_approver="qa", qms_source="qms-a", specifications={"RM": "RM-assay"})

    def route(self, item, source=None, **kwargs):
        cfg = dict(self.settings(), mes={"source_id": source}, units={"Units": "Nos"}, hold_location_id=20)
        return cfg, cfg if item == "FG" else None

    def release(self, batch, spec, qty, uom):
        observed = self.inspections.get(batch)
        return eligible([observed], batch, spec, qty, uom) if observed else None

    def get(self, kind, name):
        assert kind == INSPECTION
        return deepcopy(next(doc for doc in self.inspections.values() if doc["name"] == name))

    def work_center(self, revision, **kwargs):
        return 1

    def require_equipment(self, center, **kwargs):
        assert center == 1

    def dispatch(self, request, **kwargs):
        source = request["revision"]["mes_source"]
        count = sum(row["source"] == source for row in self.dispatches.values())
        ack = dict(id=str(count + 1), version="1", source=source, request=request)
        return self.dispatches.setdefault(request["key"], ack)

    def find_dispatch(self, key, **kwargs):
        return self.dispatches.get(key)

    def execution_reader(self, cfg, product):
        source = cfg["mes"]["source_id"]

        def read(model, mo):
            assert model == "mrp.production"
            return {"state": "done" if (source, mo) in self.reports else "confirmed"}

        return SimpleNamespace(source_id=source, read=read,
                               completion=lambda mo: self.reports[source, mo])

    def release_transfer_source(self, erp):
        return "erp-a"

    def qms_lease(self, request, *, outcome, commit=None):
        if request.get("kind") == "release_transfer":
            return self.scripted_transfer_lease(request, outcome, commit)

        for scope in request["scopes"]:
            inspection = self.get(INSPECTION, scope["inspection"])
            assert inspection["modified"] == scope["version"]
            assert eligible([inspection], scope["batch"], scope["revision"], scope["qty"], scope["uom"])
        old = self.leases.get(request["id"], "absent")
        if outcome != "status":
            assert (old, outcome) in {("absent", "active"), ("active", "active"),
                                      ("active", "armed"), ("armed", "submitted"),
                                      ("submitted", "submitted")}
            self.leases[request["id"]] = outcome
        return dict(id=request["id"], delivery=request["delivery"],
                    state=self.leases.get(request["id"], "absent"),
                    submit_permitted=old == "active" and outcome == "armed")

    def scripted_transfer_lease(self, request, outcome, commit, *, actor="dispatcher"):
        from integrations.qms.dispatch_lease import ENDPOINT

        def get_value(entity, key, fields, *, for_update, as_dict=False, order_by=None):
            assert for_update
            if entity == "Batch":
                return SimpleNamespace(name=key, item=self.inspections[key]["item_code"],
                                       custom_dispatch_lease=self.qms_batch_pointers.get(key))
            if entity == INSPECTION:
                sample = self.inspections[key["batch_no"]]
                if "name" in key:
                    return None
                row = dict(sample, creation="qms-local")
                return SimpleNamespace(**{field: row.get(field) for field in fields})
            assert entity == "Norvale Dispatch Lease"
            receipt = self.release_leases.get(key)
            return SimpleNamespace(**receipt) if receipt else None

        def get_doc(entity, name=None, **kwargs):
            if name:
                native = SimpleNamespace(**self.release_leases[name])
                def save(**kwargs):
                    self.release_leases[name].update({k: v for k, v in vars(native).items() if k != "save"})
                native.save = save
                return native
            return SimpleNamespace(insert=lambda **kwargs: self.release_leases.update(
                {entity["name"]: dict(entity, owner=actor, commit_json=None)}))

        def set_value(entity, batch, field, value, **kwargs):
            assert (entity, field) == ("Batch", "custom_dispatch_lease")
            self.qms_batch_pointers[batch] = value

        def throw(message, error=ValueError):
            raise error(message)

        frappe = SimpleNamespace(
            form_dict=dict(lease=request, outcome=outcome, commit=commit), session=SimpleNamespace(user=actor),
            db=SimpleNamespace(get_value=get_value, set_value=set_value,
                               exists=lambda *args: actor == "dispatcher"),
            get_doc=get_doc, throw=throw, PermissionError=PermissionError, response={})
        exec(ENDPOINT, dict(frappe=frappe, json=json))
        return frappe.response["message"]

    def revoke_transfer_inspection(self, batch):
        from integrations.qms.dispatch_lease import GUARD

        def throw(message):
            raise ValueError(message)

        def get_value(*args, for_update, **kwargs):
            assert for_update
            return SimpleNamespace(custom_dispatch_lease=self.qms_batch_pointers.get(batch))

        doc = SimpleNamespace(batch_no=batch, get_doc_before_save=lambda: None)
        exec(GUARD, dict(doc=doc, frappe=SimpleNamespace(
            throw=throw, db=SimpleNamespace(get_value=get_value))))
        self.inspections[batch].update(workflow_state="Revoked", modified="revoked-2")

    def observed_faults(self, centers, **kwargs):
        return []

    def equipment_fault(self, center, key, **kwargs):
        return dict(source_id="mes-a", native_id=1)

    def interrupted_batch(self, fault, **kwargs):
        return None

    def maintenance_ticket(self, key, case, **kwargs):
        return dict(number=1, html_url="https://tickets.example/issues/1", source_id="tickets-a")

    def restart_evidence(self, ticket, case, **kwargs):
        return self.restart

    def equipment_restart(self, case, **kwargs):
        assert case["verification"] == self.restart
        return "RESTART-1"

    def close_maintenance_ticket(self, key, case, **kwargs):
        pass


class CompanyFixture(Company):
    """Offline construction of the recurring controller with injected ERP roles."""
    def __init__(self, run, capacity):
        self.run = run
        self.day = self.minute = 0
        self.event = "setup"
        self.wh = dict(rm="RM", fg="FG", hold="Hold", wip="WIP")
        self.config = dict(company_operating=True, capacity_units=capacity, maintenance_units=100)
        self.erp = ERP(self)
        self.admin = self.erp
        self.api = dict.fromkeys(("IT_ADMIN", "SALES", "QA", "WAREHOUSE", "FINANCE",
                                 "PLANNER", "PRODUCTION", "MAINTENANCE"), self.erp)
        self.authority = Authority(run)
        self.reload()

    def reload(self):
        path = self.run / "state.json"
        self.state = json.loads(path.read_text()) if path.exists() else dict(
            docs={}, physical={}, ops=dict(orders={}, invoices={}, maintenance={}, changes={}, shortage_days={}, meter=0))
        self.docs = self.state["docs"]
        self.company_erp = CompanyERP(self, self.authority)
        self.company_erp.definition = self.definition

    def definition(self, item):
        assert item == "FG"
        return dict(id="revision-1", version="1", erp_bom="BOM-FG",
                    mes_source="mes-a" if self.day == 0 else "mes-b",
                    inspection_spec="FG-assay", mes_ack_id="11"), {"RM": ("Nos", 1)}

    def inventory(self, role):
        return {("RM", "RM"): sum(self.erp.balance(batch, "RM", "RM")
                                 for batch in self.company_erp.state["lots"])}

    def advance_clock(self, target):
        self.minute = (target - MIDNIGHT) // timedelta(minutes=1)
        self.day = self.minute // DAY

    def scheduled_company_event(self, clock, payload):
        self.minute = clock.now
        self.day, self.event = clock.now // DAY, clock.event["id"]
        action = {
            "plan": self.plan_production, "post": self.company_erp.poll_work,
            "inspect": self.company_erp.inspections, "dispatch": self.fulfillment,
            "settle": self.settlements, "service": self.service,
        }[payload["phase"]]
        action()

    def records(self):
        records = SalesRecords(self)
        return [records.project(*entry) for entry in records.confirmed()]

    def receipt(self, key, quantity):
        doc = self.company_erp.prepare_receipt(key, "Purchase Receipt", {"items": [
            dict(item_code="RM", qty=quantity, warehouse=self.wh["hold"])]}, "WAREHOUSE")
        self.company_erp.receive(doc, "incoming", "WAREHOUSE")
        return doc["items"][0]["batch_no"]

    def inspect(self, batch, reading):
        # This external QA fixture is supplied independently of fulfillment.
        # The scoped result follows the operating 5–6 assay range.
        if reading is None:
            return
        lot = self.company_erp.state["lots"][batch]
        cfg = dict(self.authority.settings(), lab=dict(parameter="pH", analyst_user="lab"))
        analyst = Operating.__new__(Operating)
        analyst.e, analyst.cfg = self, cfg
        status = "Accepted" if LOW <= reading <= HIGH else "Rejected"
        document = dict(
            name=f"QI-{len(self.authority.inspections) + 1}", modified="signed-1", modified_by="qa",
            batch_no=batch, item_code=lot["item"], docstatus=1, status=status,
            report_date=self.date(), inspected_by="lab", minute=self.minute,
            custom_spec_revision=lot_specification(cfg, lot),
            custom_lot_quantity=lot["qty"], custom_lot_uom=lot["uom"],
            workflow_state="Released" if status == "Accepted" else "Rejected",
            readings=[dict(specification=analyst.parameter(lot), numeric=1, min_value=LOW,
                           max_value=HIGH, reading_1=str(reading), status=status)],
        )
        assert analyst.observed(lot, document) == reading
        self.authority.inspections[batch] = document

    def mes_observed(self, *, material_batch, quantity):
        """Supply a separate MES execution observation in the adapter's native schema."""
        work, = (work for work in self.company_erp.state["work"].values()
                 if (work["dispatch"]["source"], int(work["dispatch"]["id"])) not in self.authority.reports)
        ack = work["dispatch"]
        source, mo = ack["source"], int(ack["id"])

        def native(entity, identifier):
            return dict(system="mes", source_id=source, entity=entity, native_id=identifier)

        self.authority.reports[source, mo] = dict(
            work=native("mrp.production", mo), completion_ref=f"{source}:mrp.production:{mo}",
            erp_work_ref=work["request"]["work_order"], output_hold_location=native("stock.location", 20),
            recorded_at_utc=f"{self.date()}T10:00:00Z",
            revision="1", bom=native("mrp.bom", 11), backorder=None,
            inputs=[dict(lot_name=material_batch, item_code="RM", uom_name="Units", quantity=quantity,
                         lot=native("stock.lot", 2), record=native("stock.move.line", 2))],
            output=dict(lot_name="MES output", lot=native("stock.lot", 1), item_code="FG",
                        record=native("stock.move.line", 1), uom_name="Units", quantity=quantity),
        )

        return self.authority.reports[source, mo]

    def remittance(self, reference, invoice_id, *, day, amount):
        # The caller supplies party, time and amount independently of due dates.
        doc = self.erp.create(TRANSACTION, dict(owner=FEED_USER, bank_account=ACCOUNT, company=self.company,
            currency="EUR", party_type="Customer", party="Acme", reference_number=invoice_id,
            transaction_id=reference, date=self.dated(day), deposit=amount, withdrawal=0,
            unallocated_amount=amount, status="Unreconciled"))
        self.erp.update(TRANSACTION, doc["name"], {"docstatus": 1})


def start_queue(directory, company, providers, *, boundary=None):
    from engine import Engine
    from native.system_transport import SystemTransport

    transport = SystemTransport()
    SalesRecords(company).bind(transport)
    consumer = RecurringSales({
        "systems": {"erp": {"adapter": "company-production"}},
        "roles": {"sales": {}, "FINANCE": {}},
        "products": {"finished": {"system": "erp", "item": "FG"}},
    }, company.advance_clock, company)
    return Engine(directory, consumer, seed=41, native=transport, providers=providers,
                  monotonic=providers.monotonic, boundary=boundary)


def schedule_order(test, run, quantity):
    from native.production_profiles import ADAPTER
    # RecurringSales uses this profile validation before admitting the ERP order.
    assert run.catalog["systems"]["erp"]["adapter"] == ADAPTER
    test.enterContext(patch("native.recurring_extensions.profile",
                            return_value=("bound", dict(item="FG", customer="Acme", order_roles=["sales"]))))
    test.schedule(run, "order", case="C1", quantity=quantity, rate=25, product="finished", role="sales")
