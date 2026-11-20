"""Pin the PLM definition at work release using independent ERP and MES readbacks."""
from copy import deepcopy
import json


def pin_work_revision(pins, work_ref, product, minute, revision, acknowledgements):
    """The caller supplies authenticated readbacks and checkpoints pins with work release.

    Times are simulated minutes. Pins use the originating system and native work ID.
    Replaying a release returns its original pin even after that revision expires.
    """
    if (len(work_ref) != 2 or not all(isinstance(x, str) and x.strip() for x in work_ref)
            or not isinstance(product, str) or not product.strip() or type(minute) is not int):
        raise ValueError("A native work reference, product and business minute are required")
    key = json.dumps(list(work_ref))
    identity = {field: revision.get(field) for field in ("id", "version", "product")}
    if (not all(isinstance(x, str) and x.strip() for x in identity.values())
            or identity["product"] != product):
        raise ValueError("PLM revision identity does not match the product")
    if key in pins:
        if any(pins[key][field] != value for field, value in identity.items()):
            raise ValueError("Work already pins another revision")
        return deepcopy(pins[key])
    start, end = revision.get("effective_at"), revision.get("superseded_at")
    if (revision.get("approved") is not True or type(start) is not int or start > minute
            or (end is not None and (type(end) is not int or minute >= end))):
        raise ValueError("PLM revision is not approved and effective at work release")
    links = {}
    for system in ("erp", "mes"):
        ack = acknowledgements.get(system) or {}
        if (ack.get("source_id") != identity["id"] or ack.get("version") != identity["version"]
                or ack.get("product") != product or type(ack.get("accepted_at")) is not int
                or ack["accepted_at"] > minute or not isinstance(ack.get("id"), str)
                or not ack["id"].strip()):
            raise ValueError(f"{system} has not acknowledged the work revision")
        links[system] = ack["id"]
    pins[key] = {**identity, "acknowledgements": links, "released_at": minute}
    return deepcopy(pins[key])
