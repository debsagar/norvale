"""Managed-services contracts, tickets and shared decisions."""
from decimal import Decimal, ROUND_HALF_UP
import json
import re

from . import extraction, shared


DAY = 1440
CENT = Decimal("0.01")


class Consumer:
    def __init__(self, config):
        self.config = config

    def grade_extraction(self, records, cutoff):
        return extraction.grade(records, cutoff,
                                source="timesheet", unit="hours", links="timesheet_ids", quantity="hours")

    def handle(self, engine, kind, payload):
        if kind == "policy_review":
            return shared.review_policy(engine, self.config, payload)
        case = payload["case"]
        if not isinstance(case, str) or not case.strip():
            raise ValueError("Expected a nonblank case ID")
        if shared.handle(engine, self.config, kind, payload):
            return
        if kind in ("ticket", "respond", "timesheet", "reassign", "sla_deadline"):
            ticket = payload["ticket"]
            if not isinstance(ticket, str) or not ticket.strip():
                raise ValueError("Expected a nonblank ticket ID")
        links = engine.state.setdefault("managed_services", {"contracts": {}, "tickets": {}})
        case = payload["case"]
        if "terms" not in links:
            links["terms"] = {}
            if links["contracts"]:
                for row in engine.export()["records"]:
                    prior_case = row.get("case")
                    if row.get("system") == "erp" and row.get("kind") == "contract" and (
                            links["contracts"].get(prior_case) == row["native_id"]):
                        links["terms"][prior_case] = {k: row[k] for k in ("rate", "response_minutes")}
                        if "currency" in row:
                            links["terms"][prior_case]["currency"] = row["currency"]
                    if row.get("system") == "tickets" and row.get("kind") == "ticket_response" and (
                            links["tickets"].get(prior_case, {}).get(row["ticket"]) == row["ticket_id"]):
                        links.setdefault("responses", {}).setdefault(prior_case, {})[row["ticket"]] = row["native_id"]
        terms = links["terms"]
        responses = links.setdefault("responses", {}).setdefault(case, {})
        unavailable = links.setdefault("unavailable", {}).setdefault(case, {})
        if kind == "contract":
            minutes, rate = payload["response_minutes"], payload["rate"]
            if not case or type(minutes) is not int or minutes <= 0:
                raise ValueError("Contract requires a case and positive response minutes")
            if type(rate) not in (int, float) or not 0 <= rate < float("inf"):
                raise ValueError("Contract requires a nonnegative rate")
            currency = payload.get("currency", self.config.get("currency", "USD"))
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("Contract requires a three-letter currency")
            if case in links["contracts"]:
                raise ValueError(f"Contract already exists: {case}")
            fields = {"case": case, "response_minutes": minutes, "rate": rate}
            if "currency" in payload or "currency" in self.config:
                fields["currency"] = currency
            links["contracts"][case] = shared.native_id(engine.write("erp", "contract", fields))
            terms[case] = {"response_minutes": minutes, "rate": rate, "currency": currency}
        elif kind == "ticket":
            ticket = payload["ticket"]
            tickets = links["tickets"].setdefault(case, {})
            if not ticket or ticket in tickets:
                raise ValueError(f"Invalid or duplicate ticket: {ticket}")
            if case not in terms:
                raise ValueError("Ticket requires confirmed contract terms")
            tickets[ticket] = shared.native_id(engine.write("tickets", "ticket", {
                "case": case, "ticket": ticket, "contract_id": links["contracts"][case]}))
            key = json.dumps([case, ticket], separators=(",", ":"))
            engine.schedule("service-deadline:" + key, "sla_deadline",
                                engine.now + terms[case]["response_minutes"],
                                priority=1, payload={"case": case, "ticket": ticket})
        elif kind == "sla_deadline":
            if ticket not in responses:
                engine.write("tickets", "sla_breach", {"case": case, "ticket": ticket,
                             "ticket_id": links["tickets"][case][ticket], "due": engine.now})
        elif kind == "staff_unavailable":
            technician = payload["technician"]
            if not isinstance(technician, str) or not technician.strip():
                raise ValueError("Expected a technician ID")
            unavailable[technician] = True
            engine.write("tickets", "staff_unavailable", {"case": case, "technician": technician})
        elif kind in ("respond", "reassign"):
            fields = {"case": case, "ticket": ticket, "ticket_id": links["tickets"][case][ticket]}
            if ticket in responses:
                return
            technician = payload.get("technician")
            if technician is not None or kind == "reassign":
                if not isinstance(technician, str) or not technician.strip():
                    raise ValueError("Expected a technician ID")
                fields["technician"] = technician
                if technician in unavailable:
                    engine.write("tickets", "response_blocked", {**fields, "reason": "staff_unavailable"})
                    return
            responses[ticket] = shared.native_id(engine.write("tickets", "ticket_response", fields))
        elif kind == "timesheet":
            ticket, hours, approved = payload["ticket"], payload["hours"], payload["approved"]
            if type(hours) not in (int, float) or not 0 < hours < float("inf") or type(approved) is not bool:
                raise ValueError("Timesheet requires positive hours and an approval status")
            engine.write("erp", "timesheet", {"case": case, "ticket": ticket,
                         "ticket_id": links["tickets"][case][ticket], "hours": hours,
                         "approved": approved})
        elif kind == "bill_period":
            self.bill(engine, case, links)
        elif kind == "payer_observed":
            self.observe_payment(engine, case, payload, links)
        elif kind == "post_payment":
            self.post_payment(engine, case, payload["reference"], links)
        else:
            raise ValueError(f"Unsupported managed-services event: {kind}")

    def currency(self, terms):
        return terms.get("currency", self.config.get("currency", "USD"))

    def bill(self, engine, case, links):
        contract = links["contracts"][case]
        invoices = links.setdefault("invoices", {})
        billed = {sheet for row in invoices.values() for sheet in row["timesheet_ids"]}
        tickets = links["tickets"].get(case, {}).values()
        sheets = [row for row in engine.export()["records"]
                  if row["system"] == "erp" and row["kind"] == "timesheet"
                  and row["case"] == case and row.get("contract_id", contract) == contract
                  and row["ticket_id"] in tickets and row["approved"]
                  and row["native_id"] not in billed]
        rate = Decimal(str(links["terms"][case]["rate"]))
        amount = sum((Decimal(str(row["hours"])) * rate).quantize(CENT, rounding=ROUND_HALF_UP)
                     for row in sheets)
        if amount <= 0:
            return
        invoice = dict(case=case, contract_id=contract,
                       timesheet_ids=[row["native_id"] for row in sheets],
                       currency=self.currency(links["terms"][case]), amount=float(amount),
                       due=engine.now + 3 * DAY)
        native_id = shared.native_id(engine.write("erp", "invoice", invoice))
        invoices[native_id] = {**invoice, "outstanding": invoice["amount"]}

    def observe_payment(self, engine, case, payload, links):
        if engine.event["role"] != "payer":
            raise PermissionError("Payment observation requires the payer role")
        reference, amount, currency = (payload[k] for k in ("reference", "amount", "currency"))
        if not isinstance(reference, str) or not reference or reference != reference.strip():
            raise ValueError("Payment requires a bank reference")
        if type(amount) not in (int, float) or not 0 < amount < float("inf"):
            raise ValueError("Payment requires a positive observed amount")
        invoices = links.get("invoices", {})
        observations = links.setdefault("payment_observations", {})
        prior = observations.get(reference)
        invoice_id = payload.get("invoice_id")
        if invoice_id is None and prior is not None:
            invoice_id = prior["receipt"]["invoice_id"]
        if invoice_id is None:
            candidates = [key for key, row in invoices.items() if row["case"] == case]
            if len(candidates) != 1:
                raise ValueError("Payment requires an unambiguous invoice ID")
            invoice_id = candidates[0]
        invoice = invoices[invoice_id]
        observed_amount = Decimal(str(amount))
        if (invoice["case"] != case or invoice["currency"] != currency
                or observed_amount != observed_amount.quantize(CENT)):
            raise ValueError("Payment case, currency or precision does not match invoice")
        receipt = dict(case=case, invoice_id=invoice_id, currency=currency, amount=amount,
                       reference=reference)
        if prior is not None:
            if prior["receipt"] != receipt:
                raise ValueError("Bank reference already belongs to another receipt")
            return
        observations[reference] = {"receipt": receipt, "at": engine.now}
        engine.schedule("service-payment:" + reference, "post_payment", engine.now, role="finance",
                        payload={"case": case, "reference": reference})

    def post_payment(self, engine, case, reference, links):
        if engine.event["role"] != "finance":
            raise PermissionError("Payment posting requires the Finance role")
        observation = links["payment_observations"][reference]
        receipt = observation["receipt"]
        if receipt["case"] != case:
            raise ValueError("Payment observation belongs to another case")
        if "payment_id" in observation:
            return
        invoice = links["invoices"][receipt["invoice_id"]]
        balance = Decimal(str(invoice["outstanding"])) - Decimal(str(receipt["amount"]))
        if balance < 0:
            raise ValueError("Payment exceeds the invoice balance")
        observation["payment_id"] = shared.native_id(engine.write("erp", "payment", {
            **receipt, "observed_at": observation["at"], "outstanding": float(balance)}))
        invoice["outstanding"] = float(balance)
