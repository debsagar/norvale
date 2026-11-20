"""Resolve run catalog entries against operator-controlled connection profiles."""
import hashlib
import json
import os
import re
from pathlib import Path

from native.incremental_erp import EPOCH, sales_transport


def host_profile(spec):
    if not isinstance(spec, dict) or set(spec) != {"connection"}:
        raise ValueError("A system must reference a named host connection")
    profile_id = spec["connection"]
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("A connection name is required")
    filename = os.environ.get("NORVALE_CONNECTIONS_FILE")
    if not filename:
        raise ValueError("Set NORVALE_CONNECTIONS_FILE to the host connection registry")
    profiles = json.loads(Path(filename).read_text())
    return profiles.get(profile_id)


def connection(namespace, name, spec, *, expected=None, epoch=EPOCH):
    profile = host_profile(spec)
    if not isinstance(profile, dict) or set(profile) - {"employee"} != {"adapter", "erp", "token_env"}:
        raise ValueError(f"Missing or invalid host connection {spec['connection']!r}")
    if profile["adapter"] != "erpnext-sales" or not isinstance(profile["token_env"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", profile["token_env"]):
        raise ValueError("Unsupported host adapter or token variable")
    employee = profile.get("employee")
    if "employee" in profile and (
        not isinstance(employee, dict) or set(employee) != {"role", "user"}
        or not isinstance(employee["role"], str) or not employee["role"].strip()
        or not isinstance(employee["user"], str) or not re.fullmatch(r"[^\s@]+@[^\s@]+", employee["user"])
    ):
        raise ValueError("An employee connection requires a scheduled role and native user email")
    fingerprint = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    if expected is not None and expected != fingerprint:
        raise ValueError(f"Connection target changed for {name!r}")
    instance = hashlib.sha256(json.dumps([namespace, name]).encode()).hexdigest()[:32]
    adapter = sales_transport(instance, {"consumer": "norvale", "config": {}, "events": [],
                                         "erp": profile["erp"]}, token_env=profile["token_env"], epoch=epoch, employee=employee)
    return fingerprint, adapter


def sales_binding(adapter, previous):
    # Preserve the original descriptor on command replay. Legacy descriptors
    # lacked order_role when the host used the default sales account.
    if adapter.role is None and previous is not None and "order_role" not in previous:
        return {}
    return {"order_role": adapter.role or "sales"}


def validate_sales_roles(catalog, additions):
    """Apply host identity policy to new events after exact replay reconciliation."""
    systems = {**catalog["systems"], **additions.get("systems", {})}
    products = {**catalog["products"], **additions.get("products", {})}
    for event in additions.get("events", []):
        product = products.get(event["payload"].get("product"))
        if product is None:
            continue
        system = product["system"]
        binding = systems.get(system, {})
        if system == "erp" or binding.get("adapter") == "host-connection":
            if event.get("role") != binding.get("order_role", "sales"):
                raise ValueError("Order role differs from the commissioned employee")
