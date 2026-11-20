"""Qualify a process interruption at the ordinary QMS-to-ERP handoff."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

from native.company import save, writable_run
from native.company_erp import CompanyERP


INTERRUPTION = 'Qualification interrupted after QMS release and before ERP stock transfer'


def transfers(engine, cut):
    """Find submitted moves from quarantine by lot, independently of saved operation IDs."""
    qa = engine.api['QA']
    filters = dict(company=engine.company, docstatus=1, stock_entry_type='Material Transfer')
    found = []
    while True:
        page = qa.list('Stock Entry', filters, limit=200, order_by='name asc')
        for entry in page:
            doc = qa.get('Stock Entry', entry['name'])
            for row in doc['items']:
                if row.get('s_warehouse') != cut['source'] or row.get('item_code') != cut['item']:
                    continue
                if row.get('batch_no') == cut['batch']:
                    found.append(doc)
                    break
                bundle = row.get('serial_and_batch_bundle')
                if bundle and any(line.get('batch_no') == cut['batch'] for line in
                                  qa.get('Serial and Batch Bundle', bundle)['entries']):
                    found.append(doc)
                    break
        if len(page) < 200:
            return found
        filters['name'] = ['>', page[-1]['name']]


def interrupt_before_transfer(flow, lot, original):
    e = flow.e
    path = e.run/'ordinary-release-interruption.json'
    if not path.exists() and e.config.get('company_operating'):
        disposition = flow.disposition(lot, 'QA')
        target = e.wh['fg' if lot['kind'] == 'finished' else 'rm']
        if (disposition and disposition['status'] == 'released' and disposition['qty'] == lot['qty']
                and not flow.return_reservation(lot) and flow.balance(lot, target, 'QA') == 0
                and flow.balance(lot, e.wh['hold'], 'QA') == lot['qty']):
            cut = dict(batch=lot['batch'], item=lot['item'], qty=lot['qty'], uom=lot['uom'],
                       disposition=disposition, source=e.wh['hold'], target=target, event=e.event)
            if not transfers(e, cut):
                cut['trace_offset'] = (e.run/'trace.jsonl').stat().st_size
                save(path, cut)
                raise InterruptedError(f'{INTERRUPTION}: {lot["batch"]} / {disposition["version"]}')
    return original(flow, lot)


def verified_resume(engine, cut):
    # A marker or checkpoint by itself cannot establish the process boundary.
    trace = engine.run/'trace.jsonl'
    interrupted = False
    with trace.open('rb') as stream:
        if 'trace_offset' in cut:
            stream.seek(cut['trace_offset'])
        for row in map(json.loads, stream):
            if row.get('kind') == 'failed' and row.get('error') == (
                    f'{INTERRUPTION}: {cut["batch"]} / {cut["disposition"]["version"]}'):
                interrupted = True
                break
            # A hard process exit can precede the exception log. A new controller
            # after the durable cut and before any transfer attempt proves restart.
            if 'trace_offset' in cut and row.get('kind') == 'controller_started':
                interrupted = True
                break
            if 'trace_offset' in cut and row.get('kind') in ('request', 'lot_sync_confirmed', 'completed'):
                break
    if not interrupted:
        return None
    flow = engine.company_erp
    moves = [m for m in flow.state['moves'].values()
             if m.get('done') and all(m.get(k) == cut[k] for k in ('batch','source','target','qty','disposition'))]
    if len(moves) != 1:
        return None
    # Account for every native issue of this lot from quarantine. A later
    # re-release can have another pin; an unmapped duplicate invalidates proof.
    known = {m['target_id']: m for m in flow.state['moves'].values()
             if m.get('done') and m['batch'] == cut['batch'] and m['source'] == cut['source']}
    observed = transfers(engine, cut)
    if {doc['name'] for doc in observed} != set(known):
        return None
    for doc in observed:
        move = known[doc['name']]
        row = flow.verify_move_intent(cut, move, doc)
        for warehouse, direction in ((move['source'], 'Outward'), (move['target'], 'Inward')):
            flow.verify_row(doc, row, cut['batch'], move['qty'], warehouse, direction, 'QA')
    return moves[0]['target_id']


def archived_resume(history, transfer, cut):
    """Trace the recovered transfer using independently ingested application records."""
    if not transfer:
        return None
    traced = history.transfer_lineage(transfer)
    if not traced or traced['authorization_at_transfer'] != 'confirmed':
        return None
    expected = {key: cut['disposition'][key] for key in traced['disposition'] if key in cut['disposition']}
    if (traced['disposition'] != expected or traced['release'][0] != cut['disposition'].get('qms_source', traced['release'][0])
            or any(traced[key] != cut[key] for key in ('item','batch','uom','qty','source','target'))):
        return None
    return traced


def main(run):
    import generate
    run = writable_run(run)
    if not json.loads((run/'config.json').read_text()).get('company_operating'):
        raise ValueError('Select a configured ordinary operating run')
    original = CompanyERP.sync
    with patch.object(CompanyERP, 'sync', lambda flow, lot: interrupt_before_transfer(flow, lot, original)):
        generate.main(['--run', str(run)])
    if not (run/'ordinary-release-interruption.json').exists():
        raise RuntimeError('No eligible release was encountered')


if __name__ == '__main__':
    main(Path(sys.argv[1]))
