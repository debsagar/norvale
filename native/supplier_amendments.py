"""Admit dated supplier price amendments from the buyer's native mailbox."""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re

from walkthrough.engine import ROLES, SUPPLIERS

SUBJECT = "Supplier quotation amendment"
FIELDS = {"supplier", "quote", "quote_line", "po", "po_line", "currency", "uom",
          "unit_price", "effective_at"}
MAILBOX = re.compile(r"[a-z0-9][a-z0-9._-]*@norvale\.test")
MESSAGE_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")
PRICE = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,4})?")


def sender_identities(senders):
    if not isinstance(senders, dict):
        raise ValueError("Supplier amendment senders must map catalog suppliers to local mail accounts")
    employees = {r["email"].replace(".example", ".test") for r in ROLES}
    addresses = set()
    identities = []
    for vendor, address in senders.items():
        if (vendor not in SUPPLIERS or not isinstance(address, str) or not MAILBOX.fullmatch(address)
                or address in employees or address in addresses):
            raise ValueError("Invalid or nonexclusive supplier amendment sender")
        addresses.add(address)
        identities.append(dict(id=f"SUPPLIER_{vendor}", mail=address))
    return identities


def read_buyer_inbox(company, cutoff):
    buyer = company.mail_client("BUYER")
    senders = set(company.config["supplier_amendment_senders"].values())
    entries = {}
    for header in buyer.find_subject(SUBJECT):
        if header["subject"] != SUBJECT or header["from"] not in senders:
            continue
        message = buyer.read_mail(header["uid"], body_limit=4097, delivery_metadata=True)
        received_at = datetime.fromisoformat(message["received_at"])
        if received_at > cutoff:
            continue
        sender = message["from"]
        message_id = message["message_id"]
        if (sender != header["from"] or message["return_path"] != f"<{sender}>"
                or message["to"] != buyer.addr or message["subject"] != SUBJECT or message["cc"]
                or not MESSAGE_ID.fullmatch(message_id)
                or message["content_type"] != "text/plain" or len(message["body"]) > 4096):
            continue
        try:
            terms = json.loads(message["body"])
            if (not isinstance(terms, dict) or terms.keys() != FIELDS
                    or any(not isinstance(value, str) or not value for value in terms.values())
                    or not PRICE.fullmatch(terms["unit_price"]) or Decimal(terms["unit_price"]) <= 0):
                continue
            effective_at = datetime.fromisoformat(terms["effective_at"])
            if effective_at.utcoffset() is None or effective_at > cutoff:
                continue
        except ValueError:
            continue
        terms["effective_at"] = effective_at.astimezone(timezone.utc).isoformat()
        identity = (sender, message_id)
        prior = entries.get(identity)
        if prior and prior["terms"] != terms:
            raise ValueError("Conflicting supplier amendments share a Message-ID")
        stamp = received_at.astimezone(timezone.utc).isoformat()
        if not prior or stamp < prior["received_at"]:
            entries[identity] = dict(sender=sender, message_id=message_id, received_at=stamp, terms=terms)
    return list(entries.values())


def refer_to_approver(company, key, purchase, po, inbox, cutoff):
    """Bind buyer-visible terms to the native draft without changing its price or approval."""
    sender = company.config["supplier_amendment_senders"].get(purchase["vendor"])
    candidates = [entry for entry in inbox
                  if entry["sender"] == sender and entry["terms"]["po"] == po["name"]]
    if not candidates:
        return []
    draft = company.get(key, "BUYER")
    quote = company.get("quote:" + key, "BUYER")
    if (draft["docstatus"] != 0 or draft.get("company") != company.company
            or quote["docstatus"] != 1 or quote.get("company") != company.company
            or quote["supplier"] != draft["supplier"]
            or quote["currency"] != draft["currency"]
            or draft["supplier"] != company.get("vendor:" + purchase["vendor"], "BUYER")["name"]):
        return []
    created = datetime.fromisoformat(draft["creation"])
    if created.utcoffset() is None:
        created = created.replace(tzinfo=timezone.utc)
    draft_rows = {row["name"]: row for row in draft["items"]}
    quote_rows = {row["name"]: row for row in quote["items"]}
    approver_rows = {row["name"]: row for row in po["items"]}
    referrals = []
    # Retain both messages when effective times tie. All observed terms go to judgment.
    for entry in sorted(candidates, key=lambda record: (record["terms"]["effective_at"], record["message_id"])):
        terms = entry["terms"]
        row = draft_rows.get(terms["po_line"])
        quoted = quote_rows.get(terms["quote_line"])
        if (not row or not quoted or row != approver_rows.get(terms["po_line"])
                or terms["supplier"] != draft["supplier"] or terms["quote"] != quote["name"]
                or row.get("supplier_quotation") != quote["name"]
                or row.get("supplier_quotation_item") != quoted["name"]
                or row["item_code"] != quoted["item_code"]
                or terms["currency"] != draft["currency"]
                or terms["uom"] != row.get("uom") or terms["uom"] != quoted.get("uom")
                or datetime.fromisoformat(entry["received_at"]) <= created
                or datetime.fromisoformat(terms["effective_at"]) <= created):
            continue

        response = dict(**entry, draft_unit_price=row["rate"], original_quote_unit_price=quoted["rate"],
                        quantity=row["qty"], item_code=row["item_code"],
                        buyer_response="Referred to SCM for review. PO terms unchanged.")
        operation = "supplier-amendment-referral:" + sha256(
            json.dumps([entry["sender"], entry["message_id"]]).encode()).hexdigest()
        company.mail(operation, "BUYER", "SCM_MGR", "Supplier amendment referred for review",
                     json.dumps(response, sort_keys=True), sent_at=cutoff)
        referrals.append(dict(**response, buyer_response_message_id=
                              company.state["mail_operations"][operation]["message_id"]))

    latest = {}
    for referral in referrals:
        terms = referral["terms"]
        latest[terms["po_line"]] = terms["effective_at"]
    for referral in referrals:
        terms = referral["terms"]
        referral["current_for_line"] = terms["effective_at"] == latest[terms["po_line"]]
    return referrals
