"""Publish a private ERPNext backlog snapshot through the recurring PLANNER identity."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape, unescape
import json
from math import isfinite
from pathlib import Path

ENTITY = 'Work Order'
OWNER = 'planner@norvale.test'
FIELDS = ('name', 'creation', 'modified', 'planned_start_date', 'actual_start_date',
          'actual_end_date', 'status', 'production_item', 'qty', 'produced_qty', 'stock_uom')
OPTIONAL_DATES = ('actual_start_date', 'actual_end_date')
OPEN = {'Submitted', 'Not Started', 'In Process'}
MAX_AGE = timedelta(minutes=5)


def native_time(value):
    timestamp = datetime.fromisoformat(value)
    return timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)


def visible_backlog(api, company, cutoff):
    """Frappe applies row permissions and field permissions to this list."""
    native_cutoff = str(cutoff.replace(tzinfo=None))
    filters = [['company', '=', company], ['docstatus', '=', 1],
               ['creation', '<=', native_cutoff], ['planned_start_date', '<=', native_cutoff]]
    # Keep only one complete ERP query result. Never combine different source snapshots.
    limit = 128
    while True:
        page = api.list(ENTITY, filters, FIELDS, limit=limit, order_by='name asc')
        if len(page) < limit:
            break
        limit *= 2
    for row in page:
        # An inaccessible field can be omitted by Frappe. Fail closed.
        if not set(FIELDS) <= row.keys():
            raise PermissionError('Work Order reporting fields are unavailable')
        dates = [row[k] for k in ('creation', 'modified', 'planned_start_date')]
        dates.extend(row[k] for k in OPTIONAL_DATES if row[k])
        if any(native_time(value) > cutoff for value in dates):
            raise ValueError('Work Order changed after the reporting cutoff')
        if row['status'] not in OPEN:
            continue
        if (not row['production_item'] or not row['stock_uom']
                or not all(isfinite(row[field]) for field in ('qty', 'produced_qty'))):
            raise ValueError('Invalid Work Order reporting fields')
        yield row


def snapshot(api, settings, archive, company, cutoff):
    source = settings['source']
    report = dict(source=source, entity=ENTITY, company=company, as_of=cutoff.isoformat(),
                  freshness='STALE', last_successful_scan_utc=None,
                  basis='Submitted active work orders planned to start by the business cutoff.')
    try:
        state = json.loads((archive/'state.json').read_text())
        from integrations.lake.boundary import scan_key
        entry = state[scan_key(source, ENTITY)]
        if (entry['source'], entry['entity'], entry.get('scope')) != (source, ENTITY, None):
            raise ValueError('Unexpected scan identity')
        scanned = datetime.fromisoformat(entry['last_success'])
        age = datetime.now(timezone.utc) - scanned
        if age < timedelta():
            raise ValueError('Invalid scan time')
        report['last_successful_scan_utc'] = scanned.isoformat()
        if entry.get('error') or age > MAX_AGE:
            return dict(report, detail='Work Order scan failed or exceeded five minutes.')

        visible = {row['name']: row for row in visible_backlog(api, company, cutoff)}
        latest = {}
        if visible:
            from deltalake import DeltaTable
            rows = DeltaTable(str(archive/'delta')).to_pyarrow_table(
                columns=['native_id', 'ingested_at', 'payload'],
                filters=[('source', '=', source), ('entity', '=', ENTITY),
                         ('ingested_at', '<=', entry['last_success']),
                         ('native_id', 'in', list(visible))]).to_pylist()
            for row in sorted(rows, key=lambda row: row['ingested_at']):
                latest[row['native_id']] = json.loads(row['payload'])

        counts = Counter()
        outstanding = Counter()
        for name, row in visible.items():
            archived = latest.get(name, {})
            same_fields = all(archived.get(field) == row[field] for field in FIELDS if field not in OPTIONAL_DATES)
            same_dates = all((archived.get(field) or None) == (row[field] or None) for field in OPTIONAL_DATES)
            if not (same_fields and same_dates):
                return dict(report, detail='The scan has yet to confirm current planner-visible work.')
            counts[archived['status']] += 1
            outstanding[archived['production_item'], archived['stock_uom']] += max(
                archived['qty'] - archived['produced_qty'], 0)
        return dict(report, freshness='FRESH', open_work_orders=dict(sorted(counts.items())),
                    work_order_versions={name: latest[name]['modified'] for name in sorted(visible)},
                    remaining_by_item=[dict(item=item, uom=uom, quantity=round(qty, 6))
                                       for (item, uom), qty in sorted(outstanding.items())])
    except Exception:
        return dict(report, detail='The Work Order scan or planner visibility could not be verified.')


def publish(engine, binding, report_date=None):
    report_date = report_date or engine.date()
    api = engine.api['PLANNER']
    if api.request('GET', '/api/method/frappe.auth.get_logged_user', timeout=10) != OWNER:
        raise PermissionError('Expected the configured ERPNext planner account')
    config = json.loads(binding.read_text())
    settings = json.loads(Path(config['settings']).read_text())['erp']
    reader = settings.get('entity_readers', {}).get(ENTITY, settings['reader'])
    if (reader['url'], reader['site']) != (api.base, api.site):
        raise PermissionError('Planning archive is bound to a different ERPNext site')
    cutoff = engine.stack.clock.now()
    if cutoff.date().isoformat() != engine.date():
        raise ValueError('Planning report requires the current business date')
    key = f'planning-backlog:{report_date}'
    # Journal permitted contents before POST so a lost reply can be verified independently.
    journal = engine.state.setdefault('planning_report_attempts', {})
    attempts = journal.setdefault(report_date, [])
    doc = engine.recover_company_doc(key, 'Note', 'PLANNER') if attempts else None
    if doc is None:
        if report_date == engine.date():
            report = snapshot(api, settings, Path(config['archive']), engine.company, cutoff)
        else:
            report = dict(source=settings['source'], entity=ENTITY, company=engine.company,
                          as_of=report_date, freshness='STALE', last_successful_scan_utc=None,
                          detail='No planner snapshot was confirmed on the scheduled date.')
        content = '<pre>' + escape(json.dumps(report, indent=2), quote=False) + '</pre>'
        fingerprint = sha256(content.encode()).hexdigest()
        if fingerprint not in attempts:
            attempts.append(fingerprint)
            engine.persist()
        doc = engine.write(key, 'Note', {'title': f'Production backlog {report_date}',
                                        'public': 0, 'content': content}, 'PLANNER')
    if doc['owner'] != OWNER or doc['public'] != 0:
        raise PermissionError('Planning backlog note is outside its intended scope')
    # A retry can recover the already-published snapshot. Keep that dated evidence intact.
    actual = json.loads(unescape(doc['content'].removeprefix('<pre>').removesuffix('</pre>')))
    if (sha256(doc['content'].encode()).hexdigest() not in attempts
            or (actual['source'], actual['company'], actual['as_of'][:10])
            != (settings['source'], engine.company, report_date)):
        raise ValueError('Planning backlog readback differs from the dated publication')
    engine.log('planning_report', role='PLANNER', note=doc['name'], source=actual['source'],
               as_of=actual['as_of'], freshness=actual['freshness'])

def reconcile(engine, binding):
    pending = engine.state.get('pending_planning_reports', [])
    for day in pending.copy():
        try:
            publish(engine, binding, day)
        except Exception as exc:
            engine.log('planning_report_failed', error=type(exc).__name__)
        else:
            pending.remove(day)
            engine.state['planning_report_attempts'].pop(day, None)
            engine.persist()
