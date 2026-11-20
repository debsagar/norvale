"""Independent scheduled native scans. A source failure does not block its peers."""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import time

from integrations.qms.sample_reference import (
    ENTITY as SAMPLE_ENTITY, ORIGINS, decode, decode_registered, registered_binding,
)


def check_mes_reader(client, cfg):
    """Require the commissioned Odoo instance and its single-company reader."""
    from integrations.revisions import valid_receiver_marker

    instance, company = cfg['instance'], cfg['reader_company']
    if (not valid_receiver_marker('mes', instance) or type(company) is not int or company <= 0
            or client.call('mrp.production', 'norvale_analytics_identity', company) != instance):
        raise PermissionError('MES analytics reader differs from its commissioned instance or company')


def mes_target(connection):
    return connection['url'].rstrip('/'), connection['db']


def registered_mes_scan(cfg, product, company, primary):
    execution = cfg['mes']
    reader = {'reader': execution, 'product': product, 'company': company}
    if 'mes_reader' in cfg:
        reader.update(reader=cfg['mes_reader'], instance=cfg['mes_reader_instance'], reader_company=company)
    if primary and ('instance' in primary or 'instance' in reader):
        baseline = primary['reader']
        same_site = mes_target(execution) == mes_target(baseline)
        same_source = execution['source_id'] == baseline['source_id']
        if same_site != same_source:
            raise ValueError('MES source and endpoint disagree with the primary reader')
        if same_site and 'instance' in primary:
            if (company != primary['reader_company']
                    or reader.get('instance', primary['instance']) != primary['instance']
                    or ('instance' in reader and reader['reader'] != baseline)):
                raise ValueError('Registered MES reader, company or instance differs from its commissioned site')
            if 'instance' not in reader:
                reader.update(primary)
    return reader


def scan(source, cfg, root):
    from integrations.lake.boundary import DISPOSITION_ENTITY, Frappe, ingest, project
    if source in ('erp', 'qms'):
        errors = []
        entities = cfg['entities']
        if source == 'qms':
            entities = dict.fromkeys([*entities, 'Norvale Dispatch Lease', DISPOSITION_ENTITY])
            if cfg.get('registered_samples'):
                entities[SAMPLE_ENTITY] = None

        def registered_sample(doc):
            scope = decode_registered(doc)
            if scope and not any(all(scope[k] == v for k, v in binding.items())
                                 for binding in cfg['registered_samples']):
                raise ValueError('Sample reference differs from the registered sources or employees')
            return scope
        for entity in entities:
            def open_reader():
                account = cfg.get('entity_readers', {}).get(entity, cfg['reader'])
                return Frappe(account['url'], account['api_key'], account['api_secret'], account.get('site'),
                              private_origin=cfg.get('compose_origin'))
            def projection(entity, doc):
                if entity == SAMPLE_ENTITY and 'registered_inspection' not in doc:
                    scope = decode(doc)
                    expected = dict(cfg['sample_scope'], qms_source=cfg['source'])
                    if any(scope[k] != v for k, v in expected.items()):
                        raise ValueError('Sample reference belongs to a different commissioned source or writer')
                if entity != 'BOM':
                    result = project(entity, doc)
                    if source == 'erp' and entity in ('Stock Entry', 'Work Order'):
                        field = 'remarks' if entity == 'Stock Entry' else 'custom_norvale_definition'
                        try:
                            link = json.loads(doc.get(field) or '{}')
                        except (ValueError, TypeError):
                            link = {}
                        if isinstance(link, dict) and entity == 'Stock Entry' and link.get('completion_id'):
                            result.update({k:link[k] for k in ('completion_id','dispatch_id','output_lot_id')})
                        if isinstance(link, dict) and entity == 'Work Order' and link.get('mes_ack_id'):
                            result.update(plm_part=link['id'], plm_version=link['version'],
                                          mes_bom=link['mes_ack_id'], erp_bom=link['erp_ack_id'])
                    return result
                projected = project(entity, doc)
                result = {k:doc[k] for k in ('name', 'modified','item','company','docstatus','quantity','uom')}
                for field in ('owner', 'receiver_marker'):
                    if field in projected:
                        result[field] = projected[field]
                result['items'] = [{k:row[k] for k in ('item_code','qty','uom')} for row in doc['items']]
                return result
            try:
                if entity == SAMPLE_ENTITY and cfg.get('registered_samples'):
                    def fetch():
                        return open_reader().documents(entity, registered_sample=registered_sample,
                                                       primary_sample='sample_scope' in cfg)
                    ingest(root, entity, fetch, source=cfg['source'], projection=projection)
                else:
                    ingest(root, entity, source=cfg['source'], projection=projection, open_reader=open_reader)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup('Native entity scans failed', errors)
    elif source == 'mes':
        from integrations.lake.mes_reader import scan as collect, scan_labor, scan_staging
        from integrations.mes.client import Mes
        client = Mes(**cfg['reader'])
        if 'instance' in cfg or 'reader_company' in cfg:
            check_mes_reader(client, cfg)
        errors = []
        for reader in (collect, scan_labor, scan_staging):
            try:
                reader(root, client, product=cfg.get('product'), company=cfg.get('company'))
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup('Native MES scans failed', errors)
    elif source == 'publication':
        from integrations.plm import PLM
        from integrations.lake.plm_reader import scan_publications
        scan_publications(root, cfg['source'], PLM(**cfg['reader']), cfg['families'], cfg['on'],
                          registered_family=cfg.get('registered_family'))
    elif source == 'plm':
        from integrations.lake.plm_reader import scan_bom
        from integrations.plm import PLM
        client = PLM(**cfg['reader'])
        parts = (part['pk'] for family in sorted(set(cfg['parts'])) for part in client._family(family))
        scan_bom(root, cfg['source'], client, parts, registered_family=cfg.get('registered_family'))
    else:
        raise ValueError('Unknown source')


def scan_cycle(config, root, run=None, *, qms_sources=None, business_day=None, unresolved_profiles=None):
    from native.production_profiles import profile, quality_target, registered
    sources = set()
    errors = []
    primary_mes = config.get('mes')
    mes_mismatch = False
    if run:
        path = run/'env/company-sources.json'
        if path.is_file():
            bindings = json.loads(path.read_text())
            commissioned = bindings.get('mes_analytics')
            if commissioned is not None:
                reader = commissioned['reader']
                execution = bindings.get('mes', {})
                mes_mismatch = (commissioned != primary_mes
                                or any(execution.get(key) != reader[key] for key in ('url', 'db', 'source_id'))
                                or bindings.get('company_id') != commissioned['reader_company'])
                if mes_mismatch:
                    errors.append((reader['source_id'], 'Commissioned MES analytics binding changed'))
                primary_mes = commissioned

    def attempt(kind, cfg, *, primary=False):
        identity = cfg.get('source') or cfg['reader']['source_id']
        sources.add(identity)
        try:
            if primary and kind == 'plm' and run:
                from .publications import families, publication_source
                bindings = json.loads((run/'env/company-sources.json').read_text())
                publication_source(bindings, cfg)
                cfg = dict(cfg, parts=families(bindings))
            scan(kind, cfg, root)
            if kind == "qms" and qms_sources is not None:
                qms_sources.add(identity)
        except Exception as exc:
            errors.append((identity, type(exc).__name__))

    if not run:
        for kind, cfg in config.items():
            attempt(kind, cfg, primary=True)
        return sources, errors

    primary = config['qms']
    qms = {primary['source']: dict(primary)}
    targets = {primary['source']: quality_target({'qms_reader': primary['reader']})}
    profiles = []
    # Reload the committed catalog every pass so additions need no reader restart.
    for system, spec in registered(run).items():
        try:
            _, cfg = profile(run, system, spec)
        except Exception as exc:
            errors.append((system, type(exc).__name__))
            if unresolved_profiles is not None:
                unresolved_profiles.add(system)
            continue
        profiles.append(cfg)
        identity, target = cfg['qms_source'], quality_target(cfg)
        qms_cfg = qms.setdefault(identity, dict(
            reader=cfg['qms_reader'], source=identity,
            entities=[e for e in primary['entities'] if e != SAMPLE_ENTITY]))
        targets.setdefault(identity, target)
        if targets[identity] != target:
            errors.append((identity, 'QMS source identifies multiple sites'))
        if lab := cfg.get('recurring_lab'):
            erp = config['erp']
            erp_target = (cfg['erp']['base'].rstrip('/'), cfg['erp']['site'])
            readers = [erp.get('entity_readers', {}).get(entity, erp['reader']) for entity in ORIGINS]
            if any((reader['url'].rstrip('/'), reader.get('site')) != erp_target for reader in readers):
                errors.append((identity, 'Registered sample ERP site differs from scheduled ERP reader'))
                continue
            qms_cfg.setdefault('registered_samples', []).extend(
                registered_binding(erp['source'], cfg, item) for item in lab['assays'])
    for identity, target in targets.items():
        if list(targets.values()).count(target) > 1:
            errors.append((identity, 'QMS site identifies multiple sources'))
    failed_qms = {identity for identity, _ in errors}

    for kind, cfg in config.items():
        if kind == 'qms':
            for identity, settings in qms.items():
                if identity not in failed_qms:
                    attempt(kind, settings)
        elif kind != 'mes' or not mes_mismatch:
            attempt(kind, cfg, primary=True)
    if business_day is None:
        business_day = json.loads((run/'state.json').read_text())['day']
    on = (date(2027, 1, 1) + timedelta(days=business_day)).isoformat()
    try:
        from .publications import collect
        collect(run, root, reader=config['plm'], on=on)
    except Exception as exc:
        errors.append((config['plm']['source'], type(exc).__name__))
    for cfg in profiles:
        try:
            targets = {(m['mes']['output']['item'], m['mes']['settings']['company_id'])
                       for m in cfg['mapping'].values() if m['erp']['output']['item'] == cfg['item']}
            if not targets:
                raise ValueError('No MES output matches the registered ERP item')
        except Exception as exc:
            errors.extend((identity, type(exc).__name__) for identity in
                          (cfg['mes']['source_id'], cfg['qms_source'], cfg['plm_source']))
            continue
        for product, company in sorted(targets):
            identity = cfg['mes']['source_id']
            try:
                if mes_mismatch and identity == primary_mes['reader']['source_id']:
                    continue
                settings = registered_mes_scan(cfg, product, company, primary_mes)
            except (KeyError, ValueError) as exc:
                errors.append((identity, type(exc).__name__))
                continue
            attempt('mes', settings)
        primary = config['plm']
        reader = cfg['plm']
        same_source = cfg['plm_source'] == primary['source']
        same_site = reader['url'].rstrip('/') == primary['reader']['url'].rstrip('/')
        if same_source != same_site:
            errors.append((cfg['plm_source'], 'PLM source and endpoint disagree with the primary reader'))
            continue
        if same_source:
            reader = primary['reader']
        attempt('plm', {'reader': reader, 'source': cfg['plm_source'], 'parts': [cfg['family']],
                        'registered_family': cfg['family']})
        attempt('publication', {'reader': cfg['plm'], 'source': cfg['plm_source'],
                                'families': [cfg['family']], 'registered_family': cfg['family'], 'on': on})
    return sources, errors


def scheduled_scan(engine, minute):
    """Record the business observation cutoff separately from native scan timestamps."""
    binding = engine.run/'env/planning-report.json'
    if not binding.is_file():
        return
    status = engine.state.setdefault('lake_scans', {})
    if status.get('at') == minute and not status.get('errors'):
        return
    unresolved_profiles = set()
    try:
        paths = json.loads(binding.read_text())
        config = json.loads(Path(paths['settings']).read_text())
        sources, errors = scan_cycle(config, Path(paths['archive']), engine.run,
                                     business_day=minute // 1440, unresolved_profiles=unresolved_profiles)
    except Exception as exc:
        sources, errors = set(), [('collector', type(exc).__name__)]
    failed = {source for source, _ in errors}
    last = status.setdefault('last_success_minute', {})
    # Until every profile is resolved, a missing scope could belong to any source.
    if not unresolved_profiles:
        for source in sources - failed:
            last[source] = minute
    status.update(at=minute, errors=errors, unresolved_profiles=sorted(unresolved_profiles))
    engine.persist()
    engine.log('lake_scan', business_minute=minute, last_success_minute=last, errors=errors)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('settings', type=Path)
    parser.add_argument('root', type=Path)
    parser.add_argument('--interval', type=int, default=0)
    parser.add_argument('--run', type=Path, help='Recurring run whose committed production additions to ingest')
    args = parser.parse_args()
    while True:
        config = json.loads(args.settings.read_text())
        _, errors = scan_cycle(config, args.root, args.run)
        for identity, error in errors:
            print(identity, error, flush=True)
        if not args.interval:
            raise SystemExit(int(bool(errors)))
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
