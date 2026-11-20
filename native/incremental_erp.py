"""Draft Sales Orders through the existing Norvale Sales User client."""
from datetime import date, timedelta
from hashlib import sha256
import json
import os
import re
from urllib.parse import urlsplit

from norvale.loader.erp_client import ErpClient, q


DOCTYPE = "Sales Order"
OPERATION = "custom_norvale_operation"
SALES_USER = "sales@norvale.test"
EPOCH = date(2027, 1, 4)


def role_digest(item, role):
    return sha256(json.dumps([item, role], ensure_ascii=True).encode()).hexdigest()[:32]


def order_dates(minute, epoch=EPOCH):
    try:
        day = epoch + timedelta(days=minute // 1440)
        return day, (day + timedelta(days=7)).isoformat()
    except OverflowError as exc:
        raise ValueError("Order completion exceeds the supported delivery date range") from exc


def sales_transport(namespace, plan, *, token_env="NORVALE_INCREMENTAL_SALES_TOKEN", epoch=EPOCH, employee=None):
    if plan["consumer"] != "norvale" or plan["config"]:
        raise ValueError("Native transport supports Norvale orders with empty config only")
    target = plan.get("erp")
    if (not isinstance(target, dict)
            or set(target) != {"url", "site", "company", "customer", "item", "warehouse"}
            or any(not isinstance(value, str) or not value.strip() for value in target.values())):
        raise ValueError("ERP plan requires url, site, company, customer, item and warehouse")
    url = urlsplit(target["url"])
    if (url.scheme not in ("http", "https") or not url.hostname or url.path
            or url.username or url.password or url.query or url.fragment):
        raise ValueError("ERP url must be an HTTP(S) origin")
    validate_orders(plan["events"], epoch=epoch)
    token = os.environ.get(token_env)
    if not token:
        raise ValueError(f"Set {token_env} for the isolated Sales User")
    return SalesOrders(ErpClient(target["url"], target["site"], token), namespace, target, epoch=epoch, employee=employee)


def validate_orders(events, *, epoch=EPOCH):
    for event in events:
        if event["kind"] != "order" or event.get("role") != "sales":
            raise ValueError("Native events must be order events with role sales")
        duration = event.get("duration", 0)
        if type(duration) is not int or duration < 0 or event["at"] < 0:
            raise ValueError("Order start and duration must be nonnegative integer minutes")
        order_dates(event["at"] + duration, epoch)
        payload = event["payload"]
        if set(payload) - {"product"} != {"case", "quantity", "rate"}:
            raise ValueError("Order payload requires case, quantity and rate")
        case, quantity, rate = payload["case"], payload["quantity"], payload["rate"]
        if (not isinstance(case, str) or not case or case != case.strip() or len(case) > 140
                or type(quantity) not in (int, float) or not 0 < quantity < float("inf")
                or type(rate) not in (int, float) or not 0 <= rate < float("inf")):
            raise ValueError("Invalid Sales Order reference, quantity or rate")


class SalesOrders:
    def __init__(self, client, namespace, target, *, epoch=EPOCH, employee=None):
        if not re.fullmatch(r"[0-9a-f]{32}", namespace):
            raise ValueError("Invalid incremental run namespace")
        self.client = client
        self.prefix = f"nv-incremental-{namespace}-"
        self.marker = re.compile(re.escape(self.prefix) + r"write-[0-9]+-[0-9]+-m([0-9]+)(?:-([ir])([0-9a-f]{32}))?")
        self.target = target
        self.epoch = epoch
        self.user = employee["user"] if employee else SALES_USER
        self.role = employee["role"] if employee else None
        self.checked_identity = False
        self.catalog_roles = lambda: ("sales",)

    def _sales(self, system):
        if system != "erp":
            raise ValueError(f"Unsupported native system: {system}")
        if not self.checked_identity:
            if self.client.request("GET", "/api/method/frappe.auth.get_logged_user") != self.user:
                raise ValueError(f"ERP token must authenticate as {self.user}")
            self.checked_identity = True

    def _retry_prefix(self, retry_id):
        if not re.fullmatch(r"write-[0-9]+-[0-9]+", retry_id):
            raise ValueError("Invalid engine retry ID")
        return self.prefix + retry_id + "-m"

    def find(self, system, retry_id):
        self._sales(system)
        rows = self.client.list(DOCTYPE, {OPERATION: ["like", self._retry_prefix(retry_id) + "%"]},
                                limit=2)
        if len(rows) > 1:
            raise RuntimeError("Ambiguous ERP retry identity")
        return rows[0]["name"] if rows else None

    def send(self, system, retry_id, document):
        if system != "erp" or document.get("kind") != "order":
            raise ValueError(f"Unsupported native action: {system}/{document.get('kind')}")
        # On replay the engine checks the full action, including minute, before
        # sending. The release's unique Custom Field makes that marker unique
        # even if the original POST commits after a retry lookup.
        if self.role is not None and document.get("order_role", "sales") != self.role:
            raise PermissionError("Order role differs from the commissioned employee")
        existing = self.find(system, retry_id)
        if existing is not None:
            return existing
        day, delivery = order_dates(document["at"], self.epoch)
        item = document.get("item", self.target["item"])
        marker = self._retry_prefix(retry_id) + str(document["at"])
        role = document.get("order_role")
        if role is not None:
            marker += "-r" + role_digest(item, role)
        elif "item" in document:
            marker += "-i" + sha256(item.encode()).hexdigest()[:32]
        row = self.client.create(DOCTYPE, {
            OPERATION: marker,
            **({"remarks": role} if role is not None else {}),
            "company": self.target["company"], "customer": self.target["customer"],
            "po_no": document["case"], "transaction_date": day.isoformat(),
            "delivery_date": delivery, "ignore_pricing_rule": 1,
            "items": [{"item_code": item, "warehouse": self.target["warehouse"],
                       "delivery_date": delivery, "qty": document["quantity"], "rate": document["rate"]}],
        })
        return row["name"]

    def read(self, system, native_id):
        self._sales(system)
        row = self.client.get(DOCTYPE, native_id)
        match = self.marker.fullmatch(row.get(OPERATION) or "")
        items = row.get("items", [])
        if (not match or row.get("name") != native_id or row.get("docstatus") != 0
                or row.get("owner") != self.user or len(items) != 1
                or any(row.get(key) != self.target[key] for key in ("company", "customer"))
                or items[0].get("warehouse") != self.target["warehouse"]):
            raise RuntimeError("Sales Order readback is outside the expected draft or run scope")
        item = items[0].get("item_code")
        role = row.get("remarks") if match[2] == "r" else "sales"
        if match[2] == "r" and role is None:
            # Some Frappe schemas omit remarks. The native marker still binds
            # the item and role to the write. Resolve only a commissioned or
            # catalogued name whose digest matches that stored evidence.
            candidates = (self.role,) if self.role is not None else self.catalog_roles()
            matches = [candidate for candidate in candidates
                       if role_digest(item, candidate) == match[3]]
            if len(matches) != 1:
                raise RuntimeError("Native Sales Order role marker cannot be resolved")
            role = matches[0]
        if self.role is not None and role != self.role:
            raise PermissionError("Sales Order readback belongs to another role")
        if not isinstance(role, str) or not role.strip() or not isinstance(item, str):
            raise RuntimeError("Invalid Sales Order item or role")
        digest = role_digest(item, role) if match[2] == "r" else sha256(item.encode()).hexdigest()[:32]
        if (digest != match[3] if match[2] else item != self.target["item"]):
            raise RuntimeError("Sales Order native item differs from the intended product")
        at = int(match[1])
        day, delivery = order_dates(at, self.epoch)
        if (row.get("transaction_date") != day.isoformat()
                or row.get("delivery_date") != delivery or items[0].get("delivery_date") != delivery):
            raise RuntimeError("Sales Order dates disagree with the business minute")
        return {
            "kind": "order", "at": at, "native_id": row["name"], "system": system,
            **({"item": item} if match[2] else {}),
            "case": row["po_no"], "quantity": items[0]["qty"], "rate": items[0]["rate"],
            **({"order_role": role} if match[2] == "r" else {}),
            **({"owner": self.user} if self.role is not None else {}),
            "roles": [role], "source_url": self.target["url"] + "/app/sales-order/" + q(row["name"]),
        }

    def records(self):
        self._sales("erp")
        filters = {OPERATION: ["like", self.prefix + "%"]}
        while True:
            page = self.client.list(DOCTYPE, filters, limit=100, order_by="name asc")
            if not page:
                return
            for row in page:
                yield self.read("erp", row["name"])
            filters["name"] = [">", page[-1]["name"]]
