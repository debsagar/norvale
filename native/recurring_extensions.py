"""Append-only software and scheduled sales for the recurring controller."""
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from time import monotonic
from urllib.error import URLError

from consumers.norvale import Consumer, may_order
from engine import DAY, DeferCompletion, Engine
from engine._extension import validate_event
from native.company_records import SYSTEM as SALES_READBACK, SOURCES, SalesRecords
from native.incremental_erp import order_dates, validate_orders
from native.production_profiles import ADAPTER, FACTORY, profile
from native.software_connections import connection, host_profile, sales_binding, validate_sales_roles
from native.system_transport import SystemTransport
from norvale.loader.erp_client import ErpError

EPOCH = date(2027, 1, 1)
MIDNIGHT = datetime.combine(EPOCH, time(), tzinfo=timezone.utc)
COMPANY_OPEN = 8 * 60
CORE_SYSTEMS = ('erp', 'tickets', 'mail', 'calendar', 'chat')


class RecurringSales(Consumer):
    def grade_extraction(self, records, cutoff):
        # Normalize the original single-ERP namespace for joins across an upgrade.
        normalized = []
        for row in records:
            if row['system'] == SALES_READBACK and row['kind'] in SOURCES:
                row = dict(row, system=SOURCES[row['kind']])
                if row['kind'] == 'invoice':
                    row['shipment_system'] = SOURCES['shipment']
                elif row['kind'] == 'payment':
                    row['invoice_system'] = SOURCES['invoice']
            normalized.append(row)
        return super().grade_extraction(normalized, cutoff)

    def publish_world_bulletin(self, clock, fields):
        if self.company is None:
            raise RuntimeError("World events require the recurring company controller")
        from native.company_world import BULLETIN_ROLES, publish
        publish(self.company, clock, clock.event["id"], "world_bulletin",
                "Carrier bulletin", fields["text"], BULLETIN_ROLES)

    def delivery_notice(self, clock, fields, eta):
        from native.company_world import DELAY_ROLES, publish
        promised = (EPOCH + timedelta(days=eta // DAY)).isoformat()
        publish(self.company, clock, clock.event["id"], "delay_notice", "Supplier delay notice",
                f"Carrier reports a delay for purchase order {fields['po']}, promised on {promised}. "
                "Revised delivery timing is pending.", DELAY_ROLES)

    def delivery_arrival(self, clock, fields):
        from native.company_world import arrival
        arrival(self.company, clock, fields)

    def __init__(self, config, advance_clock, company):
        super().__init__(config)
        self.advance_clock = advance_clock
        self.company = company

    def handle(self, engine, kind, payload):
        if kind == 'company-sales-readback':
            if engine.event.get('role') != 'FINANCE':
                raise PermissionError('Sales readback requires the Finance role')
            if self.company is None or not self.company.config.get('company_operating'):
                raise RuntimeError('Sales readback requires an operating company')
            return SalesRecords(self.company).capture(engine)
        if kind == 'company-native':
            handler = getattr(self.company, 'scheduled_company_event', None)
            if handler is None:
                raise RuntimeError('Run scheduled company work through integrations.company_run')
            return handler(engine, payload)
        self.advance_clock(MIDNIGHT + timedelta(minutes=engine.now))
        catalog = engine.catalog
        product = catalog['products'].get(payload.get('product'), {})
        system = product.get('system')
        spec = catalog['systems'].get(system, {})
        if kind == 'order' and spec.get('adapter') == ADAPTER:
            _, cfg = profile(self.company.run, system, spec)
            authorize(cfg, system, product, engine.event['role'], catalog['roles'], payload)
            day, delivery = order_dates(engine.now, EPOCH)
            self.company.admit_production_order(
                system, engine.event['id'], cfg, payload, day.isoformat(), delivery)
            return
        if kind == 'order' and spec.get('adapter') == 'host-connection':
            try:
                return super().handle(engine, kind, payload)
            except ErpError as exc:
                if exc.status not in (408, 429, 500, 502, 503, 504):
                    raise
                raise DeferCompletion() from exc
            except (URLError, TimeoutError, ConnectionError) as exc:
                raise DeferCompletion() from exc
        return super().handle(engine, kind, payload)


def open_catalog(run, config, state, advance_clock=None, *, company=None):
    # Both entry points import the same durable consumer identity. The stack
    # keeps its original path-derived project ID and JSON checkpoint.
    from native.company import ERP_ROLES, EnterpriseSimV4, ROLES

    namespace = sha256(str(run.resolve()).encode()).hexdigest()[:32]
    base = EnterpriseSimV4(seed=config['seed'], days=30, mode='dry-run')
    consumer = RecurringSales({
        'namespace': namespace,
        **({'world': True} if config.get('world') else {}),
        **({'world_fixture': config['world_fixture']} if 'world_fixture' in config else {}),
        'systems': {name: {} for name in CORE_SYSTEMS},
        'roles': {**{name: {} for name in {*ERP_ROLES, *(r['role_id'] for r in ROLES)}},
                  **{name: {'staff': count} for name, count in config.get('company_timing', {}).get('staff', {}).items()}},
        'products': {sku['sku_id']: {'system': 'erp', 'item': sku['sku_id']} for sku in base.skus},
        **({'shifts': config['company_timing']['shifts']} if 'company_timing' in config else {}),
    }, advance_clock, company)

    def adapter(name, spec):
        if spec['adapter'] == ADAPTER:
            return SystemTransport()
        _, transport = connection(namespace, name, {'connection': spec['connection']},
                                  expected=spec['target_hash'], epoch=EPOCH)
        transport.role = transport.role or spec.get('order_role')
        transport.catalog_roles = lambda: clock.catalog['roles']
        route = SystemTransport()
        route.bind(name, 'erp', transport)
        return route

    start = (0 if config.get('world') else
             state['day'] * DAY + (COMPANY_OPEN if 'company_timing' in config else 720))
    transport = SystemTransport()
    if company is not None and config.get('company_operating'):
        SalesRecords(company).bind(transport)
    clock = Engine(run/'scheduled-additions', consumer, seed=config['seed'], native=transport,
                  providers=None, monotonic=monotonic, adapters={'host-connection': adapter, ADAPTER: adapter},
                  start=start, standalone_extensions=False)
    return clock


def authorize(cfg, system, product, role, roles, payload):
    if cfg.get('material_kind') == 'intermediate':
        raise ValueError('Intermediate materials are manufactured against BOM demand')
    role_spec = roles.get(role, {})
    permissions = role_spec.get('permissions', []) if isinstance(role_spec, dict) else []
    if (product.get('item') != cfg['item'] or role not in cfg['order_roles']
            or not isinstance(permissions, list) or not may_order(role, system, permissions)):
        raise ValueError('Order requires an operator-approved role and scope for this production item')
    if not float(payload['quantity']).is_integer():
        raise ValueError('Production orders require whole stock units')


def submit(run, filename):
    from company import Engine as Company

    config_path = run/'config.json'
    if not config_path.is_file() or not (run/'env/site-created').is_file():
        raise ValueError('Extensions require a provisioned recurring run')
    config = json.loads(config_path.read_text())
    Company.validate_config(config)
    command = json.loads(filename.read_text())
    if not isinstance(command, dict) or 'id' not in command or set(command)-{
        'id', 'systems', 'roles', 'products', 'events'
    }:
        raise ValueError('Expected id and named catalog additions')
    sections = {key: command.get(key, {}) for key in ('systems', 'roles', 'products')}
    if any(not isinstance(section, dict) for section in sections.values()):
        raise ValueError('Catalog sections must be objects keyed by ID')
    events = command.get('events', [])
    if not isinstance(events, list):
        raise ValueError('events must be an array')
    state_path = run/'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'day': 0}
    catalog = open_catalog(run, config, state)
    current = catalog.catalog
    products = {**current['products'], **sections['products']}
    roles = {**current['roles'], **sections['roles']}
    systems = {*current['systems'], *sections['systems']} - set(CORE_SYSTEMS)
    namespace = catalog.config['namespace']
    production = {}
    for name, spec in sections['systems'].items():
        if name in CORE_SYSTEMS or name == SALES_READBACK or name in SOURCES.values():
            raise ValueError(f'Existing system: {name}')
        host = host_profile(spec)
        if isinstance(host, dict) and host.get('adapter') == ADAPTER:
            if config.get('company_authority') != FACTORY or 'company_erp' not in state:
                raise ValueError('Production extensions require a running native CompanyERP authority')
            fingerprint, cfg = profile(run, name, spec)
            production[name] = cfg
            sections['systems'][name] = {**spec, 'adapter': ADAPTER, 'target_hash': fingerprint}
        else:
            fingerprint, adapter = connection(namespace, name, spec, epoch=EPOCH)
            sections['systems'][name] = {**spec, 'adapter': 'host-connection', 'target_hash': fingerprint}
            sections['systems'][name].update(sales_binding(adapter, current['systems'].get(name)))

    def production_config(system):
        spec = current['systems'].get(system, {})
        if system in production:
            return production[system]
        if spec.get('adapter') == ADAPTER:
            return profile(run, system, spec)[1]
        return None

    for product in sections['products'].values():
        if not isinstance(product, dict) or not isinstance(product.get('system'), str):
            raise ValueError('Product requires a named system')
        cfg = production_config(product['system'])
        if cfg and product.get('item') != cfg['item']:
            raise ValueError('Product must match the operator-approved native item')

    for event in events:
        validate_event(event)
        payload = event['payload']
        product_id = payload.get('product')
        role = event.get('role')
        if (not isinstance(product_id, str) or not isinstance(role, str)
                or type(event['at']) is not int or not 0 <= event['at'] < 365*DAY
                or type(event.get('duration', 0)) is not int
                or not 0 <= event.get('duration', 0) <= 365*DAY):
            raise ValueError('Scheduled order requires a catalog product, role and supported minutes')
        product = products.get(product_id)
        role_spec = roles.get(role)
        system = product.get('system') if isinstance(product, dict) else None
        permissions = role_spec.get('permissions', []) if isinstance(role_spec, dict) else []
        if (not isinstance(system, str) or system not in systems
                or not isinstance(permissions, list) or not may_order(role, system, permissions)):
            raise ValueError('Order requires a connected product and a role authorized for that system')
        validate_orders([{**event, 'role': 'sales'}], epoch=EPOCH)

        cfg = production_config(system)
        if cfg:
            authorize(cfg, system, product, role, roles, payload)

    if production:
        from integrations.company_run.authority import Authority
        authority = Authority(run)
        for cfg in production.values():
            authority.verify_profile(cfg, as_of=(EPOCH + timedelta(days=state['day'])).isoformat(),
                                     company=Company.company)

    def validate(current):
        validate_sales_roles(current, {**sections, "events": events})
        peers = list(production.values())
        for system, spec in current['systems'].items():
            if production and spec.get('adapter') == ADAPTER:
                peers.append(profile(run, system, spec)[1])
        for i, cfg in enumerate(production.values()):
            for other in peers[i + 1:]:
                authority.check_sources(cfg, other)

    catalog.extend(command['id'], **sections, events=events, validate=validate)
