"""Bind acceptance credentials and endpoints only from this demo's private directory."""
import argparse
import json
import os
from pathlib import Path
from integrations.mes.client import Mes
from integrations.plm import PLM
from integrations.qms.sample_reference import ENTITY as SAMPLE_ENTITY
from integrations.revisions.receivers import ERPNext, erp_receiver_marker
from native.company import Engine, save
from .authority import qms_target


def configure(root, *, scoped_receipts=False, sample_writer=False):
    e = Engine(root / 'business')
    path = e.run/'env/company-sources.json'
    cfg = json.loads(path.read_text()) if path.exists() else {}
    settings = root/'analytics.json'
    if scoped_receipts:
        cfg['scoped_receipts'] = True
    def read(path):
        return (root / path).read_text().strip()
    data = json.loads(read('services.json'))
    fixture = json.loads(read('mes-fixture.json'))
    mes_cfg = dict(url=data['mes_url'], db='norvale_mes', login='planner',
                   password=read('mes-password'), source_id=data['mes_project'])
    reader_secret = root/'mes-analytics-password'
    instance = fixture.get('analytics_instance')
    previous_mes = json.loads(settings.read_text()).get('mes', {}) if settings.is_file() else {}
    mes_scan = previous_mes or dict(reader=dict(mes_cfg, login='operator'))
    previous_commission = 'instance' in previous_mes or 'reader_company' in previous_mes
    if instance is not None or reader_secret.exists() or 'mes_analytics' in cfg or previous_commission:
        from integrations.revisions import valid_receiver_marker
        from .lake import check_mes_reader
        password = read('mes-analytics-password')
        rework_secret = root/'mes-rework-password'
        if (not valid_receiver_marker('mes', instance) or len(password) < 16
                or password == mes_cfg['password']
                or (rework_secret.is_file() and password == read('mes-rework-password'))):
            raise ValueError('Invalid commissioned MES analytics credentials or instance')
        mes_scan = dict(reader=dict(mes_cfg, login='analytics', password=password),
                        instance=instance, reader_company=fixture.get('analytics_company'))
        if (('mes_analytics' in cfg and cfg['mes_analytics'] != mes_scan)
                or (previous_commission and previous_mes != mes_scan)):
            raise ValueError('Commissioned MES analytics binding changed')
        check_mes_reader(Mes(**mes_scan['reader']), mes_scan)
        cfg['mes_analytics'] = mes_scan
    elif not path.exists() and not settings.exists():
        raise ValueError('Fresh company runs require the commissioned MES analytics reader')
    ERPNext(e.admin).provision(scoped=cfg.get('scoped_receipts', False))
    mes = Mes(**mes_cfg)
    units = {}
    for item, code in [('NV-SHAMPOO', 'Nos'), ('NV-BULK', 'Kg')]:
        row, = mes.call('product.product', 'search_read', [['default_code', '=', item]],
                        fields=['uom_id', 'company_id'])
        units[code] = row['uom_id'][0]
        if 'instance' in mes_scan and row['company_id'][0] != mes_scan['reader_company']:
            raise ValueError('Primary MES product differs from the commissioned reader company')
    # Explicit demo UOM precision for the engine's formulation quantities.
    # Both the Odoo UOM and its decimal precision constrain native move lines.
    # These settings are provisioned by the private Odoo ORM fixture.
    kg = mes.call('uom.uom', 'read', [units['Kg']], fields=['rounding'])[0]
    digits = mes.call('decimal.precision', 'precision_get', 'Product Unit of Measure')
    if kg['rounding'] > 0.000001 or digits < 8:
        raise ValueError('Provision isolated Odoo mass precision before running')
    users = json.loads(read('qms/users.json'))
    if 'dispatcher' not in users:
        users.update(json.loads(read('qms/dispatcher.json')))
    if sample_writer:
        users.update(json.loads(read('qms/sample-writer.json')))
    def qms(role):
        return dict(url=read('qms/url'), **{key:users[role][key] for key in ('api_key','api_secret')})
    def plm(role):
        user = 'norvale-' + role
        login = PLM(data['plm_url'], user + ':' + read(role), basic=True, publisher='norvale-publisher')
        return dict(url=data['plm_url'], publisher='norvale-publisher',
                    token=login.request('/api/user/token/?name=company-run')['token'])
    cfg.update(mes=mes_cfg, maintenance_mes=dict(mes_cfg, login='maintenance'), operator=dict(mes_cfg, login='operator'), plm=plm('publisher'),
        erp=dict(base=e.eps['erp'], site=e.eps['site']),
        source=root.name, erp_source=root.name + '-erp', plm_source=root.name + '-plm', qms_source=root.name + '-qms', stock=fixture['stock'], center=fixture['centers'][0], hold=fixture['hold'],
        equipment_centers=fixture['centers'],
        units={'Units':'Nos', 'kg':'Kg'}, uoms=units, company_id=row['company_id'][0],
        qms_reader=qms('reader'), qms_dispatcher=qms('dispatcher'), qms_approver=users['approver']['user'],
        qms_script=str(Path(__file__).resolve().parents[1]/'qms/provision.sh'), qms_runtime=str(root/'qms'))
    if (root / 'mes-rework-password').is_file():
        cfg['rework_mes'] = dict(mes_cfg, login='rework', password=read('mes-rework-password'))
    if e.config.get('company_operating'):
        cfg['plm_commissioning'] = {
            role: dict(plm(role), decision_actor='norvale-' + role)
            for role in ('engineering', 'approver')
        }
    cfg.setdefault('products', {})
    cfg.setdefault('specifications', {})
    cfg.setdefault('lab', dict(references={})).update(analyst=qms('analyst'), approver=qms('approver'),
        analyst_user=users['analyst']['user'], **{key: json.loads(read('qms/fixture.json'))[key] for key in ('procedure', 'parameter')})
    if e.config.get('company_operating') and 'sample_writer' in users:
        user = users['sample_writer']
        writer = dict(connection=qms('sample_writer'), user=user['user'], instance=user['instance'],
                      target=list(qms_target(cfg['qms_reader'])), erp_source=cfg['erp_source'],
                      erp_instance=erp_receiver_marker(e.api['PLANNER'].metadata('BOM')),
                      qms_source=cfg['qms_source'], analyst_user=cfg['lab']['analyst_user'],
                      approver_user=cfg['qms_approver'])
        previous = cfg['lab'].get('sample_writer')
        if previous and any(previous[k] != writer[k] for k in writer if k != 'connection'):
            raise ValueError('Sample writer commissioning identity changed')
        cfg['lab']['sample_writer'] = writer
    save(path, cfg)
    key, secret = e.tokens['QA'].split(':')
    from integrations.lake.erp_scope import ERP_ANALYTICS_ROLES
    entity_readers = {}
    for entity, role in {'BOM':'PLANNER', 'Work Order':'PLANNER', **ERP_ANALYTICS_ROLES}.items():
        api_key, api_secret = e.tokens[role].split(':')
        entity_readers[entity] = dict(url=e.eps['erp'], site=e.eps['site'], api_key=api_key, api_secret=api_secret)
    scans = dict(erp=dict(source=cfg['erp_source'], compose_origin=e.eps['erp'], reader=dict(url=e.eps['erp'], site=e.eps['site'],
        api_key=key, api_secret=secret), entities=['BOM','Work Order','Stock Entry','Serial and Batch Bundle','Delivery Note', *ERP_ANALYTICS_ROLES],
        entity_readers=entity_readers),
        qms=dict(source=root.name + '-qms', reader=qms('reader'),
                 entities=['Quality Inspection','Quality Inspection Workflow','Norvale Dispatch Lease','Quality Action','Non Conformance']),
        mes=mes_scan, plm=dict(source=root.name + '-plm',
            reader=plm('reader'), parts=[p['family'] for p in cfg['products'].values()]))
    if writer := cfg['lab'].get('sample_writer'):
        scans['qms']['entities'].append(SAMPLE_ENTITY)
        scans['qms']['sample_scope'] = dict(qms_instance=writer['instance'], writer=writer['user'],
                                          erp_source=writer['erp_source'], erp_instance=writer['erp_instance'])
    save(settings, scans)
    binding = e.run/'env/planning-report.json'
    if (e.config or {}).get('company_operating'):
        from .provision_maintenance import provision
        provision(e.run)
        if not binding.exists():
            save(binding, dict(settings=str(root/'analytics.json'), archive=str(root/'lake')))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--scoped-receipts', action='store_true')
    parser.add_argument('--sample-writer', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    configure(args.root.resolve(), scoped_receipts=args.scoped_receipts,
              sample_writer=args.sample_writer)
