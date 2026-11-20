"""Local acceptance laboratory input and role-scoped native transitions."""
from hashlib import sha256
import json

from integrations.qms.adapter import QMS, INSPECTION, CASE, ACTION


def finished_specification(item, procedure_version, plm_source, publication):
    """Bind the assay method and exact approved definition at work release."""
    fields = [item, procedure_version, plm_source, str(publication['native_id']),
              publication['source_version']]
    if not all(isinstance(v,str) and v.strip() for v in fields):
        raise ValueError("Missing identity for finished-lot inspection specification")
    encoded = json.dumps(fields, ensure_ascii=True, separators=(',', ':')).encode()
    return 'PLM-QC-' + sha256(encoded).hexdigest()


def lot_specification(config, lot):
    if lot.get('kind') in ('finished', 'intermediate'):
        return lot.get('revision', {}).get('inspection_spec')
    return config.get('specifications', {}).get(lot['item'])


def inspect(config, lot, day, *, failed=False, prior=None):
    spec = lot_specification(config, lot)
    if not spec:
        raise ValueError('Lot lacks a pinned inspection specification')
    cfg = config['lab']
    analyst = QMS(**cfg['analyst'])
    approver = QMS(**cfg['approver'])
    ref = analyst.get('Stock Entry', cfg['references'][lot['batch']])
    row, = [r for r in ref['items'] if r['batch_no'] == lot['batch']]
    if row['item_code'] != lot['item'] or row['qty'] != lot['qty']:
        raise ValueError('QMS sample source mismatch')
    fields = dict(inspection_type='In Process', reference_type='Stock Entry',
                  reference_name=ref['name'], child_row_reference=row['name'],
                  item_code=lot['item'], batch_no=lot['batch'], report_date=day,
                  sample_size=1, inspected_by=cfg['analyst_user'],
                  custom_spec_revision=spec,
                  custom_lot_quantity=lot['qty'], custom_lot_uom=lot['uom'],
                  custom_decision_rationale='Local acceptance sample measurement',
                  readings=[dict(specification=cfg['parameter'], numeric=1, min_value=5,
                                 max_value=6, reading_1='7.2' if failed else '5.5')])
    if prior:
        before = analyst.get(INSPECTION, prior)
        if before['batch_no'] != lot['batch'] or before['workflow_state'] != 'Rework Authorized':
            raise ValueError('Retest requires authorized lot rework')
        actions = analyst.list(ACTION, {'custom_qms_batch':lot['batch']}, limit=2)
        if len(actions) != 1:
            raise ValueError('Retest requires a single corrective action for this lot')
        change = analyst.get(ACTION, actions[0]['name'])
        if not change['resolutions'][0].get('resolution'):
            change = analyst.update(ACTION, change['name'], resolutions=[dict(
                problem='Reading above upper limit', status='Open',
                resolution='Demo operator completed the authorized pH adjustment.',
                responsible=cfg['analyst_user'])])
        if not change['resolutions'][0].get('resolution'):
            raise ValueError('Missing correction readback')
        fields['custom_retest_of'] = prior
    operation = lot['batch'] + (':retest' if prior else ':initial')
    record = analyst.create(INSPECTION, operation, **fields)
    if not record['docstatus']:
        record = analyst.transition(record['name'], 'Record')
    if record['status'] != ('Rejected' if failed else 'Accepted'):
        raise ValueError('Native measurement outcome differs')
    if failed:
        case = analyst.create(CASE, lot['batch'] + ':case', custom_qms_batch=lot['batch'],
                              custom_failed_inspection=record['name'], procedure=cfg['procedure'],
                              subject='Out-of-specification lot', details='Sample measured 7.2 against 5–6.')
        analyst.create(ACTION, lot['batch'] + ':correct', procedure=cfg['procedure'],
                       custom_qms_batch=lot['batch'], custom_non_conformance=case['name'],
                       corrective_preventive='Corrective',
                       resolutions=[dict(problem='Reading above upper limit', status='Open')])
    if record['workflow_state'] == 'Recorded':
        record = approver.transition(record['name'], 'Authorize Rework' if failed else 'Release Lot')
    return record
