"""Publish approved InvenTree revisions and dispatch work from durable pins."""

from contextlib import closing, contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
import re
import sqlite3


class RevisionMismatch(RuntimeError):
    """A source, mapping, or native record prevents release or dispatch."""


def require(condition, message):
    if not condition:
        raise RevisionMismatch(message)


def valid_receiver_marker(system, value):
    patterns = {"erp": r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}", "mes": r"[0-9a-f]{64}"}
    return system in patterns and isinstance(value, str) and re.fullmatch(patterns[system], value) is not None


def text(value):
    require(isinstance(value, str) and value and value == value.strip(),
            "Expected a nonempty identifier without surrounding whitespace")
    return value


def work_reference(value):
    value = text(value)
    require(len(value) <= 160 and "\n" not in value and "\r" not in value,
            "Work ID must fit the native Odoo reference")
    return value


def day(value):
    require(date.fromisoformat(value).isoformat() == value, "Expected YYYY-MM-DD")
    return value


def amount(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise RevisionMismatch("Expected a numeric quantity") from exc
    require(number.is_finite() and number > 0, "Expected a positive finite quantity")
    decimal = format(number, "f")
    return decimal.rstrip("0").rstrip(".") if "." in decimal else decimal


def wire_quantity(value):
    number = float(value)
    require(amount(number) == amount(value), "Quantity cannot be represented by the receiver")
    return number


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def line_map(rows):
    result = {row["item"]: row for row in rows}
    require(len(result) == len(rows), "Duplicate destination BOM item")
    return result


def project(publication, mapping, system):
    definition = publication["definition"]
    product = text(definition["name"])
    require(product in mapping, f"No output mapping for {product}")
    config = mapping[product][system]
    output = config["output"]
    output = {"item": output["item"], "unit": output["unit"], "qty": amount(output["qty"])}
    components = config["components"]
    rows = []
    from integrations.plm import valid_material_lines
    require(valid_material_lines(definition.get("material_kind", "finished"), definition["lines"]),
            "Revision line kinds differ from the approved material kind")
    for line in definition["lines"]:
        source = line["source_id"]
        require(source in components, f"Missing {system} component mapping for {source}")
        target = components[source]
        require((target["source_unit"], target["kind"]) == (line["unit"], line["kind"]),
                f"{system} unit or component kind mismatch for {source}")
        quantity = Decimal(amount(line["quantity"]))
        factor = Decimal(amount(target["factor"]))
        with localcontext() as context:
            context.prec = len(quantity.as_tuple().digits) + len(factor.as_tuple().digits)
            rows.append({"item": target["item"], "unit": target["unit"],
                         "qty": amount(quantity * factor)})
    items = line_map(rows)
    require(output["item"] not in items, "Output also appears as a component")
    return {"output": output, "lines": rows, "settings": config["settings"]}


class Bridge:
    """One controller per PLM store. Share its checkpoint across restarts."""

    def __init__(self, plm, erp, mes, *, source, mapping, checkpoint, scoped_receipts=False):
        self.plm = plm
        self.receivers = {"erp": erp, "mes": mes}
        self.source = text(source)
        self.mapping = mapping
        self.checkpoint = checkpoint
        require(type(scoped_receipts) is bool, "scoped_receipts must be a boolean")
        self.scoped_receipts = scoped_receipts
        require(str(checkpoint) not in ("", ":memory:"), "Use a durable SQLite checkpoint file")

    @contextmanager
    def _checkpoint(self):
        with closing(sqlite3.connect(self.checkpoint, timeout=60)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS revision_pin (
                source TEXT NOT NULL, work_id TEXT NOT NULL, record TEXT NOT NULL,
                PRIMARY KEY (source, work_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS revision_publication (
                source TEXT NOT NULL, native_id INTEGER NOT NULL, version TEXT NOT NULL,
                plans TEXT NOT NULL, evidence TEXT, PRIMARY KEY (source, native_id))""")
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _read_pin(self, db, work_id):
        row = db.execute("SELECT record FROM revision_pin WHERE source=? AND work_id=?",
                         (self.source, work_id)).fetchone()
        return json.loads(row[0]) if row else None

    def _plans(self, publication):
        return {system: project(publication, self.mapping, system) for system in self.receivers}

    def _sync(self, publication, plans, on, *, create):
        pk, version = publication["native_id"], publication["source_version"]
        observed = {}
        for system, receiver in self.receivers.items():
            plan = plans[system]
            marker = sha256(encode([self.source, system, pk, version]).encode()).hexdigest()

            def ensure(candidate, retry_id):
                require(create, f"Missing existing {system} acknowledgement")
                require((candidate["native_id"], candidate["source_version"]) == (pk, version),
                        "PLM publication changed during delivery")
                options = {"scoped": True} if self.scoped_receipts else {}
                return receiver.ensure(marker, plan, **options)

            def read(native_id, *, scoped=False):
                # The receiver checks its own stored import key and native content
                # before the callback attests to the PLM identity.
                options = {"scoped": True} if scoped else {}
                evidence = receiver.verify(native_id, marker, plan, **options)
                observed[system] = evidence
                return {"native_id": native_id, "plm_native_id": pk,
                        "plm_source_version": version,
                        **({"receiver_marker": evidence["receiver_marker"]} if scoped else {})}

            options = {"scoped": True} if self.scoped_receipts else {}
            receipt = self.plm.publish_to(pk, system, on=on, ensure_target=ensure, read_target=read, **options)
            observed[system]["receipt"] = receipt
        return observed

    def _publication_record(self, db, publication):
        row = db.execute("""SELECT version, plans, evidence FROM revision_publication
                            WHERE source=? AND native_id=?""",
                         (self.source, publication["native_id"])).fetchone()
        if row:
            require(row[0] == publication["source_version"], "Published PLM revision changed")
        return row

    def publish(self, native_id, *, on):
        """Reconcile both native BOMs and record their PLM acknowledgements."""
        on = day(on)
        publication = self.plm.publication(native_id)
        require(on >= publication["approval"]["on"], "Cannot publish before PLM approval")
        plans = self._plans(publication)
        # Keep the original destination definitions across partial imports.
        with self._checkpoint() as db:
            row = self._publication_record(db, publication)
            if row is None:
                for system, receiver in self.receivers.items():
                    receiver.validate(plans[system])
                db.execute("INSERT INTO revision_publication VALUES (?, ?, ?, ?, NULL)",
                           (self.source, native_id, publication["source_version"], encode(plans)))
            else:
                require(row[1] == encode(plans), "Conflicting destination mapping for this revision")
        with self._checkpoint() as db:
            row = self._publication_record(db, publication)
            observed = self._sync(publication, plans, on, create=row[2] is None)
            if row[2] is not None:
                require(encode(observed) == row[2], "Published receiver identity or fingerprint changed")
            else:
                db.execute("UPDATE revision_publication SET evidence=? WHERE source=? AND native_id=?",
                           (encode(observed), self.source, native_id))
            return observed

    def installed(self, pin, publication):
        """Verify existing native acknowledgements and BOM content without publishing."""
        verified = self.plm.resolve_pin(pin, work_id=pin["work_id"], on=pin["released_on"])
        require(verified == publication, "Installed PLM publication differs from the pin")
        plans = self._plans(publication)
        evidence = {}
        for system, receiver in self.receivers.items():
            native_id = pin["receiver_ids"][system]
            marker = sha256(encode([self.source, system, pin["native_id"],
                                   pin["source_version"]]).encode()).hexdigest()
            expected = pin.get("receiver_markers", {}).get(system)
            options = {"scoped": True} if expected is not None else {}
            actual = receiver.verify(native_id, marker, plans[system], **options)
            require(actual.get("receiver_marker") == expected, f"Installed {system} receiver identity differs")
            evidence[system] = {**actual, "receipt": {"native_id": native_id}}
        return plans, evidence

    def release(self, base_id, work_id, *, on, installed=False, pinned=None):
        """Persist the PLM pin and both verified receiver references before returning."""
        work_id, on = work_reference(work_id), day(on)
        with self._checkpoint() as db:
            saved = self._read_pin(db, work_id)
            if saved is not None:
                require((saved["base_id"], saved["pin"]["released_on"]) == (base_id, on),
                        "Work release conflicts with the existing pin")
                require(pinned is None or saved["pin"] == pinned, "ERP work differs from the saved revision pin")
                self._verify(saved, work_id, on)
                return saved
            pin = self.plm.pin(base_id, work_id, on=on) if pinned is None else pinned
            require(pin["released_on"] == on and pin["work_id"] == work_id,
                    "ERP work differs from the supplied revision pin")
            publication = self.plm.resolve_pin(pin, work_id=work_id, on=on)
            if pinned is not None:
                require((publication["definition"].get("revision_of") or publication["native_id"]) == base_id
                        and publication["source_version"] == pin["source_version"],
                        "Pinned revision belongs to a different family or version")
            row = self._publication_record(db, publication)
            if installed:
                plans, evidence = self.installed(pin, publication)
            else:
                require(row is not None and row[2] is not None, "Revision publication is incomplete")
                plans, evidence = json.loads(row[1]), json.loads(row[2])
            saved = {"base_id": base_id, "pin": pin, "plans": plans, "receivers": evidence}
            self._verify_receivers(publication, saved, message="Published receiver identity or fingerprint changed")
            db.execute("INSERT INTO revision_pin VALUES (?, ?, ?)",
                       (self.source, work_id, encode(saved)))
            return saved

    def _verify(self, saved, work_id, on):
        pin = saved["pin"]
        publication = self.plm.resolve_pin(pin, work_id=work_id, on=on)
        self._verify_receivers(publication, saved, message="Pinned receiver identity or fingerprint changed")

    def _verify_receivers(self, publication, saved, *, message):
        pin, plans, evidence = saved["pin"], saved["plans"], saved["receivers"]
        if "receiver_ids" not in pin:
            # Earlier PLM adapters confirm receipt dates and source versions in
            # resolve_pin. Re-read their receipt IDs via the publish callback.
            require(self._sync(publication, plans, pin["released_on"], create=False) == evidence, message)
            return
        for system, receiver in self.receivers.items():
            receipt = evidence[system]["receipt"]
            require(receipt["native_id"] == pin["receiver_ids"][system], message)
            expected = pin.get("receiver_markers", {}).get(system)
            require(evidence[system].get("receiver_marker") == expected
                    and receipt.get("receiver_marker", expected) == expected, message)
            options = {"scoped": True} if expected is not None else {}
            actual = receiver.verify(receipt["native_id"], evidence[system]["import_key"], plans[system], **options)
            require({**actual, "receipt": receipt} == evidence[system], message)

    def dispatch(self, work_id, *, on, quantity, hold_location_id):
        """Dispatch the pinned Odoo BOM using the native execution fingerprint."""
        work_id, on = work_reference(work_id), day(on)
        require(type(hold_location_id) is int and hold_location_id > 0,
                "Expected a positive Odoo hold location ID")
        qty = wire_quantity(quantity)
        with self._checkpoint() as db:
            saved = self._read_pin(db, work_id)
            require(saved is not None, "Work has no durable release pin")
            self._verify(saved, work_id, on)
            mes = saved["receivers"]["mes"]
            return self.receivers["mes"].client.dispatch(
                work_id, saved["plans"]["mes"]["output"]["item"], mes["bom_id"],
                mes["revision"], qty, hold_location_id)
