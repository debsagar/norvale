"""Norvale sales and duration-aware production."""
from fractions import Fraction
import math
import json
from . import extraction, shared
from engine import world


def may_order(role, system, permissions):
    return ((system == "erp" and role == "sales")
            or f"write:{system}" in permissions or f"write:{system}:order" in permissions)


def number(value, *, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


class Consumer:
    def __init__(self, config):
        self.config = config

    def delivery_notice(self, engine, fields, eta):
        engine.write("mail", "delay_notice", {"case": fields["case"], "previous_eta": eta,
                     "text": "Carrier reports a delay. Revised delivery timing is pending.",
                     "roles": ["manager", "warehouse"]})

    def delivery_arrival(self, engine, fields):
        engine.write("erp", "receipt", fields)

    def grade_extraction(self, records, cutoff):
        return extraction.grade(records, cutoff,
                                source="shipment", unit="units", links="shipment_id", quantity="quantity")

    def handle(self, engine, kind, payload):
        if kind == "policy_review":
            return shared.review_policy(engine, self.config, payload)
        case = payload["case"]
        if not isinstance(case, str) or not case.strip():
            raise ValueError("Expected a nonblank case ID")
        if shared.handle(engine, self.config, kind, payload):
            return
        if kind == "dispatch_supply":
            quantity = payload["quantity"]
            if not number(quantity, positive=True):
                raise ValueError("Dispatch requires a positive quantity")
            world.dispatch(engine, case, payload["eta"], {"case": case, "quantity": quantity})
            return
        if kind == "production":
            reservations = engine.state.get("norvale_reservations", {})
            order_id = payload.get("order_id")
            source_system = payload.get("order_system", "erp")
            reservation_key = json.dumps([source_system, order_id])
            reservation = reservations.get(reservation_key)
            if reservation is None and source_system == "erp":
                reservation = reservations.get(order_id)  # Earlier single-ERP checkpoints.
            expected = {"case": case, "quantity": payload.get("quantity"),
                        "started_at": engine.event_started_at}
            if (not number(payload.get("quantity"), positive=True) or not reservation
                    or any(reservation[k] != v for k, v in expected.items())
                    or engine.event.get("sequence") != reservation["sequence"]
                    or engine.event.get("role") != "operator"
                    or engine.event.get("duration") != reservation["duration"]
                    or engine.now < reservation["started_at"] + reservation["duration"]
                    or engine.event.get("id") != reservation["event_id"]):
                raise ValueError("Production does not match its reservation")
            if reservation.get("completed"):
                return
            record = engine.write("erp", "production", {"case": case, "order_id": order_id, "order_system": source_system,
                                  "quantity": reservation["quantity"], "started_at": reservation["started_at"]})
            reservation["production_id"] = shared.native_id(record)
            reservation["completed"] = True
            return
        if kind != "order":
            raise ValueError(f"Unsupported Norvale event: {kind}")
        quantity, rate = payload["quantity"], payload["rate"]
        if not number(quantity, positive=True) or not number(rate):
            raise ValueError("Order requires positive quantity and nonnegative rate")
        production = "production_minutes" in self.config
        if production:
            duration = self.config["production_minutes"]
            capacity = self.config.get("daily_capacity")
            material = self.config.get("opening_material")
            if type(duration) is not int or duration <= 0 or not number(capacity, positive=True) or not number(material):
                raise ValueError("Invalid production configuration")
            shift_start = getattr(engine, "shift_start", None)
            if not callable(shift_start):
                raise RuntimeError("Public shift_start is required")
            find_start = getattr(engine, "available_start", shift_start)
            start = find_start(engine.now, "operator")
            if type(start) is not int or start < engine.now:
                raise ValueError("Invalid production shift start")
            event_id = engine.event.get("id")
            if not isinstance(event_id, str) or not event_id.strip():
                raise ValueError("Production requires a stable event ID")
            reservations = engine.state.get("norvale_reservations", {})
            if any(r["source_event"] == event_id for r in reservations.values()):
                raise ValueError("Duplicate order event")
            if sum(Fraction(str(r["quantity"])) for r in reservations.values()) + Fraction(str(quantity)) > Fraction(str(material)):
                raise ValueError("Insufficient material")
            if sum(Fraction(str(r["quantity"])) for r in reservations.values() if r["started_at"] // 1440 == start // 1440) + Fraction(str(quantity)) > Fraction(str(capacity)):
                raise ValueError("Daily production capacity exceeded")
        fields = {"case": case, "quantity": quantity, "rate": rate}
        system = "erp"
        if "product" in payload:
            product = engine.catalog["products"].get(payload["product"])
            if not product or not isinstance(product.get("item"), str) or not product["item"].strip():
                raise ValueError("Order requires a registered native product")
            system = product["system"]
            role = engine.event.get("role")
            permissions = engine.catalog["roles"].get(role, {}).get("permissions", [])
            if not may_order(role, system, permissions):
                raise ValueError("Order role lacks permission for this instance")
            fields["item"] = product["item"]
            if role != "sales":
                fields["order_role"] = role
        order_id = shared.native_id(engine.write(system, "order", fields))
        if production:
            completion_id = "production:" + event_id
            reservations = engine.state.setdefault("norvale_reservations", {})
            reservation_key = json.dumps([system, order_id])
            if reservation_key in reservations or (system == "erp" and order_id in reservations):
                raise ValueError("Duplicate native order ID")
            reservations[reservation_key] = {"case": case, "quantity": quantity, "started_at": start,
                                      "event_id": completion_id, "source_event": event_id, "duration": duration}
            reservations[reservation_key]["sequence"] = engine.schedule(completion_id, "production", start, role="operator", duration=duration, book_staff=True,
                            payload={"case": case, "order_id": order_id, "order_system": system, "quantity": quantity})
