"""InvenTree PLM adapter for the Norvale controller."""
import base64
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from integrations.revisions import valid_receiver_marker


class Blocked(RuntimeError):
    """The controller must retain or defer the work."""


def valid_material_lines(material_kind, lines):
    expected = {"finished": {"formulation", "packaging"}, "intermediate": {"formulation"}}
    return material_kind in expected and {row["kind"] for row in lines} == expected[material_kind]


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def required(value, max_length=None):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Expected a nonempty string without surrounding whitespace")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"Value exceeds native maximum of {max_length} characters")
    return value


def day(value):
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("Expected a YYYY-MM-DD business date")
    return value


def quantity(value):
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Invalid BOM quantity") from error
    if (not amount.is_finite() or amount <= 0 or not -5 <= amount.adjusted() < 10
            or amount != round(amount, 5)):
        raise ValueError("BOM quantity must fit InvenTree's positive decimal (15, 5)")
    return format(amount, ".5f").rstrip("0").rstrip(".")


class PinnedEndpoint(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Blocked("PLM redirected a request away from its configured endpoint")


class PLM:
    """Call through one serialized controller per InvenTree store."""

    systems = ("erp", "mes")

    def __init__(self, url, token, *, publisher, basic=False, decision_actor=None):
        self.url = url.rstrip("/")
        self.opener = build_opener(PinnedEndpoint())
        self.publisher = required(publisher)
        self.decision_actor = required(decision_actor) if decision_actor is not None else None
        self.auth = "Token " + token
        if basic:  # Used by isolated qualification to avoid provisioning API tokens.
            self.auth = "Basic " + base64.b64encode(token.encode()).decode()

    def request(self, path, method="GET", data=None, *, require_guard=False):
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data, allow_nan=False).encode()
        with self.opener.open(Request(self.url + path, body, headers, method=method), timeout=30) as reply:
            if require_guard and reply.headers.get("X-Norvale-PLM-Change-Guard") != "1":
                raise Blocked("InvenTree definition change permissions are not enabled")
            return json.load(reply)

    def _list(self, path, **filters):
        offset = 0
        while True:
            result = self.request(path + "?" + urlencode(dict(filters, limit=100, offset=offset)))
            if isinstance(result, list):
                yield from result
                return
            yield from result["results"]
            if not result["next"]:
                return
            offset += len(result["results"])

    def require_change_guard(self):
        self.request("/api/", require_guard=True)

    def _actor(self, expected):
        guarded = self.decision_actor is not None and expected != self.publisher
        user = (self.request("/api/user/me/", require_guard=True) if guarded
                else self.request("/api/user/me/"))
        if user["username"] != expected:
            raise Blocked("PLM publication requires the configured native identity")
        return user["username"]

    def _publisher(self):
        return self._actor(self.publisher)

    def _part(self, pk):
        return self.request(f"/api/part/{pk}/")

    def _meta(self, pk, update=None):
        path = f"/api/metadata/part/{pk}/"
        if update is not None:
            self.request(path, "PATCH", {"metadata": update})
        value = self.request(path)["metadata"]
        if update is not None and any(value.get(key) != item for key, item in update.items()):
            raise Blocked("PLM metadata write did not match readback")
        return value

    def _find_part(self, ipn):
        rows = list(self._list("/api/part/", IPN=ipn))
        if len(rows) > 1:
            raise Blocked(f"Ambiguous InvenTree IPN {ipn}")
        return rows[0] if rows else None

    def _ensure_part(self, fields):
        part = self._find_part(fields["IPN"])
        if part is None:
            result = self.request("/api/part/", "POST", fields)
            part = self._part(result["pk"])
        if any(part[key] != value for key, value in fields.items()):
            raise Blocked(f"Part does not match intent for {fields['IPN']}")
        return part["pk"]

    def _bom(self, pk):
        rows = []
        for row in self._list("/api/bom/", part=pk, substitutes="true", sub_part_detail="true"):
            if (row["part"] != pk or any(row[key] for key in
                ("optional", "consumable", "inherited", "allow_variants", "substitutes"))
                or Decimal(str(row["attrition"])) or Decimal(str(row["setup_quantity"]))
                or row["rounding_multiple"] is not None):
                raise Blocked(f"Unsupported BOM modifiers on row {row['pk']}")
            component = row["sub_part_detail"]
            if not component or component["pk"] != row["sub_part"]:
                raise Blocked(f"Missing native component for BOM row {row['pk']}")
            rows.append({
                "native_id": row["pk"], "component_id": component["pk"],
                "source_id": component["IPN"], "unit": component["units"],
                "kind": row["reference"], "quantity": quantity(row["quantity"]),
                "specification": row["note"],
            })
        return sorted(rows, key=lambda row: row["native_id"])

    def draft(self, product, revision, lines, *, base_id=None, material_kind="finished", proposal=None):
        """Ensure a complete draft. A retry must pass the same specification."""
        self._publisher()
        product, revision = required(product, 100), required(revision, 100)
        wanted = {}
        for row in lines:
            source = required(row["source_id"], 100)
            kind = row["kind"]
            if kind not in ("formulation", "packaging") or source in wanted:
                raise ValueError("Each BOM input must have a distinct source ID and a valid kind")
            wanted[source] = {
                "source_id": source, "kind": kind, "unit": required(row["unit"], 20),
                "quantity": quantity(row["quantity"]),
                "specification": required(row["specification"], 500),
            }
        if not valid_material_lines(material_kind, wanted.values()):
            raise ValueError("Finished definitions require formulation and packaging; intermediate requires formulation")
        if base_id is not None:
            base = self._part(base_id)
            if (base["name"] != product or base["revision_of"] is not None
                    or not base["IPN"].startswith("NV-PLM-")):
                raise Blocked("Revision base must be the original Part for this product")
        ipn = "NV-PLM-" + digest([product, revision])
        if base_id is None:
            for part in self._list("/api/part/", name_regex=f"^{re.escape(product)}$"):
                if (part["name"] == product and part["revision_of"] is None
                        and part["IPN"].startswith("NV-PLM-") and part["IPN"] != ipn):
                    raise Blocked("Supply the original Part ID for another revision")
        pk = self._ensure_part({
            "IPN": ipn, "name": product, "revision": revision, "revision_of": base_id,
            "description": "Norvale manufacturing definition", "assembly": True,
        })
        self._set_once(pk, "norvale_draft_sha256", digest(wanted))
        if proposal is not None:
            self._set_once(pk, "norvale_proposal", proposal)
        if material_kind == "intermediate":
            self._set_once(pk, "norvale_material_kind", material_kind)
        current = self._bom(pk)
        self._check_lines(current, wanted, complete=False)
        missing = wanted.keys() - {line["source_id"] for line in current}
        if missing and (self._part(pk)["locked"] or self._meta(pk).get("norvale_review")):
            raise Blocked("Reviewed definitions cannot be repaired by changing their BOM")
        for source in sorted(missing):
            line = wanted[source]
            component = self._ensure_part({
                "IPN": source, "name": source, "units": line["unit"], "assembly": False,
            })
            self.request("/api/bom/", "POST", {
                "part": pk, "sub_part": component, "reference": line["kind"],
                "quantity": line["quantity"], "note": line["specification"],
            })
        self._check_lines(self._bom(pk), wanted, complete=True)
        return pk

    @staticmethod
    def _check_lines(lines, wanted, *, complete):
        actual = {line["source_id"]: {key: line[key] for key in wanted[line["source_id"]]}
                  for line in lines if line["source_id"] in wanted}
        if (len(actual) != len(lines) or any(wanted[key] != value for key, value in actual.items())
                or (complete and actual != wanted)):
            raise Blocked("InvenTree BOM differs from the draft intent")

    def definition(self, pk):
        part = self._part(pk)
        fields = {key: part[key] for key in ("pk", "name", "IPN", "revision", "revision_of")}
        fields["lines"] = self._bom(pk)
        meta = self._meta(pk)
        if "norvale_proposal" in meta:
            fields["proposal"] = meta["norvale_proposal"]
        material_kind = meta.get("norvale_material_kind", "finished")
        if material_kind == "intermediate":
            fields["material_kind"] = material_kind
        if (not part["assembly"] or part["IPN"] != "NV-PLM-" + digest([part["name"], part["revision"]])
                or not valid_material_lines(material_kind, fields["lines"])):
            raise Blocked("Part is not a complete Norvale definition")
        intended = meta.get("norvale_draft_sha256")
        observed = {line["source_id"]: {key: value for key, value in line.items()
                                       if key not in ("native_id", "component_id")}
                    for line in fields["lines"]}
        if len(observed) != len(fields["lines"]) or intended != digest(observed):
            raise Blocked("Native BOM does not match its recorded draft intent")
        return fields

    def _set_once(self, pk, key, value):
        existing = self._meta(pk).get(key)
        if existing is not None and existing != value:
            raise Blocked(f"Conflicting PLM replay for {key}")
        if existing is None:
            self._meta(pk, {key: value})

    def review(self, pk, *, on, evidence):
        actor = self._actor(self.decision_actor or self.publisher)
        record = {"on": day(on), "evidence": required(evidence), "actor": actor,
                  "sha256": digest(self.definition(pk))}
        self._set_once(pk, "norvale_review", record)
        return record

    def approve(self, pk, *, on, effective, evidence):
        actor = self._actor(self.decision_actor or self.publisher)
        on, effective = day(on), day(effective)
        definition = self.definition(pk)
        checksum = digest(definition)
        meta = self._meta(pk)
        review = meta.get("norvale_review")
        if (not review or review["sha256"] != checksum or not review["on"] <= on <= effective
                or (self.decision_actor and review["actor"] == actor)
                or meta.get("norvale_rejection")):
            raise Blocked("Approval requires reviewed content and ordered business dates")
        approval = {"on": on, "effective": effective, "evidence": required(evidence),
                    "actor": actor, "sha256": checksum}
        proposal = definition.get("proposal")
        if proposal and not meta.get("norvale_approval"):
            base = self.pin(definition["revision_of"], "approval:" + str(pk), on=on)
            if (proposal["on"] > review["on"] or
                    (base["native_id"], base["source_version"]) !=
                    (proposal["base_id"], proposal["base_version"])):
                raise Blocked("Proposal is stale or review predates its source evidence")
        for part in self._family(definition["revision_of"] or pk):
            prior = self._meta(part["pk"]).get("norvale_approval")
            if prior and part["pk"] != pk:
                if prior["effective"] == effective:
                    raise Blocked("Two approved revisions share the same effective date")
                if proposal and not meta.get("norvale_approval") and prior["effective"] > on:
                    raise Blocked("Wait for the approved successor to become effective before approving changes")
        if not self._part(pk)["locked"]:
            self.request(f"/api/part/{pk}/", "PATCH", {"locked": True})
        if not self._part(pk)["locked"] or digest(self.definition(pk)) != checksum:
            raise Blocked("InvenTree revision failed lock or content verification")
        self._set_once(pk, "norvale_approval", approval)
        return self.publication(pk)

    def reject(self, pk, *, on, evidence):
        actor = self._actor(self.decision_actor or self.publisher)
        on = day(on)
        checksum = digest(self.definition(pk))
        meta = self._meta(pk)
        review = meta.get("norvale_review")
        if (not review or review["sha256"] != checksum or review["on"] > on
                or actor == review["actor"] or meta.get("norvale_approval")):
            raise Blocked("Rejection requires independent review of the current undecided draft")
        record = {"on": on, "evidence": required(evidence), "actor": actor, "sha256": checksum}
        self._set_once(pk, "norvale_rejection", record)
        return record

    def publication(self, pk):
        definition = self.definition(pk)
        meta = self._meta(pk)
        approval = meta.get("norvale_approval")
        review = meta.get("norvale_review")
        if (not approval or meta.get("norvale_rejection") or approval["sha256"] != digest(definition)
                or not self._part(pk)["locked"]):
            raise Blocked("PLM revision is unapproved or its approved content changed")
        if "proposal" in definition and (not review or review["actor"] == approval["actor"]
                                         or review["sha256"] != approval["sha256"]):
            raise Blocked("Change publication requires an independent review of the approved content")
        return {"source_system": "plm", "native_id": pk, "native_ref": f"/api/part/{pk}/",
                "source_version": approval["sha256"], "definition": definition, "approval": approval}

    def publish_to(self, pk, system, *, on, ensure_target, read_target, scoped=False):
        """Deliver an approved definition and store the verified native receipt.

        ensure_target(publication, retry_id) reconciles or writes the native
        receiver import and returns its ID. The callback owns native deduplication.
        """
        self._publisher()
        if system not in self.systems:
            raise ValueError("Only ERP and MES are PLM receivers")
        publication = self.publication(pk)
        if day(on) < publication["approval"]["on"]:
            raise Blocked("Cannot publish before approval")
        existing = self._meta(pk).get(f"norvale_ack_{system}")
        if existing:
            target_id = existing["native_id"]
            on = existing["on"]
        else:
            retry_id = digest([system, pk, publication["source_version"]])
            target_id = ensure_target(publication, retry_id)
        return self.acknowledge(pk, system, target_id, on=on, read_target=read_target, scoped=scoped)

    def acknowledge(self, pk, system, target_id, *, on, read_target, scoped=False):
        """Verify a receiver's native record via its adapter before recording the receipt.

        read_target(target_id) returns the verified native values normalized to
        native_id, plm_native_id and plm_source_version. For a scoped receipt
        the callback also accepts scoped=True and returns receiver_marker.
        """
        self._publisher()
        if system not in self.systems:
            raise ValueError("Only ERP and MES are PLM receivers")
        target_id = required(target_id)
        publication = self.publication(pk)
        on = day(on)
        expected = {"native_id": target_id, "plm_native_id": pk,
                    "plm_source_version": publication["source_version"]}
        key = f"norvale_ack_{system}"
        existing = self._meta(pk).get(key)
        qualified = "receiver_marker" in existing if existing is not None else scoped
        observed = read_target(target_id, scoped=True) if qualified else read_target(target_id)
        if on < publication["approval"]["on"] or any(
                observed.get(key) != value for key, value in expected.items()):
            raise Blocked(f"{system} readback has not confirmed the exact approved definition")
        record = {**expected, "on": on}
        if qualified:
            marker = observed.get("receiver_marker")
            if not valid_receiver_marker(system, marker):
                raise Blocked(f"{system} readback has no valid receiver identity")
            record["receiver_marker"] = marker
        self._set_once(pk, key, record)
        return record

    def _family(self, base_id):
        base = self._part(base_id)
        if base["revision_of"] is not None:
            raise Blocked("Use the original Part native ID when selecting a new revision")
        yield base
        yield from self._list("/api/part/", revision_of=base_id)

    def _receipts(self, publication, on):
        pk = publication["native_id"]
        meta = self._meta(pk)
        receipts = {}
        for system in self.systems:
            receipt = meta.get(f"norvale_ack_{system}")
            if (not receipt or receipt["on"] > on or receipt["plm_native_id"] != pk
                    or receipt["plm_source_version"] != publication["source_version"]):
                raise Blocked(f"PLM revision {pk} is awaiting {system} acknowledgment")
            if "receiver_marker" in receipt and not valid_receiver_marker(system, receipt["receiver_marker"]):
                raise Blocked(f"PLM revision {pk} has an invalid {system} receiver identity")
            receipts[system] = receipt
        return receipts

    def pin(self, base_id, work_id, *, on):
        """Select at release; persist the returned JSON in the existing checkpoint."""
        on = day(on)
        candidates = []
        for part in self._family(base_id):
            approval = self._meta(part["pk"]).get("norvale_approval")
            if approval and approval["on"] <= on and approval["effective"] <= on:
                candidates.append((approval["effective"], part["pk"]))
        if not candidates:
            raise Blocked("No approved effective revision")
        candidates.sort()
        if len(candidates) > 1 and candidates[-2][0] == candidates[-1][0]:
            raise Blocked("Multiple revisions became effective on the same date")
        chosen = self.publication(candidates[-1][1])
        return {"work_id": required(work_id), "released_on": on, "native_id": chosen["native_id"],
                "source_version": chosen["source_version"], **self._receiver_pin(chosen, on)}

    def _receiver_pin(self, publication, on):
        receipts = self._receipts(publication, on)
        markers = {system: row["receiver_marker"] for system, row in receipts.items()
                   if "receiver_marker" in row}
        return {"receiver_ids": {system: row["native_id"] for system, row in receipts.items()},
                **({"receiver_markers": markers} if markers else {})}

    def resolve_pin(self, pin, *, work_id, on):
        """Read and verify the explicit native ID without reselecting current revision."""
        on = day(on)
        if pin["work_id"] != work_id or on < pin["released_on"]:
            raise Blocked("PLM pin belongs to different work or a later release")
        publication = self.publication(pin["native_id"])
        if (publication["source_version"] != pin["source_version"]
                or publication["approval"]["effective"] > pin["released_on"]):
            raise Blocked("Pinned revision failed identity or effectivity verification")
        receivers = {key: pin[key] for key in ("receiver_ids", "receiver_markers") if key in pin}
        if self._receiver_pin(publication, pin["released_on"]) != receivers:
            raise Blocked("Pinned revision receiver identities changed after release")
        return publication

    def change_events(self, base_id, *, on):
        """Observable, dated actions on employee-proposed revisions in one native family."""
        on = day(on)
        for part in self._family(base_id):
            meta = self._meta(part['pk'])
            proposal = meta.get('norvale_proposal')
            if not proposal or day(proposal['on']) > on:
                continue
            evidence = {k: proposal[k] for k in ('doctype', 'id', 'version', 'on', 'base_id', 'base_version')}
            for stage in ('proposal', 'review', 'approval', 'rejection'):
                event = meta.get('norvale_' + stage)
                if not event or day(event['on']) > on:
                    continue
                row = dict(name=f"{part['pk']}:{stage}", part_id=part['pk'], family_id=base_id,
                           revision=part['revision'], stage=stage, on=event['on'], proposal=evidence)
                if stage != 'proposal':
                    row.update({k: event[k] for k in ('actor', 'evidence', 'sha256')})
                if stage == 'approval':
                    row['effective_on'] = event['effective']
                yield row

    def export(self, base_id, *, on):
        """Observable approved definitions and dated receipts from native APIs."""
        on = day(on)
        records = []
        for part in self._family(base_id):
            pk = part["pk"]
            meta = self._meta(pk)
            approval = meta.get("norvale_approval")
            if approval and approval["on"] <= on:
                record = self.publication(pk)
                record["review"] = meta["norvale_review"]
                record["acknowledgments"] = {
                    system: receipt for system in self.systems
                    if (receipt := meta.get(f"norvale_ack_{system}")) and receipt["on"] <= on
                }
                records.append(record)
        return sorted(records, key=lambda record: record["native_id"])
