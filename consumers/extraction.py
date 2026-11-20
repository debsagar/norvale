"""Grade recorded billing and allocations using canonical native identities."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from fractions import Fraction


def measure(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def total(values):
    values = list(values)
    if None in values:
        return None
    # Sum fixed-point coefficients as integers so the Decimal context cannot round.
    scale = max((max(0, -value.as_tuple().exponent) for value in values), default=0)
    factor = 10 ** scale
    coefficient = int(sum(Fraction(value) for value in values) * factor)
    whole, remainder = divmod(coefficient, factor)
    if not remainder:
        return whole
    exact = f"{whole}.{remainder:0{scale}d}"
    number = float(exact)
    return number if Decimal(str(number)) == Decimal(exact) else exact


def grade(records, cutoff, *, source, unit, links, quantity):
    # Last confirmed version wins at the cutoff. Kind and system are part
    # of identity because independently stored types can issue the same ID.
    latest, first = {}, {}
    for row in sorted(records, key=lambda row: row["at"]):
        identity = row["kind"], row["system"], row["native_id"]
        first.setdefault(identity, row["at"])
        latest[identity] = row
    invoices = defaultdict(list)
    payments = defaultdict(list)
    for (kind, system, _), row in latest.items():
        if kind == "invoice":
            invoices[row["case"]].append(row)
        elif kind == "payment" and row.get("invoice_id"):
            payments[row.get("invoice_system", system), row["invoice_id"]].append(row)

    questions, answers = [], {}
    for case, bills in sorted(invoices.items()):
        currency = {bill.get("currency") for bill in bills}
        recipients = []
        quantities = []
        billed = set()
        units = set()
        for bill in bills:
            key = bill["system"], bill["native_id"]
            issued_at = first["invoice", *key]
            recipients.extend((bill, pay, issued_at) for pay in payments[key])
            ids = bill.get(links)
            if links == "shipment_id" and isinstance(ids, str):
                ids = [ids]
            if not isinstance(ids, list) or not ids:
                quantities.append(None)
                continue
            for native_id in ids:
                if not isinstance(native_id, str) or not native_id:
                    quantities.append(None)
                    continue
                target = (source, bill.get(f"{source}_system", bill["system"]), native_id)
                row = latest.get(target)
                valid = (row is not None and row["case"] == case and first[target] <= issued_at
                         and (source != "timesheet" or row.get("approved") is True)
                         and (bill.get("contract_id") is None or
                              row.get("contract_id", bill["contract_id"]) == bill["contract_id"]))
                value = measure(row.get(quantity)) if valid else None
                if valid:
                    units.add(row.get("uom"))
                # A second billing of the same source is ambiguous.
                quantities.append(None if target in billed else value)
                billed.add(target)

        amount = total(measure(row.get("amount")) for row in bills) if len(currency) == 1 else None
        paid = total(
            measure(pay.get("amount")) if pay.get("case", case) == case
            and pay.get("currency", bill.get("currency")) == bill.get("currency")
            and first["payment", pay["system"], pay["native_id"]] >= issued_at else None
            for bill, pay, issued_at in recipients
        ) if amount is not None and recipients else None

        for metric, description, answer in (
            ("invoiced", "What amount was invoiced", amount),
            (f"billed-{unit}", f"How many {unit} were billed from linked {source} records",
             total(quantities) if len(units) <= 1 else None),
            ("paid", "What amount was paid according to linked payment allocations", paid),
        ):
            key = f"{case}-{metric}"
            questions.append({"id": key, "question": f"{description} for {case} through {cutoff}?"})
            answers[key] = answer
    return questions, answers
