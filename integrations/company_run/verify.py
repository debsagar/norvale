"""Fail acceptance unless independent native histories and retained business work exist."""
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sys

from adapter import Lake
from genealogy import ShipmentHistory
from integrations.qms.disposition_history import ENTITY as DISPOSITION_ENTITY, confirmed_current
from integrations.qms.measurements import numeric_results
from deltalake import DeltaTable
from integrations.mes.client import EVENT
from native.company import Engine, save
from native.company_erp import digest
from .consumption import STOCK_UNITS, amount, bundle_entry, index_issues, reconcile_inputs
from .lake import scan_cycle
from .settlement import read_settlements
from .interrupted_acceptance import verified_interrupted


def submitted_names(client, doctype):
    names = []
    filters = {'docstatus': 1}
    while True:
        page = client.list(doctype, filters, limit=500, order_by='name asc')
        names.extend(row['name'] for row in page)
        if len(page) < 500:
            return names
        filters['name'] = ['>', names[-1]]


def shipment_archive_coverage(history, outbound):
    """Compare the native submitted deliveries with the latest snapshots from this ERP."""
    observed = {name: doc for (source, entity, name), doc in history.latest.items()
                if source == history.erp and entity == 'Delivery Note'
                and doc.get('docstatus') == 1 and not doc.get('is_return')}
    def line_evidence(doc):
        fields = ('name', 'item_code', 'qty', 'uom', 'stock_uom', 'stock_qty', 'batch_no', 'warehouse',
                  'against_sales_order', 'so_detail', 'serial_and_batch_bundle')
        return {row['name']: tuple(row.get(k) for k in fields) for row in doc.get('items', [])}


    changed = []
    for name in sorted(outbound.keys() & observed.keys()):
        doc, snapshot = outbound[name], observed[name]
        if (not doc.get('modified') or any(doc.get(k) != snapshot.get(k)
                for k in ('name', 'modified', 'company', 'customer', 'posting_date'))
                or not doc.get('items') or len(doc['items']) != len(snapshot.get('items', []))
                or line_evidence(doc) != line_evidence(snapshot)):
            changed.append(name)
    return dict(missing=sorted(outbound.keys() - observed.keys()),
                unexpected=sorted(observed.keys() - outbound.keys()), changed=changed)


def native_work_pin(engine, work):
    """Reconstruct a released definition using only reads from its native owners."""
    work_id = work['request']['work_order']
    order = engine.api['PLANNER'].get('Work Order', work_id)
    revision = json.loads(order['custom_norvale_definition'])
    released = revision.get('erp_released_on') or order['planned_start_date'][:10]
    authority = engine.company_erp.authority
    cfg, product = authority.route(order['production_item'], work['dispatch'].get('source'), pinned=True)
    plm, bridge, mes = authority.family_clients(cfg, product)
    pin = dict(work_id=work_id, released_on=released, native_id=int(revision['id']),
               source_version=revision['version'],
               receiver_ids={'erp':revision['erp_ack_id'], 'mes':revision['mes_ack_id']},
               **({'receiver_markers': revision['receiver_markers']} if 'receiver_markers' in revision else {}))
    publication = plm.resolve_pin(pin, work_id=work_id, on=released)
    _, receivers = bridge.installed(pin, publication)
    mo = mes.read('mrp.production', int(work['dispatch']['id']))
    if (order['docstatus'] != 1 or order['bom_no'] != receivers['erp']['bom_id']
            or (publication['definition'].get('revision_of') or publication['native_id']) != product['family']
            or mo['origin'] != work_id or mo['bom_id'][0] != int(receivers['mes']['bom_id'])
            or mo['norvale_revision'] != receivers['mes']['revision']):
        raise ValueError(f'Native release and dispatch disagree for {work_id}')
    return dict(work_order=work_id, plm=pin['native_id'], version=pin['source_version'], released_on=released,
                erp=order['bom_no'], mes=mo['bom_id'][0], mes_source=mes.source_id, dispatch=mo['id'],
                **({'receiver_markers': pin['receiver_markers']} if 'receiver_markers' in pin else {}))


def reconcile_completions(rows, erp_source, orders, namespace, quarantine):
    """Compare independent native histories, including entries the controller never recorded."""
    completions = {}
    postings = {}
    bundles, ledger, input_versions = {}, {}, defaultdict(set)
    unit_ids = defaultdict(set)
    line_owners = defaultdict(set)
    output_receipts = []
    input_issues = []
    problems = []
    for row in sorted(rows, key=lambda r:r['ingested_at']):
        kind = row['entity']
        if kind == 'Stock Entry' and row['source'] == erp_source:
            postings[row['native_id']] = json.loads(row['payload'])
        elif kind == 'Serial and Batch Bundle' and row['source'] == erp_source:
            bundles[row['native_id']] = json.loads(row['payload'])
        elif kind == 'Stock Ledger Entry' and row['source'] == erp_source:
            ledger[row['native_id']] = json.loads(row['payload'])
        elif kind == 'mrp.production':
            doc = json.loads(row['payload'])
            if doc.get('erp_work_ref') in orders:
                key = (row['source'], doc['completion_ref'])
                version = (doc['modified'], doc['erp_work_ref'])
                output = doc.get('output') or {}
                facts = json.dumps(output, sort_keys=True)
                unit_ids[key[0], 'name', output.get('unit')].add(output.get('unit_id'))
                unit_ids[key[0], 'id', output.get('unit_id')].add(output.get('unit'))
                completions.setdefault(key, {}).setdefault(version, set()).add(facts)
                inputs = doc.get('inputs') or []
                input_versions[key, version].add(json.dumps(sorted(
                    inputs, key=lambda line: str(line.get('line_id'))), sort_keys=True))
                for line in inputs:
                    unit, unit_id = line.get('unit'), line.get('unit_id')
                    unit_ids[key[0], 'name', unit].add(unit_id)
                    unit_ids[key[0], 'id', unit_id].add(unit)
                    line_owners[key[0], line.get('line_id')].add(key[1])
    ledger = index_issues(ledger)
    by_reference = {}
    for key in completions:
        by_reference.setdefault(key[1], []).append(key)
    matches = {key: [] for key in completions}
    for name, doc in sorted(postings.items()):
        if (doc.get('docstatus') != 1 or doc.get('stock_entry_type') != 'Manufacture'
                or doc.get('work_order') not in orders):
            continue
        pin = doc.get('completion') or {}
        candidates = [key for key in by_reference.get(pin.get('completion_id'), ())
                      if key[0] == pin.get('mes_source', key[0])
                      and (pin.get('version'), doc['work_order']) in completions[key]]
        if len(candidates) != 1:
            problems.append(dict(posting=name, work=doc['work_order'], matching_completions=len(candidates)))
        else:
            source, completion = candidates[0]
            matches[source, completion].append(name)
            outputs = [r for r in doc.get('items', []) if r.get('is_finished_item')]
            facts = completions[source, completion][pin['version'], doc['work_order']]
            try:
                if len(outputs) != 1 or len(facts) != 1:
                    raise ValueError('finished output mismatch')
                output = json.loads(next(iter(facts)))
                row = outputs[0]
                item, lot = output.get('item_code'), output.get('lot_id')
                unit, unit_id = output.get('unit'), output.get('unit_id')
                stock_unit = STOCK_UNITS.get(unit)
                qty = amount(output.get('quantity'))
                lot_key = ['MES', source, pin.get('output_lot_id')] if pin.get('mes_source') else ['MES', pin.get('output_lot_id')]
                expected_batch = 'NV-' + digest([namespace, lot_key, item])[:32]
                if (not namespace or not quarantine or not item or not lot or not unit_id
                        or not stock_unit or unit_ids[source, 'name', unit] != {unit_id}
                        or unit_ids[source, 'id', unit_id] != {unit}
                        or item != row.get('item_code') or pin.get('output_lot_id') != f'{source}:stock.lot:{lot}'
                        or row.get('batch_no') != expected_batch or row.get('s_warehouse')
                        or row.get('t_warehouse') != quarantine or row.get('uom') != stock_unit
                        or row.get('stock_uom') != stock_unit or qty <= 0
                        or qty != amount(row.get('qty')) or qty != amount(doc.get('fg_completed_qty'))
                        or any(amount(row[k]) != qty for k in ('stock_qty', 'transfer_qty') if k in row)
                        or ('conversion_factor' in row and amount(row['conversion_factor']) != 1)):
                    raise ValueError('finished output mismatch')
                entry = bundle_entry(doc, row, bundles, 'Inward', quarantine)
                moves = ledger.get(name, {}).get(row['name'], [])
                if amount(entry.get('qty')) != qty or len(moves) != 1:
                    raise ValueError('missing, duplicate or mismatched finished receipt')
                move, = moves
                if (not doc.get('company') or not doc.get('posting_date')
                        or (move.get('docstatus'), move.get('is_cancelled'), move.get('company'),
                            move.get('posting_date'), move.get('item_code'), move.get('warehouse'),
                            move.get('stock_uom'), move.get('serial_and_batch_bundle'))
                        != (1, 0, doc['company'], doc['posting_date'], item, quarantine, stock_unit,
                            row['serial_and_batch_bundle']) or amount(move.get('actual_qty')) != qty):
                    raise ValueError('finished ledger receipt mismatch')
                output_receipts.append(dict(posting=name, work=doc['work_order'], mes_source=source,
                                            completion=completion, mes_lot=lot, version=pin['version'],
                                            item=item, batch=expected_batch, quantity=str(qty), unit=stock_unit,
                                            warehouse=quarantine, bundle=row['serial_and_batch_bundle'],
                                            ledger=move['name']))
            except (ValueError, KeyError, TypeError) as exc:
                problems.append(dict(posting=name, completion=completion, error=str(exc)))
            try:
                observations = input_versions[(source, completion), (pin['version'], doc['work_order'])]
                if len(observations) != 1:
                    raise ValueError('conflicting MES inputs for the posted completion version')
                inputs = json.loads(next(iter(observations)))
                if (len({line.get('line_id') for line in inputs}) != len(inputs)
                        or any(len(line_owners[source, line.get('line_id')]) != 1 for line in inputs)):
                    raise ValueError('duplicate MES stock move line')
                issues = reconcile_inputs(doc, inputs, source, postings, bundles, ledger.get(name, {}), unit_ids)
            except (ValueError, KeyError, TypeError) as exc:
                problems.append(dict(posting=name, completion=completion, error=str(exc)))
            else:
                input_issues.append(dict(posting=name, work=doc['work_order'], source=source,
                                         completion=completion, version=pin['version'], issues=issues))
    for (source, completion), names in matches.items():
        if len(names) != 1:
            problems.append(dict(source=source, completion=completion, matching_postings=names))
    return dict(completions=len(completions), matching_posts=sorted(n for names in matches.values() for n in names),
                output_receipts=sorted(output_receipts, key=lambda result: result['posting']),
                input_issues=sorted(input_issues, key=lambda result: result['posting']), discrepancies=problems)


def active_releases(rows, qms_source):
    """A later revocation, cancellation or disappearance supersedes an earlier release."""
    versions = defaultdict(list)
    for row in sorted(rows, key=lambda r: r['ingested_at']):
        if row['source'] == qms_source and row['entity'] == 'Quality Inspection':
            versions[row['native_id']].append(json.loads(row['payload']))
    return sorted(name for name, snapshots in versions.items()
                  if (doc := confirmed_current(snapshots)) and doc.get('docstatus') == 1 and doc.get('status') == 'Accepted'
                  and doc.get('workflow_state') == 'Released')


def measured_retests(rows):
    """Match the failed measurement and the released retest to one native QMS case."""
    failures = defaultdict(set)
    retests = []
    for row in rows:
        if row['entity'] != 'Quality Inspection':
            continue
        doc = json.loads(row['payload'])
        scope = tuple(doc.get(k) for k in ('item_code', 'batch_no', 'custom_spec_revision'))
        if doc.get('docstatus') != 1 or not all(scope):
            continue
        results = numeric_results(doc)
        if not results:
            continue
        methods = frozenset(results)
        if doc.get('status') == 'Rejected' and not all(results.values()):
            failures[row['source'], doc['name'], scope].add(methods)
        parent = doc.get('custom_retest_of')
        if (parent and parent != doc['name'] and doc.get('status') == 'Accepted'
                and doc.get('workflow_state') == 'Released' and all(results.values())):
            retests.append((row['source'], parent, scope, doc['name'], methods))
    return sorted({(source, parent, child) for source, parent, scope, child, methods in retests
                   if failures.get((source, parent, scope)) == {methods}})


def revoked_allocation_held(revocation, draft, inspection, ledger, traces, history, qms_source, *, release=None):
    """Check the held draft and order any earlier issues using the QMS site's own clock."""
    batch = revocation.get('batch')
    if (not batch or not revocation.get('version') or ledger or draft.get('docstatus') != 0
            or draft.get('name') != revocation.get('draft') or inspection.get('docstatus') != 1
            or inspection.get('name') != revocation.get('inspection')
            or inspection.get('workflow_state') != 'Revoked' or inspection.get('modified') != revocation['version']
            or not inspection.get('batch_no')):
        return False
    try:
        allocations = json.loads(draft.get('custom_norvale_quality_releases') or draft.get('remarks') or 'null')
        pin, = [row for row in allocations if row['batch'] == batch]
        scope = pin['disposition']
        line, = [row for row in draft['items'] if row.get('batch_no') == batch]
        if (scope['version'] != revocation.get('released_version', scope['version'])
                or amount(pin['qty']) != amount(revocation.get('allocated_qty', pin['qty']))
                or scope['id'] != inspection['name'] or scope['status'] != 'released'
                or scope['batch'] != batch or scope.get('qms_batch', batch) != inspection['batch_no']
                or scope.get('qms_source', qms_source) != qms_source
                or scope['item'] != inspection['item_code'] or line['item_code'] != scope['item']
                or amount(line['qty']) != amount(pin['qty']) or not 0 < amount(pin['qty']) <= amount(scope['qty'])
                or datetime.fromisoformat(scope['version']) >= datetime.fromisoformat(revocation['version'])):
            return False
        if release and (scope.get('qms_batch', batch),
                        batch, scope['item'], amount(scope['qty']), scope['uom'], scope['approver']) != (
                            release['batch_no'], release['output_batch'], release['item_code'],
                            amount(release['lot_quantity']), release['lot_uom'], release['approver']):
            return False
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False
    for trace in traces:
        for lot in trace['lots']:
            if lot['batch'] != batch:
                continue
            lease = lot.get('dispatch_lease')
            prior_release = lot.get('quality_release')
            if lease and prior_release and lease[0] == prior_release[0] != qms_source:
                continue
            if trace['unresolved'] or not lease or lease[0] != qms_source:
                return False
            receipt = history.latest.get(tuple(lease), {})
            if receipt.get('lease_state') != 'submitted' or receipt.get('delivery') != trace['shipment'][2]:
                return False
            try:
                revoked_at = datetime.fromisoformat(revocation['version'])
                finalized = datetime.fromisoformat(receipt['modified'])
                if finalized >= revoked_at:
                    prior_release = prior_release or []
                    if (len(prior_release) != 4 or prior_release[:2] != [qms_source, 'Quality Inspection']
                            or not revoked_at < datetime.fromisoformat(prior_release[3]) <= finalized):
                        return False
            except (KeyError, ValueError, TypeError):
                return False
    return True


def ordinary_revocations(engine, primary, history, traces):
    """Locate changed allocations, then verify their disposition at the owning site."""
    from integrations.qms.adapter import QMS
    from .authority import qms_target
    flow = engine.company_erp
    results = []
    for shipment in flow.state['shipments'].values():
        change = shipment.get('allocation_change')
        if not change:
            continue
        draft = engine.api['WAREHOUSE'].get('Delivery Note', change['delivery'])
        for row in shipment['rows']:
            batch = row['batch']
            scope = row['disposition']
            cfg, _ = flow.authority.quality_route(flow.state['lots'][batch])
            source = cfg.get('qms_source')
            if not source and qms_target(cfg['qms_reader']) == qms_target(primary['reader']):
                source = primary['source']
            if not source or scope.get('qms_source', source) != source:
                raise ValueError('Withdrawn allocation differs from its registered QMS authority')
            inspection = QMS(**cfg['qms_reader']).get('Quality Inspection', scope['id'])
            if inspection.get('workflow_state') != 'Revoked':
                continue
            rev = dict(draft=change['delivery'], batch=batch, inspection=scope['id'],
                       version=inspection['modified'], qms_source=source,
                       released_version=scope['version'], allocated_qty=row['qty'])
            ledger = engine.api['WAREHOUSE'].list('Stock Ledger Entry', {
                'voucher_type':'Delivery Note', 'voucher_no':rev['draft'], 'is_cancelled':0}, limit=1)
            events = [event for event in history.history.get(source, {}).get(DISPOSITION_ENTITY, [])
                      if event['inspection'] == scope['id']]
            release = None
            if events:
                releases = [event for event in events if event['disposition'] == 'Released'
                            and event['inspection_version'] == scope['version']]
                revocations = [event for event in events if event['disposition'] == 'Revoked'
                               and event['inspection_version'] == rev['version']]
                release = releases[0] if len(releases) == 1 else None
                fields = ('batch_no', 'output_batch', 'item_code', 'lot_quantity', 'lot_uom', 'spec_revision')
                earlier = bool(release and len(revocations) == 1
                               and all(release[field] == revocations[0][field] for field in fields))
            else:
                earlier = history.matches('release', source, scope['id'], scope['version'], batch, scope['item']) == {
                    (source, 'Quality Inspection', scope['id'])}
            archived = history.latest.get((source, 'Quality Inspection', scope['id']), {})
            rev['verified'] = (earlier and archived.get('modified') == rev['version']
                               and archived.get('workflow_state') == 'Revoked'
                               and revoked_allocation_held(rev, draft, inspection, ledger, traces, history, source,
                                                           release=release))
            if rev['verified'] and release:
                rev['disposition_evidence'] = [[source, DISPOSITION_ENTITY, event['name']]
                                               for event in (release, revocations[0])]
            results.append(rev)
    return results


def verified_maintenance(engine):
    equipment = engine.company_erp.equipment
    verified = False
    for key, case in equipment.cases.items():
        if not case.get('restart'):
            continue
        authority = equipment.scoped_authority(case['center'])
        settings = authority.settings()
        token = settings.get('maintenance_staff', {}).get('reader') or settings.get('maintenance_ticket_reader')
        evidence = case.get('verification')
        if not evidence or not token:
            return False
        role = 'MAINTENANCE'
        title, body = authority.ticket_fields(case)
        ticket = authority.tickets(role, token=token).verify_issue(
            case['ticket']['number'], key, title, body, ('closed',))
        if authority.restart_evidence(ticket, case, role=role) != evidence:
            return False
        reader = authority.operator('PRODUCTION')
        center = authority.native_center(case['center'])
        event = case['fault']
        if any(record.get('source_id') != reader.source_id or record.get('native_id') != event['native_id']
               for record in (event, case['restart'])):
            return False
        fault = reader.read(EVENT, event['native_id'])
        original = event['description'].splitlines()[0]
        if (not fault['date_end'] or fault['workcenter_id'][0] != center
                or fault['description'] != f'{original}\nVerified restart gitea:{digest(evidence["ref"])}'):
            return False
        verified = True
    return verified


def verify(root):
    e = Engine(root / 'business')
    settings = json.loads((root/'analytics.json').read_text())
    archive = root/'lake/delta'
    before = DeltaTable(str(archive)).version()
    qms_sources = set()
    expected, scan_errors = scan_cycle(settings, root/'lake', root/'business', qms_sources=qms_sources)
    if scan_errors:
        raise RuntimeError(f'Native reads failed: {scan_errors}')
    table = DeltaTable(str(archive))
    lake = Lake(archive,{})
    rows = lake._rows()
    lots = e.company_erp.state
    documents = {}
    for doctype in ('Sales Order','Purchase Order','Purchase Receipt',
                    'Stock Entry','Delivery Note','Journal Entry','Work Order'):
        documents[doctype] = submitted_names(e.admin, doctype)
    settlement = read_settlements(e, rows, settings['erp'])
    for entity in ('Sales Invoice', 'Purchase Invoice', 'Payment Entry'):
        documents[entity] = settlement['documents'][entity]
    revoked = e.state.get('demo_revocation', {})
    draft = e.admin.get('Delivery Note', revoked['draft']) if revoked else {}
    present = {row['source'] for row in rows}
    from .partial_work import partial_work
    dispatched = [work for work in lots['work'].values() if work.get('dispatch')]
    pins = [native_work_pin(e, work) for work in dispatched]
    families = partial_work(e, dispatched, {pin['work_order']: pin for pin in pins}, include_finished=True)
    backlog = {work: family for work, family in families.items() if family['backorder'] is not None}
    finished = {work: family for work, family in families.items() if family['backorder'] is None}
    qms_source = settings['qms']['source']
    qms_rows = [r for r in rows if r['source'] in qms_sources and r['entity'] == 'Quality Inspection']
    retests = measured_retests(qms_rows)
    releases_by_site = {site: active_releases(qms_rows, site) for site in sorted(qms_sources)}
    checks = dict(recurring=all(key in e.state['completed'] for _,key,_ in e.events()),
                  documents=all(documents.values()), interruption=(root/'interruption-confirmed').exists(),
                  partial=bool(families), four_sources=present == expected,
                  duplicate_scan=before == table.version(),
                  maintenance=verified_maintenance(e),
                  interrupted_wip=verified_interrupted(e),
                  revoked_draft=bool(revoked and draft.get('docstatus') == 0),
                  retested=bool(retests),
                  active_release=any(releases_by_site.values()),
                  failed=any(doc.get('docstatus') == 1 and doc.get('status') == 'Rejected'
                             and False in numeric_results(doc).values()
                             for doc in (json.loads(r['payload']) for r in qms_rows)),
                  native_completion=any(json.loads(r['payload']).get('erp_work_ref') in documents['Work Order']
                                        for r in rows if r['entity'] == 'mrp.production'))
    if e.config.get('company_operating'):
        checks['backorder_completed'] = bool(finished)
    checks.update(settlement['checks'])
    from integrations.qms.adapter import QMS
    qclient = QMS(**settings['qms']['reader'])
    shipments = {}
    for shipment in lots['shipments'].values():
        if shipment.get('target_id'):
            delivery = e.admin.get('Delivery Note', shipment['target_id'])
            for row, intended in e.company_erp.match_batch_rows(delivery['items'], shipment['rows'], 'batch'):
                e.company_erp.verify_row(delivery, row, intended['batch'], intended['qty'],
                                         e.wh['fg'], 'Outward', 'WAREHOUSE')
            shipments[shipment['target_id']] = [r['batch'] for r in shipment['rows']]
    posts = {}
    for operation_key, operation in lots['completions'].items():
        if operation.get('done'):
            target = operation['target_id']
            doc = e.admin.get('Stock Entry', target)
            if doc['docstatus'] != 1:
                raise ValueError('Completion stock entry lost submission')
            posts[operation_key] = target
    observed_posts = reconcile_completions(rows, settings['erp']['source'], set(documents['Work Order']),
                                           lots.get('namespace'), e.wh['hold'])
    checks['one_post_per_completion'] = (bool(posts) and observed_posts['completions'] == len(posts)
                                         and not observed_posts['discrepancies']
                                         and observed_posts['matching_posts'] == sorted(posts.values()))
    checks['native_pins'] = bool(pins)
    outbound = {}
    for name in documents['Delivery Note']:
        delivery = e.api['WAREHOUSE'].get('Delivery Note', name)
        if not delivery.get('is_return') and delivery['docstatus'] == 1:
            outbound[name] = delivery
    checks['accounted_shipments'] = set(shipments) == set(outbound)
    history = ShipmentHistory(rows, settings['erp']['source'])
    receipt_traces = []
    if e.config.get('company_operating'):
        for name in documents['Purchase Receipt']:
            if e.api['BUYER'].get('Purchase Receipt', name).get('is_return'):
                continue
            try:
                receipt_traces.append(history.purchase_receipt(name))
            except ValueError as exc:
                receipt_traces.append(dict(receipt=[history.erp, 'Purchase Receipt', name], lots=[],
                                           unresolved=[str(exc)]))
        checks['observable_receipt_trace'] = bool(receipt_traces) and all(
            trace['lots'] and not trace['unresolved'] for trace in receipt_traces)
    coverage = shipment_archive_coverage(history, outbound)
    checks['shipment_archive_coverage'] = bool(outbound) and not any(coverage.values())
    traces = [history.trace(delivery) for delivery in sorted(outbound.keys() - set(coverage['missing']))]
    checks['revocation_enforced'] = bool(revoked and revoked_allocation_held(
        revoked, draft, qclient.get('Quality Inspection', revoked['inspection']),
        e.api['WAREHOUSE'].list('Stock Ledger Entry', {'voucher_type': 'Delivery Note',
                                'voucher_no': draft['name'], 'is_cancelled': 0}, limit=1),
        traces, history, qms_source))
    withdrawal_evidence = ordinary_revocations(e, settings['qms'], history, traces) if e.config.get('company_operating') else []
    if withdrawal_evidence:
        ordinary_verified = all(row['verified'] for row in withdrawal_evidence)
        if revoked:
            checks['revocation_enforced'] &= ordinary_verified
        else:
            checks['revoked_draft'] = checks['revocation_enforced'] = ordinary_verified
    resumed_transfer = None
    archived_transfer = None
    if e.config.get('company_operating'):
        from .release_recovery import archived_resume, verified_resume
        cut = e.run/'ordinary-release-interruption.json'
        if cut.is_file():
            handoff = json.loads(cut.read_text())
            resumed_transfer = verified_resume(e, handoff)
            archived_transfer = archived_resume(history, resumed_transfer, handoff)
        checks['interruption'] = bool(resumed_transfer)
        checks['single_release_after_resume'] = bool(resumed_transfer)
        checks['archived_release_transfer'] = bool(archived_transfer and
            archived_transfer['authorization_at_transfer'] == 'confirmed')
    from rework_archive import reconcile as reconcile_rework
    rework = reconcile_rework(rows, settings['erp']['source'])
    if e.config.get('company_operating'):
        checks['retested'] = any(r['status'] == 'released' for r in rework)
        checks['rework_links'] = all(r['status'] != 'broken' for r in rework)
    if e.config.get('company_operating'):
        checks['shipment_order_lines'] = checks['shipment_archive_coverage'] and bool(traces) and all(
            trace['sales_order_lines'] and not trace['unresolved_order_lines'] for trace in traces)
    checks['observable_shipment_trace'] = checks['shipment_archive_coverage'] and bool(traces) and all(
        trace['lots'] and not trace['unresolved'] for trace in traces)
    result = dict(active_qms_releases=releases_by_site[qms_source], active_qms_releases_by_source=releases_by_site, measured_retests=retests, rework_reconciliation=rework, native_completion_reconciliation=observed_posts, shipment_archive_coverage=coverage, observable_shipment_traces=traces, observable_receipt_traces=receipt_traces, pins=pins, completion_posts=posts, shipments=shipments, checks=checks, documents=documents, partial_work=backlog, completed_backorders=finished, revoked=revoked, ordinary_revocations=withdrawal_evidence, resumed_transfer=resumed_transfer, archived_release_transfer=archived_transfer,
                  settlements=settlement['settlements'], settlement_reconciliation=settlement['bills'],
                  settlement_discrepancies=settlement['discrepancies'], delta_version=table.version(), sources=sorted(present), pending=lots.get('pending',{}))
    save(root/'acceptance.json', result)
    print(json.dumps(checks, indent=2))
    return all(checks.values())


if __name__ == '__main__':
    raise SystemExit(0 if verify(Path(sys.argv[1])) else 1)
