"""Validation for append-only catalog entries and extension commands."""

import json
from pathlib import Path
import re
from urllib.parse import urlsplit


SECTIONS = ("systems", "roles", "products")
BUILTIN_SPECS = json.loads(Path(__file__).with_name("adapter_specs.json").read_text())


def name(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def validate_json(value):
    """Reject non-string object keys before JSON serialization can coerce them."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Extension object keys must be strings")
            validate_json(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_json(item)


def validate_system(spec, adapters):
    adapter = spec.get("adapter")
    if not name(adapter):
        raise ValueError("System requires a named adapter")
    schema = BUILTIN_SPECS.get(adapter)
    if schema is None:
        if not callable(adapters.get(adapter)):
            raise ValueError("Extension adapter must be registered by the trusted host")
        return
    settings, credential = schema["settings"], schema["credential"]
    target = spec.get(settings)
    token_env = spec.get(credential)
    if (set(spec) != {"adapter", settings, credential}
            or not isinstance(target, dict)
            or set(target) != set(schema["fields"])
            or not all(map(name, target.values()))
            or not isinstance(token_env, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env)):
        raise ValueError(f"{adapter} requires connection fields and a token environment name")
    origin = target[schema["origin"]]
    url = urlsplit(origin)
    if (any(char.isspace() or ord(char) < 32 for char in origin)
            or url.scheme not in ("http", "https") or not url.hostname
            or url.path not in ("", "/") or url.username is not None
            or url.password is not None or url.query or url.fragment
            or url.port == 0):
        raise ValueError("Adapter url must be an HTTP(S) origin")


def validate_entries(entries, catalog, calendars, adapters):
    for section in SECTIONS:
        mapping = entries[section]
        if not isinstance(mapping, dict):
            raise ValueError(f"{section} must be an object keyed by ID")
        reserved = set(catalog[section])
        if section == "roles":
            reserved.update(calendars)
        for key, spec in mapping.items():
            if not name(key) or not isinstance(spec, dict):
                raise ValueError(f"{section} requires named object entries")
            if key in reserved:
                raise ValueError(f"Existing {section} entry: {key!r}")
            if section == "systems":
                if ":" in key:
                    raise ValueError("System IDs cannot contain the permission scope delimiter")
                validate_system(spec, adapters)
            elif section == "roles":
                permissions = spec.get("permissions", [])
                if (set(spec) - {"permissions", "shifts", "staff"} or "shifts" not in spec
                        or not isinstance(permissions, list) or not all(map(name, permissions))):
                    raise ValueError("Role requires shifts and may specify permission scopes")
            elif (set(spec) != {"system", "item"} or not all(map(name, spec.values()))
                  or (spec["system"] not in catalog["systems"]
                      and spec["system"] not in entries["systems"])):
                raise ValueError("Product requires an existing system and a native item ID")


def validate_event(event):
    if (not isinstance(event, dict)
            or set(event) - {"id", "kind", "at", "payload", "priority", "duration", "role", "elapsed"}
            or not {"id", "kind", "at", "payload"} <= event.keys()
            or not name(event["id"]) or not name(event["kind"])
            or not isinstance(event["payload"], dict)):
        raise ValueError("Extension event requires id, kind, at and an object payload")
