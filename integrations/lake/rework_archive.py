"""Reconcile physical rework from archived native records, without a delivery."""
from datetime import datetime

from integrations.company_run.consumption import amount, bundle_entry, index_issues
from integrations.qms.disposition_history import confirmed_current
from genealogy import ShipmentHistory
from rework_reader import resolve


def reconcile(rows, erp):
    archive = ShipmentHistory(rows, erp)
    history, latest = archive.history, archive.latest
    ledger = index_issues({key[2]: doc for key, doc in latest.items()
                           if key[:2] == (erp, 'Stock Ledger Entry')})
    results = []
    cited = set()
    postings = {key[2]: post for key, post in latest.items()
                if key[:2] == (erp, 'Stock Entry') and post.get('docstatus') == 1
                and post.get('stock_entry_type') == 'Repack'}
    disputed = archive.disputed_stock_entries
    for doc in history.get(erp, {}).get('Stock Entry', []):
        if (doc['name'] in disputed and doc.get('stock_entry_type') == 'Repack'
                and doc.get('docstatus') == 1
                and doc.get('modified') == latest[erp, 'Stock Entry', doc['name']].get('modified')):
            postings.setdefault(doc['name'], doc)
    for name, post in postings.items():
        result = {'erp': erp, 'stock_entry': name, 'status': 'broken', 'unresolved': []}
        results.append(result)
        if name in disputed:
            result['unresolved'].append([name, 'conflicting ERP observations for rework posting'])
            continue
        pin = post.get('rework') or {}
        authority = pin.get('qms_authority') or {}
        candidates = [source for source, entities in history.items()
                      if any(d.get('name') == authority.get('inspection') and d.get('modified') == authority.get('version')
                             for d in entities.get('Quality Inspection', []))
                      and any(d.get('name') == authority.get('case') and d.get('custom_failed_inspection') == authority.get('inspection')
                              and d.get('custom_qms_batch') == authority.get('batch') for d in entities.get('Non Conformance', []))]
        if pin.get('qms_source'):
            candidates = [source for source in candidates if source == pin['qms_source']]
        try:
            if len(candidates) != 1:
                raise ValueError('ambiguous or missing QMS source')
            source, = candidates
            result['qms_source'] = source
            cited.add((source, authority.get('inspection'), authority.get('version')))
            items = post.get('items', [])
            if len(items) != 2 or len(ledger.get(post['name'], {})) != 2:
                raise ValueError('rework ledger rows')
            output = None
            for row in items:
                direction = 'Outward' if row.get('s_warehouse') and not row.get('t_warehouse') else 'Inward' if row.get('t_warehouse') and not row.get('s_warehouse') else None
                if not direction:
                    raise ValueError('rework warehouse direction')
                warehouse = row['s_warehouse'] if direction == 'Outward' else row['t_warehouse']
                entry = bundle_entry(post, row, archive.bundles, direction, warehouse)
                stock_qty = row.get('stock_qty')
                if stock_qty is None and row.get('stock_uom') and row.get('uom') == row['stock_uom']:
                    stock_qty = row.get('qty')
                qty = amount(stock_qty)
                signed = qty if direction == 'Inward' else -qty
                moves = ledger[post['name']].get(row['name'], [])
                if (qty <= 0 or ('conversion_factor' in row and amount(row['conversion_factor']) != 1)
                    or any(amount(row[field]) != qty for field in ('qty', 'transfer_qty') if field in row)
                    or amount(entry.get('qty')) != signed or len(moves) != 1
                    or not post.get('company') or not post.get('posting_date')):
                    raise ValueError('rework batch bundle or ledger quantity')
                move, = moves
                if ((move.get('docstatus'), move.get('is_cancelled'), move.get('company'), move.get('posting_date'),
                     move.get('item_code'), move.get('warehouse'), move.get('stock_uom'), move.get('serial_and_batch_bundle'))
                    != (1, 0, post['company'], post['posting_date'], row.get('item_code'), warehouse,
                        row.get('stock_uom'), row.get('serial_and_batch_bundle')) or amount(move.get('actual_qty')) != signed):
                    raise ValueError('rework ledger movement')
                if direction == 'Inward':
                    if output is not None:
                        raise ValueError('duplicate rework output')
                    output = row
            if not output:
                raise ValueError('missing rework output')
            lot = dict(batch=output['batch_no'], item=output['item_code'], unit=output['stock_uom'], qms_source=source)
            result['batch'] = lot['batch']
            resolve(lot, post, erp, history, latest, archive.movements, result['unresolved'], disputed=disputed, physical_only=True)
            if not lot.get('physical_rework'):
                continue
            # A historical release proves the prior decision; current state determines whether it remains active.
            retests = [d for d in history[source].get('Quality Inspection', [])
                       if d.get('custom_retest_of') == authority.get('inspection')
                       and d.get('custom_rework_execution') == pin.get('rework')
                       and d.get('custom_rework_output_batch') == lot['batch']]
            releases = [d for d in retests if (d.get('workflow_state'), d.get('status'), d.get('docstatus')) == ('Released', 'Accepted', 1)]
            if not releases:
                related = [d for d in history[source].get('Quality Inspection', [])
                           if d.get('custom_retest_of') == authority.get('inspection')]
                if any(d not in retests for d in related):
                    raise ValueError('retest execution or output batch link')
                result['status'] = 'pending'
                continue
            valid = []
            for release in releases:
                candidate = dict(lot, quality_release=[source, 'Quality Inspection', release['name'], release.get('modified')])
                errors = []
                resolve(candidate, post, erp, history, latest, archive.movements, errors, disputed=disputed)
                if not errors and candidate.get('rework') and (release, candidate['rework']) not in valid:
                    valid.append((release, candidate['rework']))
            if len(valid) != 1:
                raise ValueError('released rework retest version or measurement')
            release, evidence = valid[0]
            current = confirmed_current([d for d in history[source]['Quality Inspection']
                                         if d.get('name') == release['name']]) or {}
            result['rework'] = evidence
            result['retest'] = [source, 'Quality Inspection', release['name'], release['modified']]
            if current == release:
                result['status'] = 'released'
            elif ((current.get('workflow_state'), current.get('status'), current.get('docstatus')) ==
                  ('Revoked', 'Accepted', 1) and current.get('modified_by') and current.get('modified')
                  and len(current['modified']) > 10 and len(release['modified']) > 10
                  and datetime.fromisoformat(current['modified']) > datetime.fromisoformat(release['modified'])
                  and all(current.get(field) == release.get(field) for field in (
                      'batch_no', 'item_code', 'custom_retest_of', 'custom_rework_execution',
                      'custom_rework_output_batch', 'custom_spec_revision', 'custom_lot_quantity',
                      'custom_lot_uom', 'report_date', 'readings'))):
                result['status'] = 'revoked'
            else:
                raise ValueError('current retest does not confirm release or revocation for this salvage lot')
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            result['unresolved'].append([post.get('name'), str(exc)])
    for (source, entity, name), doc in latest.items():
        if (entity == 'Quality Inspection' and doc.get('workflow_state') == 'Rework Authorized'
                and doc.get('status') == 'Rejected' and doc.get('docstatus') == 1
                and (source, name, doc.get('modified')) not in cited):
            results.append(dict(qms_source=source, inspection=name, status='pending', unresolved=[]))
    return results
