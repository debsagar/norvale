"""Engine transport for a scoped ERPNext managed-services engagement."""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json

from norvale.loader.erp_client import ErpError


KEY = "custom_service_operation"
EVENT = "custom_service_event"
TYPES = {
    "erp": {"contract": "Project", "timesheet": "Timesheet",
            "invoice": "Sales Invoice", "payment": "Payment Entry"},
    "tickets": {"ticket": "Issue", "ticket_response": "Comment", "sla_breach": "Comment",
                "staff_unavailable": "Comment", "response_blocked": "Comment"},
}


def amount(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def require(condition, message):
    if not condition:
        raise ValueError(message)


class ServiceTransport:
    """Use scoped service and Finance clients on one site and a unique run namespace.

    The site needs a non-stock service item and a same-currency cash account.
    Each mapped DocType needs a unique Data field named by KEY and a Long
    Text field named by EVENT to recover interrupted writes and verify scope.
    The service identity needs access to the Timesheet billing fields.
    """

    def __init__(self, service, finance, *, namespace, company, customer, item,
                 cash_account, epoch, currency="USD"):
        require(service.base == finance.base and service.site == finance.site,
                "Service and Finance must use the same site")
        self.service, self.finance = service, finance
        self.prefix = sha256(namespace.encode()).hexdigest()[:24] + ":"
        self.company, self.customer = company, customer
        self.item, self.cash_account, self.currency = item, cash_account, currency
        self.epoch = datetime.fromisoformat(epoch)

    def _api(self, doctype):
        return self.finance if doctype in ("Sales Invoice", "Payment Entry") else self.service

    def _get(self, native_id, doctype):
        kind, name = native_id.split("/", 1)
        require(kind == doctype, f"Expected {doctype} reference")
        doc = self._api(kind).get(kind, name)
        require(doc.get(KEY, "").startswith(self.prefix), "Native reference belongs to another run")
        return doc

    def _find(self, doctype, key):
        api = self._api(doctype)
        matches = api.list(doctype, {KEY: key}, limit=2)
        require(len(matches) <= 1, "Duplicate native operation key")
        return api.get(doctype, matches[0]["name"]) if matches else None

    def find(self, system, retry_id):
        for doctype in dict.fromkeys(TYPES[system].values()):
            doc = self._find(doctype, self.prefix + retry_id)
            if doc and doc["docstatus"] == self._docstatus(json.loads(doc[EVENT])):
                return f"{doctype}/{doc['name']}"
        return None

    @staticmethod
    def _docstatus(event):
        return int(event["kind"] in ("invoice", "payment") or
                   (event["kind"] == "timesheet" and event["approved"]))

    def send(self, system, retry_id, document):
        doctype = TYPES[system][document["kind"]]
        api = self._api(doctype)
        key = self.prefix + retry_id
        row = self._find(doctype, key)
        if row is None:
            fields = self._fields(key, document)
            try:
                row = api.create(doctype, {**fields, KEY: key, EVENT: json.dumps(document, sort_keys=True)})
            except ErpError:
                row = self._find(doctype, key)
                if row is None:
                    raise
        require(json.loads(row[EVENT]) == document, "Native operation differs from replay intent")
        self._verify(row, document)
        require(row["docstatus"] in (0, self._docstatus(document)), "Unexpected native document status")
        if row["docstatus"] == 0 and self._docstatus(document):
            row = api.update(doctype, row["name"], {"docstatus": 1})
            if document["kind"] == "invoice":
                require(amount(row["outstanding_amount"]) == amount(document["amount"]),
                        "New invoice balance differs from the amount billed")
        return f"{doctype}/{row['name']}"

    def read(self, system, native_id):
        doctype = native_id.split("/", 1)[0]
        require(doctype in TYPES[system].values(), "Wrong native system")
        row = self._get(native_id, doctype)
        event = json.loads(row[EVENT])
        require(TYPES[system][event["kind"]] == doctype and row["docstatus"] == self._docstatus(event),
                "Unexpected native document status")
        self._verify(row, event)
        if event["kind"] == "timesheet":
            event["contract_id"] = f"Project/{row['parent_project']}"
        return {**event, "native_id": native_id, "system": system}

    def records(self):
        records = []
        for system, mapping in TYPES.items():
            for doctype in dict.fromkeys(mapping.values()):
                api = self._api(doctype)
                matches = api.list(doctype, [[KEY, "like", self.prefix + "%"]], limit=0)
                for match in matches:
                    row = api.get(doctype, match["name"])
                    if row["docstatus"] == self._docstatus(json.loads(row[EVENT])):
                        records.append(self.read(system, f"{doctype}/{row['name']}"))
        return records

    def _date(self, minutes):
        return (self.epoch + timedelta(minutes=minutes)).date().isoformat()

    def _timesheet_terms(self, event):
        issue = self._get(event["ticket_id"], "Issue")
        ticket = json.loads(issue[EVENT])
        self._verify(issue, ticket)
        contract = self._get(ticket["contract_id"], "Project")
        terms = json.loads(contract[EVENT])
        self._verify(contract, terms)
        require(terms["case"] == ticket["case"] == event["case"] and ticket["ticket"] == event["ticket"],
                "Timesheet does not match its contract and issue")
        return contract, terms

    def _fields(self, key, event):
        kind = event["kind"]
        case = event["case"]
        if kind == "contract":
            require(event.get("currency", "USD") == self.currency, "Contract currency is outside the site scope")
            return dict(project_name=f"{case} ({key})", company=self.company, customer=self.customer)
        if kind == "payment":
            return self._payment_fields(event)
        if "contract_id" in event:
            contract = self._get(event["contract_id"], "Project")
            terms = json.loads(contract[EVENT])
            require(contract["company"] == self.company and contract["customer"] == self.customer
                    and terms["case"] == case, "Project is outside the service scope")
        if kind == "ticket":
            return dict(subject=event["ticket"], description=key, customer=self.customer,
                        project=contract["name"], company=self.company)
        if kind == "timesheet":
            contract, terms = self._timesheet_terms(event)
            start = self.epoch + timedelta(minutes=event["at"])
            log = dict(project=contract["name"], hours=event["hours"], from_time=str(start),
                       is_billable=int(event["approved"]),
                       billing_rate=terms["rate"] if event["approved"] else 0)
            return dict(company=self.company, parent_project=contract["name"],
                        currency=terms.get("currency", "USD"), exchange_rate=1, time_logs=[log])
        if kind == "invoice":
            require(terms.get("currency", "USD") == event["currency"], "Invoice currency differs from contract")
            details = self._invoice_details(event, contract["name"], terms["rate"])
            return dict(company=self.company, customer=self.customer, currency=event["currency"],
                        project=contract["name"], set_posting_time=1, posting_date=self._date(event["at"]),
                        due_date=self._date(event["due"]), update_stock=0, disable_rounded_total=1,
                        ignore_pricing_rule=1, timesheets=details,
                        items=[dict(item_code=self.item, qty=1, rate=event["amount"])])
        if kind == "staff_unavailable":
            projects = self.service.list("Project", [[KEY, "like", self.prefix + "%"]], limit=0)
            name = next(project["name"] for project in projects if json.loads(
                self.service.get("Project", project["name"])[EVENT])["case"] == case)
            target = "Project", name
        else:
            issue = self._get(event["ticket_id"], "Issue")
            require(json.loads(issue[EVENT])["case"] == case, "Issue belongs to another case")
            target = "Issue", issue["name"]
        return dict(reference_doctype=target[0], reference_name=target[1], comment_type="Comment",
                    content=" ".join(str(event[field]) for field in ("kind", "technician") if field in event))

    def _invoice_details(self, event, project, rate, invoice=None):
        ids = event["timesheet_ids"]
        require(ids and len(set(ids)) == len(ids), "Invoice requires unique timesheets")
        children = []
        for native_id in ids:
            sheet = self._get(native_id, "Timesheet")
            recorded = json.loads(sheet[EVENT])
            self._verify(sheet, recorded)
            log, = sheet["time_logs"]
            require(sheet["docstatus"] == 1 and recorded["approved"] and log["is_billable"]
                    and sheet["parent_project"] == project and log["project"] == project
                    and sheet["currency"] == event["currency"]
                    and recorded["case"] == event["case"] and Decimal(str(log["billing_rate"])) == Decimal(str(rate))
                    and (not log.get("sales_invoice") or log["sales_invoice"] == invoice),
                    "Timesheet is unapproved, belongs elsewhere, or has already been billed")
            subtotal = amount(Decimal(str(recorded["hours"])) * Decimal(str(rate)))
            children.append(dict(time_sheet=sheet["name"], timesheet_detail=log["name"],
                                 billing_hours=recorded["hours"], billing_amount=float(subtotal)))
        require(sum(amount(row["billing_amount"]) for row in children) == amount(event["amount"]),
                "Invoice total differs from contract-rate timesheets")
        return children

    def _payment_fields(self, event):
        duplicate = self.finance.list("Payment Entry", {"company": self.company,
                                                         "reference_no": event["reference"]}, limit=1)
        require(not duplicate, "Bank reference has already been posted")
        invoice = self._get(event["invoice_id"], "Sales Invoice")
        self._verify(invoice, json.loads(invoice[EVENT]))
        require(invoice["docstatus"] == 1 and json.loads(invoice[EVENT])["case"] == event["case"]
                and invoice["currency"] == event["currency"]
                and amount(invoice["outstanding_amount"]) == amount(event["amount"]) + amount(event["outstanding"]),
                "Observed payment does not reconcile to the native invoice balance")
        entry = self.finance.call("erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
                                  dict(dt="Sales Invoice", dn=invoice["name"], bank_account=self.cash_account,
                                       party_amount=event["amount"], reference_date=self._date(event["observed_at"])))
        return {**entry, "posting_date": self._date(event["at"]), "reference_no": event["reference"],
                "reference_date": self._date(event["observed_at"])}

    def _verify(self, row, event):
        kind = event["kind"]
        if kind == "contract":
            require((row["company"], row["customer"]) == (self.company, self.customer),
                    "Project company or customer mismatch")
        elif kind == "ticket":
            require((row["project"], row["company"], row["customer"], row["subject"]) ==
                    (event["contract_id"].split("/", 1)[1], self.company, self.customer, event["ticket"]),
                    "Issue does not match the contract")
        elif kind == "timesheet":
            log, = row["time_logs"]
            contract, terms = self._timesheet_terms(event)
            rate = terms["rate"] if event["approved"] else 0
            require(row["company"] == self.company
                    and row["parent_project"] == log["project"] == contract["name"]
                    and row["currency"] == terms.get("currency", "USD")
                    and Decimal(str(log["hours"])) == Decimal(str(event["hours"]))
                    and bool(log["is_billable"]) == event["approved"]
                    and Decimal(str(log["billing_rate"])) == Decimal(str(rate)),
                    "Timesheet project, hours, rate or approval mismatch")
        elif kind == "invoice":
            project = self._get(event["contract_id"], "Project")
            details = self._invoice_details(event, project["name"], json.loads(project[EVENT])["rate"],
                                            row["name"] if row["docstatus"] == 1 else None)
            require((row["company"], row["customer"], row["project"], row["currency"], row["due_date"]) ==
                    (self.company, self.customer, project["name"], event["currency"], self._date(event["due"]))
                    and not row["update_stock"] and row["disable_rounded_total"]
                    and row["posting_date"] == self._date(event["at"])
                    and amount(row["grand_total"]) == amount(event["amount"])
                    and [{k: detail[k] for k in expected} for detail, expected in
                         zip(row["timesheets"], details, strict=True)] == details,
                    "Sales Invoice does not match its terms or timesheet links")
        elif kind == "payment":
            invoice = self._get(event["invoice_id"], "Sales Invoice")
            ref, = row["references"]
            require((row["company"], row["party_type"], row["party"], row["payment_type"],
                     row["paid_from_account_currency"], row["paid_to_account_currency"],
                     row["paid_to"], row["reference_no"],
                     ref["reference_doctype"], ref["reference_name"]) ==
                    (self.company, "Customer", self.customer, "Receive", event["currency"],
                     event["currency"], self.cash_account, event["reference"], "Sales Invoice", invoice["name"])
                    and row["posting_date"] == self._date(event["at"])
                    and row["reference_date"] == self._date(event["observed_at"])
                    and amount(row["paid_amount"]) == amount(row["received_amount"])
                    == amount(ref["allocated_amount"]) == amount(event["amount"]),
                    "Payment Entry does not match the bank observation or invoice allocation")
            expected = amount(event["outstanding"])
            current = amount(invoice["outstanding_amount"])
            require(current <= expected if row["docstatus"] == 1 else
                    current == expected + amount(event["amount"]), "Payment Entry balance mismatch")
        elif "ticket_id" in event:
            require((row["reference_doctype"], row["reference_name"]) ==
                    ("Issue", event["ticket_id"].split("/", 1)[1]), "Comment issue mismatch")
