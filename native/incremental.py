"""Opt-in event runner over the integrated engine and company consumers."""
import argparse
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from consumers.norvale import may_order
from engine import Engine
from norvale.loader.erp_client import ErpError


CONSUMERS = ("norvale", "managed_services")
ROOT = Path(__file__).resolve().parents[1]


def main(argv=None, *, transport_factory=None):
    parser = argparse.ArgumentParser(prog="generate.py incremental")
    parser.add_argument("--run", type=Path, required=True, help="Distinct event run directory")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--extend", type=Path, help="Apply an addition to an existing run")
    source.add_argument("--plan", type=Path, help="JSON plan required only for a new run")
    parser.add_argument("--until", type=int, required=True, help="Inclusive business minute")
    args = parser.parse_args(argv)
    try:
        result = execute(args.run, args.plan, args.until, transport_factory, extension_path=args.extend)
    except ErpError as exc:
        parser.exit(1, f"{parser.prog}: ERP request failed (HTTP {exc.status})\n")
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")
    print(json.dumps(result, indent=2))
    return result


def execute(directory, plan_path, until, transport_factory=None, *, extension_path=None):
    directory = directory.resolve()
    if directory.is_relative_to(ROOT / "data") or until < 0:
        raise ValueError("Choose a run outside data/ and a nonnegative cutoff")
    manifest = directory / "incremental.json"
    if plan_path is None:
        if not manifest.is_file() or not (directory / "engine.sqlite3").is_file():
            raise ValueError("Resume requires an existing incremental run and checkpoint")
        identity = json.loads(manifest.read_text())
    else:
        if directory.exists():
            raise ValueError("A new incremental run requires a path that does not exist")
        identity = {"namespace": uuid4().hex, "plan": json.loads(plan_path.read_text()),
                    "transport": "injected" if transport_factory else "erpnext-sales"}
    mode = "injected" if transport_factory else "erpnext-sales"
    if identity["transport"] != mode:
        raise ValueError("Resume requires the original transport mode")
    plan = identity["plan"]
    validate(plan)
    if transport_factory is None:
        from native.incremental_erp import sales_transport
        transport_factory = sales_transport
    native = transport_factory(identity["namespace"], plan)
    if mode == "erpnext-sales":
        from native.system_transport import SystemTransport
        transport = SystemTransport()
        transport.bind("erp", "erp", native)
        native = transport
    consumer = importlib.import_module("consumers." + plan["consumer"]).Consumer(plan["config"])
    # The engine persists config and checks it on reopen. Bind the entire plan
    # and namespace to the checkpoint before any event is eligible to run.
    consumer.config = {**consumer.config, "_incremental_identity": identity}

    def open_connection(name, spec):
        from native.software_connections import connection
        _, adapter = connection(identity["namespace"], name, {"connection": spec["connection"]},
                                expected=spec["target_hash"])
        adapter.role = adapter.role or spec.get("order_role")
        adapter.catalog_roles = lambda: engine.catalog["roles"]
        route = SystemTransport()
        route.bind(name, "erp", adapter)
        return route

    def open_engine(path):
        return Engine(path, consumer, seed=plan["seed"], native=native,
                      providers=None, monotonic=monotonic, standalone_extensions=False,
                      adapters={"host-connection": open_connection} if mode == "erpnext-sales" else {})

    if plan_path is not None:
        directory.parent.mkdir(parents=True, exist_ok=True)
        # Publish the manifest and fully seeded queue together. No writes run
        # before publication and a competing published directory is nonempty.
        with TemporaryDirectory(dir=directory.parent, prefix=".incremental-") as temp:
            staging = Path(temp) / "run"
            staging.mkdir(mode=0o700)
            engine = open_engine(staging)
            for event in plan["events"]:
                engine.schedule(**event)
            (staging / manifest.name).write_text(json.dumps(identity, allow_nan=False))
            staging.rename(directory)
    engine = open_engine(directory)
    if until < engine.now:
        raise ValueError("Cutoff precedes the latest processed edge")
    if mode == "erpnext-sales":
        from native.software_connections import connection, sales_binding, validate_sales_roles
    if extension_path is not None:
        extension = json.loads(extension_path.read_text())
        if (not isinstance(extension, dict) or not isinstance(extension.get("id"), str)
                or not extension["id"].strip()
                or set(extension) - {"id", "systems", "roles", "products", "events"}):
            raise ValueError("An extension requires an ID and named additions")
        addition_plan = {**plan, "events": extension.get("events", [])}
        validate(addition_plan)
        if mode == "erpnext-sales":
            catalog = engine.catalog
            roles = {**catalog["roles"], **extension.get("roles", {})}
            products = {**catalog["products"], **extension.get("products", {})}
            systems = {"erp", *catalog["systems"], *extension.get("systems", {})}
            for name, spec in extension.get("systems", {}).items():
                if name == "erp":
                    raise ValueError("The initial ERP instance already exists")
                fingerprint, adapter = connection(identity["namespace"], name, spec)
                extension["systems"][name] = {**spec, "adapter": "host-connection", "target_hash": fingerprint}
                extension["systems"][name].update(sales_binding(adapter, catalog["systems"].get(name)))
            native_events = []
            for event in addition_plan["events"]:
                if "product" in event["payload"]:
                    product = products.get(event["payload"]["product"], {})
                    system = product.get("system")
                    if system not in systems or not isinstance(product.get("item"), str) or not product["item"].strip():
                        raise ValueError("Unresolved catalog product in order")
                    role = event.get("role")
                    if not may_order(role, system, roles.get(role, {}).get("permissions", [])):
                        raise ValueError("Role lacks order permission for product instance")
                    # The native connector enforces its Sales User identity independently.
                    event = {**event, "role": "sales"}
                native_events.append(event)
            sales_transport(identity["namespace"], {**addition_plan, "events": native_events})
        check = (lambda current: validate_sales_roles(current, extension)) if mode == "erpnext-sales" else None
        engine.extend(extension.pop("id"), validate=check, **extension)
    engine.run(until=until)
    return engine.export()


def validate(plan):
    if (not isinstance(plan, dict)
            or set(plan) - {"consumer", "seed", "config", "events", "erp"}
            or plan.get("consumer") not in CONSUMERS
            or type(plan.get("seed")) is not int
            or not isinstance(plan.get("config"), dict)
            or "_incremental_identity" in plan["config"]
            or not isinstance(plan.get("events"), list)):
        raise ValueError("Plan requires consumer, integer seed, config object and events array")
    ids = set()
    for event in plan["events"]:
        if (not isinstance(event, dict)
                or set(event) - {"id", "kind", "at", "payload", "duration", "priority", "role"}
                or any(not isinstance(event.get(key), str) or not event[key].strip()
                       for key in ("id", "kind"))
                or not isinstance(event.get("payload"), dict)
                or type(event.get("at")) is not int
                or (event.get("role") is not None and not isinstance(event["role"], str))
                or event["id"] in ids):
            raise ValueError("Events require unique ids, kind, integer at and object payload")
        ids.add(event["id"])
