"""Role-scoped Frappe REST client for the dedicated QMS site."""

import json
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

INSPECTION = "Quality Inspection"
CASE = "Non Conformance"
ACTION = "Quality Action"
ENTITIES = ("Batch", "Stock Entry", INSPECTION, CASE, ACTION)
OP = "custom_qms_operation"
STATE = "workflow_state"
STATES = {"Record": "Recorded", "Authorize Rework": "Rework Authorized",
          "Reject Lot": "Lot Rejected", "Release Lot": "Released", "Revoke Release": "Revoked"}


class APIError(RuntimeError):
    def __init__(self, status, kind):
        self.status = status
        self.kind = kind
        super().__init__(f"Frappe HTTP {status} ({kind})")


def eligible(records, batch, revision, quantity, uom):
    """Accept exactly one current, submitted release with the requested full scope."""
    if len(records) != 1:
        return None
    record = records[0]
    try:
        scope = Decimal(str(quantity))
        if not scope.is_finite() or scope <= 0:
            return None
        matches = scope == Decimal(str(record.get("custom_lot_quantity")))
    except InvalidOperation:
        return None
    if (matches and batch and revision and uom and record.get("batch_no") == batch
            and record.get("custom_spec_revision") == revision and record.get("custom_lot_uom") == uom
            and record.get("docstatus") == 1 and record.get("status") == "Accepted"
            and record.get(STATE) == "Released"):
        return record
    return None


class PinnedEndpoint(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise APIError(code, "UnexpectedRedirect")


class QMS:
    def __init__(self, url, api_key, api_secret, site="qms.localhost"):
        self.url = url.rstrip("/")
        self.opener = build_opener(PinnedEndpoint())
        self.headers = {"Authorization": f"token {api_key}:{api_secret}",
                        "X-Frappe-Site-Name": site, "Content-Type": "application/json",
                        "Accept": "application/json"}

    def request(self, method, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        request = Request(self.url + "/api/" + path, body, self.headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                kind = json.load(exc).get("exc_type", "HTTPError")
            except ValueError:
                kind = "HTTPError"
            raise APIError(exc.code, kind) from None

    @staticmethod
    def resource(doctype, name=None):
        path = "resource/" + quote(doctype, safe="")
        return path if name is None else path + "/" + quote(name, safe="")

    def get(self, doctype, name):
        return self.request("GET", self.resource(doctype, name))["data"]

    def list(self, doctype, filters=None, *, limit=100, start=0, order="name asc",
             fields=("name",)):
        args = urlencode({"filters": json.dumps(filters or {}), "fields": json.dumps(fields),
                          "limit_page_length": limit, "limit_start": start, "order_by": order})
        return self.request("GET", f"{self.resource(doctype)}?{args}")["data"]

    def create(self, doctype, operation, *, verify_fields=None, **fields):
        """Reconcile a retried POST by its unique native field and verify readback."""
        if doctype not in (INSPECTION, CASE, ACTION) or not operation or len(operation) > 100:
            raise ValueError("Expected a QMS DocType and a stable operation ID of at most 100 characters")
        found = self.list(doctype, {OP: operation}, limit=2)
        if not found:
            try:
                found = [self.request("POST", self.resource(doctype), {**fields, OP: operation})["data"]]
            except (APIError, OSError):
                found = self.list(doctype, {OP: operation}, limit=2)
                if not found:
                    raise
        if len(found) != 1:
            raise ValueError("Operation resolved to multiple QMS records")
        record = self.get(doctype, found[0]["name"])
        return self.verify_intent(record, operation, fields if verify_fields is None else verify_fields)

    @staticmethod
    def verify_intent(record, operation, fields):
        # ERPNext fills child metadata and derives status fields from readings/resolutions.
        for field, value in fields.items():
            if field == "status":
                continue
            observed = record.get(field)
            if isinstance(value, list):
                matches = isinstance(observed, list) and len(observed) == len(value) and all(
                    all(row.get(key) == cell for key, cell in expected.items())
                    for row, expected in zip(observed, value)
                )
            else:
                matches = observed == value
            if not matches:
                raise ValueError(f"QMS operation readback mismatch in {field}")
        if record.get(OP) != operation:
            raise ValueError("QMS operation identifier did not survive readback")
        return record

    def update(self, doctype, name, **fields):
        """Update an investigation or corrective action through its native controller."""
        if doctype not in (CASE, ACTION) or set(fields) & {"doctype", "name", OP, "docstatus"}:
            raise ValueError("Expected investigation or corrective action fields")
        self.request("PUT", self.resource(doctype, name), fields)
        return self.get(doctype, name)

    def transition(self, inspection, action):
        target = STATES[action]
        document = self.get(INSPECTION, inspection)
        if document.get(STATE) != target or document.get("docstatus") != 1:
            self.request("POST", "method/frappe.model.workflow.apply_workflow",
                         {"doc": {"doctype": INSPECTION, "name": inspection}, "action": action})
        document = self.get(INSPECTION, inspection)
        if document.get(STATE) != target or document.get("docstatus") != 1:
            raise ValueError("QMS workflow readback mismatch")
        return document

    def release(self, batch, revision, quantity, uom):
        """Use the latest sample at the time of the query, including drafts and failures."""
        rows = self.list(INSPECTION, {"batch_no": batch}, limit=2, order="creation desc, name desc",
                         fields=("name", "creation", "modified", "modified_by", "docstatus", "status", STATE,
                                 "batch_no", "custom_spec_revision", "custom_lot_quantity", "custom_lot_uom"))
        if len(rows) == 2 and rows[0]["creation"] == rows[1]["creation"]:
            return None
        return eligible(rows[:1], batch, revision, quantity, uom)

    def dispatch_lease(self, lease, *, outcome, commit=None):
        """Change or read one QMS lease using the dedicated dispatcher credential."""
        try:
            return self.request("POST", "method/norvale_dispatch_lease",
                                {"lease": lease, "outcome": outcome, **({"commit": commit} if commit else {})}).get("message")
        except (APIError, json.JSONDecodeError) as exc:
            # The recurring runner treats an uncertain native transport as pending.
            raise URLError(str(exc)) from exc

    def export(self):
        """Read native business records with child rows, omitting retry bookkeeping."""
        exported = {}
        for doctype in ENTITIES:
            records = {}
            start = 0
            while rows := self.list(doctype, start=start):
                for row in rows:
                    document = self.get(doctype, row["name"])
                    document.pop(OP, None)
                    if doctype == "Batch":
                        document.pop("custom_dispatch_lease", None)
                    records[row["name"]] = document
                start += len(rows)
            exported[doctype] = records
        return exported
