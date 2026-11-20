"""Resolve operator-approved production families without cataloguing credentials."""
from contextlib import closing
from hashlib import sha256
import json
import math
import os
import re
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from engine._extension import name
from native.software_connections import host_profile

ADAPTER = 'company-production'
FACTORY = 'integrations.company_run.authority:Authority'


def quality_target(cfg):
    reader = cfg['qms_reader']
    return (reader['url'].rstrip('/'), reader.get('site', 'qms.localhost'))



LAB_MODEL = 'norvale-quality-index-v1'


def recurring_lab(cfg, resolved):
    lab = cfg['recurring_lab']
    connections = ('analyst', 'approver', 'reference_writer')
    users = ('analyst_user', 'reference_user')
    labels = (*users, 'company', 'warehouse', 'procedure')
    if (not isinstance(lab, dict)
            or set(lab) != {*connections, *labels, 'model', 'assays'}
            or not all(name(lab[key]) for key in labels)
            or len({*(lab[key] for key in users), cfg['qms_approver']}) != 3
            or lab['model'] != LAB_MODEL):
        raise ValueError('Invalid recurring QMS lab scope')
    assays = lab['assays']
    components = cfg['mapping'].get(cfg['item'], {}).get('erp', {}).get('components', {})
    items = {cfg['item'], *(row['item'] for row in components.values())}
    if (not isinstance(assays, dict) or cfg['item'] not in assays
            or not assays.keys() <= (cfg['specifications'].keys() & items)):
        raise ValueError('Recurring assays must cover the product and only registered specifications')
    for item, assay in assays.items():
        if (not isinstance(assay, dict) or set(assay) != {'method', 'parameter', 'min', 'max'}
                or assay['method'] != cfg['specifications'][item] or not name(assay['parameter'])
                or any(type(assay[key]) not in (int, float) or not math.isfinite(assay[key])
                       for key in ('min', 'max')) or assay['min'] >= assay['max']):
            raise ValueError('Invalid recurring QMS assay')

    reader = cfg['qms_reader']
    seen = {resolved['qms_reader']['api_key']}
    result = dict(lab)
    for key in connections:
        target = lab[key]
        if (not isinstance(target, dict) or set(target) != {'api_key_env', 'api_secret_env'}
                or not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value)
                           for value in target.values())):
            raise ValueError('Invalid recurring QMS credential names')
        token = {field: os.environ.get(target[field + '_env']) for field in ('api_key', 'api_secret')}
        if not all(token.values()) or token['api_key'] in seen:
            raise ValueError('Recurring QMS requires separate available credentials')
        seen.add(token['api_key'])
        result[key] = {'url': reader['url'], 'site': reader['site'], **token}
    return result


def production_batch(policy):
    if (not isinstance(policy, dict) or set(policy) - {'target', 'backorder_not_before'}
            or type(policy.get('target')) not in (int, float)
            or not math.isfinite(policy['target']) or policy['target'] <= 0
            or ('backorder_not_before' in policy
                and (type(policy['backorder_not_before']) is not int or policy['backorder_not_before'] < 0))):
        raise ValueError('Invalid commissioned production batch policy')
    return policy


def profile(run, system, spec):
    cfg = host_profile({'connection': spec.get('connection')})
    optional = {'recurring_lab', 'qms_source', 'qms_dispatcher', 'material_kind', 'stock_step', 'customer', 'order_roles', 'maintenance', 'scoped_receipts', 'mes_operator', 'production_batch', 'physical_rework', 'mes_reader'}
    if (not isinstance(cfg, dict) or set(cfg) - optional != {
            'adapter', 'item', 'family', 'mapping',
            'source', 'hold_location_id', 'plm', 'erp', 'mes', 'qms_reader',
            'qms_approver', 'specifications', 'units'}
            or type(cfg.get('scoped_receipts', False)) is not bool
            or ('qms_source' in cfg and not name(cfg['qms_source']))
            or cfg['adapter'] != ADAPTER
            or not all(name(cfg[k]) for k in ('item', 'source', 'qms_approver'))
            or system != 'production-' + cfg['item']
            or any(type(cfg[k]) is not int or cfg[k] <= 0 for k in ('family', 'hold_location_id'))
            or any(not isinstance(cfg[k], dict) or not cfg[k]
                   for k in ('mapping', 'specifications', 'units'))
            or cfg['item'] not in cfg['specifications']
            or not all(name(key) and name(value) for k in ('specifications', 'units')
                       for key, value in cfg[k].items())):
        raise ValueError('Invalid operator production profile')
    if 'production_batch' in cfg:
        production_batch(cfg['production_batch'])
        if 'mes_operator' not in cfg:
            raise ValueError('Bounded production requires a commissioned MES operator')
    kind = cfg.get('material_kind', 'finished')
    step = cfg.get('stock_step')
    if (kind not in ('finished', 'intermediate')
            or (kind == 'intermediate' and (type(step) not in (int, float)
                                           or not math.isfinite(step) or step < 1e-8))
            or (kind == 'finished' and 'stock_step' in cfg)):
        raise ValueError('Invalid registered production material kind or stock increment')
    roles = cfg.get('order_roles', [])
    if (not isinstance(roles, list) or not all(map(name, roles))
            or ('customer' in cfg and not name(cfg['customer']))
            or (kind == 'finished' and (not roles or 'customer' not in cfg))):
        raise ValueError('Invalid registered production order scope')
    fingerprint = sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    if spec.get('target_hash', fingerprint) != fingerprint:
        raise ValueError(f'Production profile changed for {system}')
    if 'maintenance' in cfg:
        scope = cfg['maintenance']
        fields = {'login', 'password_env', 'ticket_url', 'ticket_repo', 'ticket_operator_env',
                  'technician_env', 'tester_env', 'verifier_env', 'reader_env'}
        if not isinstance(scope, dict) or set(scope) != fields or not all(map(name, scope.values())):
            raise ValueError('Invalid production maintenance scope')
        ticket_url = urlsplit(scope['ticket_url'])
        envs = {scope[key] for key in fields if key.endswith('_env')}
        if (ticket_url.scheme not in ('http', 'https') or not ticket_url.hostname
                or ticket_url.username or ticket_url.password or ticket_url.query or ticket_url.fragment
                or len(envs) != 6 or not all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env) for env in envs)
                or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*',
                                    scope['ticket_repo'])
                or (isinstance(cfg['mes'], dict) and scope['login'] == cfg['mes'].get('login'))):
            raise ValueError('Invalid production maintenance scope')
    resolved = dict(cfg)
    for client, fields, credentials in (
        ('plm', {'url', 'publisher'}, {'token'}),
        ('erp', {'base', 'site'}, {'token'}),
        ('mes', {'url', 'db', 'login', 'source_id'}, {'password'}),
        ('qms_reader', {'url', 'site'}, {'api_key', 'api_secret'}),
        ('qms_dispatcher', {'url', 'site'}, {'api_key', 'api_secret'}),
    ):
        if client == 'qms_dispatcher' and client not in cfg:
            continue
        target = cfg[client]
        expected = fields | {key + '_env' for key in credentials}
        if (not isinstance(target, dict) or set(target) != expected
                or not all(map(name, target.values()))):
            raise ValueError(f'Invalid production {client} connection')
        resolved[client] = {key: target[key] for key in fields}
        for key in credentials:
            value = os.environ.get(target[key + '_env'])
            if not value:
                raise ValueError(f'Missing production {client} credential environment variable')
            resolved[client][key] = value
    if 'mes_operator' in cfg:
        operator = cfg['mes_operator']
        if (not isinstance(operator, dict) or set(operator) != {'login', 'password_env'}
                or not all(map(name, operator.values()))
                or any(operator[k] == cfg['mes'][k] for k in operator)
                or operator['login'] == cfg.get('maintenance', {}).get('login')):
            raise ValueError('Invalid scoped MES operator')
        password = os.environ.get(operator['password_env'])
        if not password:
            raise ValueError('Missing scoped MES operator credential environment variable')
        resolved['mes_operator'] = dict(resolved['mes'], login=operator['login'], password=password)
    if 'physical_rework' in cfg:
        bridge = cfg['physical_rework']
        other_accounts = [cfg[key] for key in ('mes', 'mes_operator', 'maintenance') if key in cfg]
        if ('recurring_lab' not in cfg or not isinstance(bridge, dict)
                or set(bridge) != {'login', 'password_env'} or not all(map(name, bridge.values()))
                or any(bridge[key] == account[key] for account in other_accounts for key in bridge)):
            raise ValueError('Physical rework requires a separate scoped MES bridge and recurring lab')
        password = os.environ.get(bridge['password_env'])
        if not password or any(password == os.environ.get(account['password_env'])
                               for account in other_accounts):
            raise ValueError('Physical rework needs a separate available MES bridge password')
        resolved['physical_rework'] = dict(resolved['mes'], login=bridge['login'], password=password)
    if 'mes_reader' in cfg:
        from integrations.revisions import valid_receiver_marker

        reader = cfg['mes_reader']
        accounts = [cfg[key] for key in ('mes', 'mes_operator', 'maintenance', 'physical_rework') if key in cfg]
        if (not isinstance(reader, dict) or set(reader) != {'login', 'password_env', 'instance'}
                or not name(reader['login']) or not name(reader['password_env'])
                or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', reader['password_env'])
                or not valid_receiver_marker('mes', reader['instance'])
                or any(reader[key] == account[key] for account in accounts for key in ('login', 'password_env'))):
            raise ValueError('Invalid commissioned MES analytics reader')
        password = os.environ.get(reader['password_env'])
        if not password or any(password == os.environ.get(account['password_env']) for account in accounts):
            raise ValueError('MES analytics requires a separate available credential')
        resolved['mes_reader'] = dict(resolved['mes'], login=reader['login'], password=password)
        resolved['mes_reader_instance'] = reader['instance']
    if ':' in resolved['mes']['source_id']:
        raise ValueError('Invalid MES source ID')
    endpoints = json.loads((run/'env/endpoints.json').read_text())
    if (resolved['erp']['base'], resolved['erp']['site']) != (endpoints['erp'], endpoints['site']):
        raise ValueError('Production ERP must be the recurring company site')
    settings = run/'env/company-sources.json'
    legacy = json.loads(settings.read_text()) if settings.exists() else {}
    resolved['plm_source'] = resolved['source']
    if legacy.get('plm'):
        primary = legacy.get('plm_source') or legacy['source'] + '-plm'
        if legacy['plm']['url'].rstrip('/') == resolved['plm']['url'].rstrip('/'):
            resolved['plm_source'] = primary
        elif resolved['plm_source'] == primary:
            raise ValueError('PLM source already identifies the primary company site')
    target = quality_target(resolved)
    if 'qms_source' not in resolved:
        identity = json.dumps(target).encode()
        resolved['qms_source'] = 'qms-' + sha256(identity).hexdigest()[:20]
    if legacy.get('qms_reader'):
        primary = legacy.get('qms_source') or legacy['source'] + '-qms'
        if quality_target(legacy) == target:
            if cfg.get('qms_source', primary) != primary:
                raise ValueError('QMS source differs from the registered company site')
            resolved['qms_source'] = primary
        elif resolved['qms_source'] == primary:
            raise ValueError('QMS source already identifies the primary company site')
    if 'recurring_lab' in cfg:
        resolved['recurring_lab'] = recurring_lab(cfg, resolved)
    return fingerprint, resolved


def registered(run, item=None):
    """Read committed extension systems from the atomic catalog."""
    path = run/'scheduled-additions/engine.sqlite3'
    if not path.is_file():
        return {}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        data, = db.execute('SELECT data FROM checkpoint WHERE id=1').fetchone()
    return {system: spec for change in json.loads(data).get('extensions', {}).values()
            for system, spec in change['systems'].items() if spec['adapter'] == ADAPTER
            and (item is None or system == 'production-' + item)}
