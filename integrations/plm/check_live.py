"""Verify the PLM adapter against a fresh or restarted isolated InvenTree instance."""
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError

from integrations.plm import Blocked, PLM


def denied(call, exception=Blocked, *, status=None):
    try:
        call()
    except exception as error:
        if status is not None:
            assert error.code == status, error
    else:
        raise AssertionError("Expected denial")


def client(role):
    name = f"norvale-{role}"
    password = os.environ[f"PLM_{role.upper()}_PASSWORD"]
    url = os.environ["PLM_URL"]
    login = PLM(url, f"{name}:{password}", publisher="norvale-publisher", basic=True)
    token = login.request("/api/user/token/?name=norvale-plm-check")["token"]
    return PLM(url, token, publisher="norvale-publisher")


def lines(package):
    return [
        {"source_id": "RAW-BASE", "unit": "kg", "kind": "formulation",
         "quantity": "0.25", "specification": "Base blend, homogenize before filling"},
        {"source_id": package, "unit": "ea", "kind": "packaging",
         "quantity": "1", "specification": "250 mL bottle, closure and label per drawing"},
    ]


def receipt(plm, pk, system, on):
    # Isolated native fixture inbox: two distinct InvenTree Part records act
    # as receiver readback evidence. Production integrations read ERP/MES IDs.
    def ensure(publication, retry_id):
        ref = f"inbox-{system}-{retry_id}"
        fields = {"name": ref, "IPN": ref, "assembly": False}
        target = plm._ensure_part(fields)
        plm._set_once(target, "imported_revision", {
            "plm_native_id": pk, "plm_source_version": publication["source_version"],
        })
        return str(target)

    return plm.publish_to(pk, system, on=on, ensure_target=ensure, read_target=receiver_read(plm))


def receiver_read(plm):
    def read(native_id):
        observed = plm._part(int(native_id))
        return {**plm._meta(observed["pk"])["imported_revision"], "native_id": str(observed["pk"])}

    return read


def loss_after_commit(plm, method, path, call):
    request = plm.request

    def lost_response(candidate, verb="GET", data=None):
        result = request(candidate, verb, data)
        if (verb, candidate) == (method, path):
            raise TimeoutError("qualification dropped reply after native commit")
        return result

    plm.request = lost_response
    try:
        denied(call, TimeoutError)
    finally:
        plm.request = request


def create(plm, product):
    d1, d2 = "2027-01-04", "2027-02-01"
    args = (product, "A", lines("PKG-A"))
    loss_after_commit(plm, "POST", "/api/part/", lambda: plm.draft(*args))
    loss_after_commit(plm, "POST", "/api/bom/", lambda: plm.draft(*args))
    a = plm.draft(*args)
    assert plm.draft(*args) == a
    denied(lambda: plm.draft(product, "A", lines("PKG-B")))
    denied(lambda: plm.approve(a, on=d1, effective=d1, evidence="ECN-001"))
    plm.review(a, on=d1, evidence="REV-001")
    approve_a = lambda: plm.approve(a, on=d1, effective=d1, evidence="ECN-001")
    loss_after_commit(plm, "PATCH", f"/api/metadata/part/{a}/", approve_a)
    first = approve_a()
    denied(lambda: plm.pin(a, "WO-EARLY", on=d1))
    erp_a = receipt(plm, a, "erp", d1)
    denied(lambda: plm.pin(a, "WO-EARLY", on=d1))
    mes_a = receipt(plm, a, "mes", d1)
    pin = plm.pin(a, "WO-EARLY", on=d1)
    assert pin["receiver_ids"] == {"erp": erp_a["native_id"], "mes": mes_a["native_id"]}
    assert receipt(plm, a, "erp", d1) == erp_a

    reader = client("reader")
    assert reader.request(f"/api/part/{a}/")["pk"] == a
    denied(lambda: reader.request(f"/api/part/{a}/", "PATCH", {"locked": False}), HTTPError, status=403)
    denied(lambda: reader.request(f"/api/metadata/part/{a}/", "PATCH",
                                  {"metadata": {"norvale_approval": None}}), HTTPError, status=403)
    denied(lambda: reader.approve(a, on=d1, effective=d1, evidence="ECN-001"))
    row = first["definition"]["lines"][0]["native_id"]
    denied(lambda: plm.request(f"/api/bom/{row}/", "PATCH", {"quantity": 2}), HTTPError, status=400)

    b = plm.draft(product, "B", lines("PKG-B"), base_id=a)
    plm.review(b, on=d1, evidence="REV-002")
    future = plm.approve(b, on=d1, effective=d2, evidence="ECN-002")
    assert plm.pin(a, "WO-BEFORE", on="2027-01-31")["native_id"] == a
    denied(lambda: plm.pin(a, "WO-AFTER", on=d2))
    assert plm.resolve_pin(pin, work_id="WO-EARLY", on=d2) == first
    denied(lambda: plm.acknowledge(b, "mes", mes_a["native_id"], on=d2,
                                 read_target=receiver_read(plm)))
    receipt(plm, b, "erp", d2)
    denied(lambda: plm.pin(a, "WO-AFTER", on=d2))
    receipt(plm, b, "mes", d2)
    new_pin = plm.pin(a, "WO-AFTER", on=d2)
    assert new_pin["native_id"] == b
    assert new_pin["source_version"] == future["source_version"]
    return {"product": product, "first": first, "old_pin": pin, "new_pin": new_pin, "export": plm.export(a, on=d2)}


def reopened(plm, state):
    d2 = "2027-02-01"
    first, old, new = (state[key] for key in ("first", "old_pin", "new_pin"))
    a, b = old["native_id"], new["native_id"]
    assert plm.resolve_pin(old, work_id="WO-EARLY", on=d2) == first
    assert plm.resolve_pin(new, work_id="WO-AFTER", on=d2)["native_id"] == b
    assert plm.draft(state["product"], "B", lines("PKG-B"), base_id=a) == b
    assert plm.pin(a, "WO-AFTER", on=d2) == new
    assert plm.export(a, on=d2) == state["export"]
    assert len(state["export"]) == 2
    for record, package in zip(state["export"], ("PKG-A", "PKG-B")):
        assert {row["source_id"] for row in record["definition"]["lines"]} == {"RAW-BASE", package}
        assert set(record["acknowledgments"]) == {"erp", "mes"}
    denied(lambda: plm.resolve_pin(old, work_id="WRONG", on=d2))


if __name__ == "__main__":
    mode, file = sys.argv[1:]
    path = Path(file)
    plm = client("publisher")
    if mode == "create":
        state = create(plm, os.environ.get("PLM_FIXTURE_PRODUCT", "FG-LOTION"))
        path.write_text(json.dumps(state, indent=2) + "\n")
    elif mode == "restart":
        state = json.loads(path.read_text())
        reopened(plm, state)
    else:
        raise ValueError("Use create or restart")
    print(f"{mode}: {len(state['export'])} revisions, old Part {state['old_pin']['native_id']}, "
          f"new Part {state['new_pin']['native_id']}")
