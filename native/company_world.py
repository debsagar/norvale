"""Project the calendar's public events through the recurring native controller."""
from datetime import timedelta

from engine import DAY, world

BULLETIN_ROLES = ("BUYER", "SCM_MGR", "WAREHOUSE")
DELAY_ROLES = ("SCM_MGR", "WAREHOUSE")


def binding(config):
    if not config.get("world"):
        return None
    return {"seed": config["seed"], "fixture": config.get("world_fixture", {})}


def check_binding(config, saved):
    configured = binding(config)
    if configured != saved.get("world_binding") and (
            saved.get("completed") or saved.get("docs") or "world_binding" in saved):
        raise ValueError("World processes require the original fresh-run configuration")


def dispatch(company, key, submitted, promised):
    from native.recurring_extensions import COMPANY_OPEN, open_catalog

    if company.additions is None:
        company.additions = open_catalog(company.run, company.config, company.state,
                                         company.advance_clock, company=company)
    clock = company.additions
    # A late approval preserves the PO's original date in the notice while
    # allowing at least one calendar day for physical dispatch.
    minute = COMPANY_OPEN if "company_timing" in company.config else 720
    world.dispatch(clock, key, promised * DAY + minute,
                   {"key": key, "po": submitted["name"]},
                   earliest=(company.day + 1) * DAY + minute)


def publish(company, clock, key, kind, subject, text, recipients):
    from native.recurring_extensions import MIDNIGHT

    old_day, old_event = company.day, company.event
    try:
        company.day, company.event = clock.now // DAY, clock.event["id"]
        company.advance_clock(MIDNIGHT + timedelta(minutes=clock.now))
        date = company.dated(company.day)
        body = f"{date} — {text}"
        for role in recipients:
            mail_key = f"{key}:{role}"
            sender = "SCM_MGR" if role == "BUYER" else "BUYER"
            company.mail(mail_key, sender, role, subject, body)
            company.state.setdefault("world_observations", {})[mail_key] = {
                "kind": kind, "date": date, "text": body, "role": role,
                "message_id": company.state["mail_operations"][mail_key]["message_id"],
            }
        company.persist()
    finally:
        company.day, company.event = old_day, old_event


def evidence(company, role):
    visible = [row for row in company.state.get("world_observations", {}).values()
               if row["role"] == role and row["date"] <= company.date()]
    bulletins = [row for row in visible if row["kind"] == "world_bulletin"]
    delays = [row for row in visible if row["kind"] == "delay_notice"]
    return bulletins[-1:] + delays[-6:]


def arrival(company, clock, fields):
    key = fields["key"]
    purchase = company.state["ops"]["purchases"][key]
    # Persist the physical event before completing its queue edge. Receipt
    # remains a separate warehouse operation with its existing booking lag.
    due = clock.now // DAY
    old_day, old_event = company.day, company.event
    try:
        company.day, company.event = due, clock.event["id"]
        purchase.setdefault("arrival_due", due)
        company.record_supplier_arrival(key, purchase)
    finally:
        company.day, company.event = old_day, old_event
