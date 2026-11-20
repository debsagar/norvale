"""Native sample references and replay scope for registered production labs."""
from datetime import date
from math import isfinite

from integrations.qms.adapter import APIError, INSPECTION, OP
from integrations.qms.rework import rework_assay
from integrations.qms.sample_reference import (
    LOT_FIELDS, ORIGINS, decode_registered, reference_row, registered_binding, registered_note, submitted_row,
)
from native.company_erp import Pending, digest
from .quality import finished_specification, lot_specification


def authenticated_actor(client, connection, employee):
    actor = client(**connection)
    if not employee or actor.request('GET', 'method/frappe.auth.get_logged_user').get('message') != employee:
        raise Pending('Authenticated QMS user differs from the commissioned lab identity')
    return actor


def ensure_batch(writer, batch, item):
    rows = writer.list('Batch', {'batch_id': batch}, limit=2)
    if not rows:
        try:
            writer.request('POST', writer.resource('Batch'), {'batch_id': batch, 'item': item})
        except (APIError, OSError):
            if not writer.list('Batch', {'batch_id': batch}, limit=2):
                raise
        rows = writer.list('Batch', {'batch_id': batch}, limit=2)
    if (len(rows) != 1 or rows[0]['name'] != batch
            or writer.get('Batch', batch).get('item') != item):
        raise Pending('QMS batch identity collides with another lot')


class RegisteredLab:
    def __init__(self, actor, lot, cfg, client, *, output=None):
        self.lot = lot
        self.output = output
        self.cfg = cfg
        self.lab = cfg['recurring_lab']
        self.assay = self.lab['assays'][lot['item']]
        self.client = client
        self.spec = lot_specification(cfg, lot)
        source = lot.get('revision', {}).get('mes_source')
        production = None
        if lot['kind'] in ('finished', 'intermediate'):
            revision = lot['revision']
            recorded = actor.completed_work(lot)
            report = recorded['report']
            output = report['output']
            expected = finished_specification(lot['item'], self.assay['method'], cfg['source'],
                                              {'native_id': revision['id'],
                                               'source_version': revision['version']})
            if (self.spec != expected or revision['item'] != cfg['item']
                    or revision.get('plm_source') != cfg['plm_source']
                    or report.get('revision') != revision or not report.get('authorized')
                    or report.get('source') != source
                    or report.get('work_order') != lot['work_order']
                    or recorded.get('target_id') != lot['source_id']
                    or (output['lot_id'], output['item'], output['qty'], output['uom']) !=
                    (lot['mes_lot_id'], lot['item'], lot['qty'], lot['uom'])):
                raise Pending('Registered assay does not match the posted manufacture and pinned method')
            production = [cfg['source'], cfg['family'], cfg['item'], source, recorded['work'],
                          report['id'], report['version'], output['lot_id']]
        self.key = digest([cfg['qms_source'], cfg['qms_reader']['url'].rstrip('/'), cfg['qms_reader']['site'],
                           cfg['qms_approver'],
                           self.lab['analyst_user'], self.lab['reference_user'], self.lab['procedure'],
                           self.lab['company'], self.lab['warehouse'],
                           lot['source_type'], lot['source_id'], lot['source_row'], lot['batch'],
                           lot['item'], lot['qty'], lot['uom'], self.spec, self.assay,
                           self.lab['model'], production])

        self.engine = actor.e
        self.erp_source = actor.cfg.get('erp_source') or (
            actor.cfg['source'] + '-erp' if actor.cfg.get('source') else None)

    def scope(self):
        binding = registered_binding(self.erp_source, self.cfg, self.lot['item'])
        return {**{k: self.lot[k] for k in LOT_FIELDS}, **binding, 'operation': self.operation('initial')}

    def verify_erp_row(self):
        lot = self.lot
        doctype = lot['source_type']
        if not self.erp_source or doctype not in ORIGINS:
            raise Pending('Registered sample requires a configured ERP source')
        erp = self.engine.api['BUYER' if doctype == 'Purchase Receipt' else 'WAREHOUSE']
        target = self.cfg['erp']
        if (erp.base.rstrip('/'), erp.site) != (target['base'].rstrip('/'), target['site']):
            raise Pending('Registered sample ERP endpoint differs from the profile')
        try:
            submitted_row(erp.get(doctype, lot['source_id']), lot, self.engine.company)
        except (KeyError, ValueError) as exc:
            raise Pending('Registered sample requires the exact submitted ERP row') from exc

    @property
    def limits(self):
        return self.assay['min'], self.assay['max']

    def operation(self, stage):
        return f'operating:{self.key}:{stage}'

    @property
    def sample_scope(self):
        if self.output:
            return digest([self.key, self.output['batch'], self.output['completion_id']])
        return self.key

    @property
    def physical_rework(self):
        if rework_assay(self.lab, self.lot['item']):
            return self.cfg.get('physical_rework')
        return None

    def sample_operation(self, stage):
        return f'operating:{self.sample_scope}:{stage}'

    def actor(self, role):
        user = {'analyst': self.lab['analyst_user'], 'approver': self.cfg['qms_approver'],
                'reference_writer': self.lab['reference_user']}[role]
        return authenticated_actor(self.client, self.lab[role], user)

    def reference(self):
        """Reconcile native reference masters and a draft before the LAB result."""
        reader = self.client(**self.cfg['qms_reader'])
        self.require_own_inspection(reader)
        lot = self.lot
        item, batch, unit = lot['item'], lot['batch'], lot['uom']
        parameter = self.assay['parameter']
        procedure = self.lab['procedure']
        if (reader.get('Quality Inspection Parameter', parameter).get('name') != parameter
                or reader.get('Quality Procedure', procedure).get('name') != procedure):
            raise Pending('Registered QMS parameter or investigation procedure is unavailable')
        master = reader.get('Item', item)
        if (master.get('name') != item or master.get('stock_uom') != unit
                or not master.get('has_batch_no') or master.get('has_serial_no') or master.get('disabled')):
            raise Pending('Registered lab item differs from the ERP lot')
        writer = self.actor('reference_writer')
        note = registered_note(self.scope())

        def references():
            return [row for remark in (note, self.note)
                    for row in writer.list('Stock Entry', {'remarks': remark}, limit=2)]

        rows = references()
        if not rows:
            self.verify_erp_row()
            ensure_batch(writer, batch, item)
            try:
                writer.request('POST', writer.resource('Stock Entry'), dict(
                    stock_entry_type='Material Receipt', company=self.lab['company'], remarks=note,
                    items=[dict(item_code=item, qty=lot['qty'], basic_rate=1, batch_no=batch,
                                use_serial_batch_fields=1,
                                t_warehouse=self.lab['warehouse'])]))
            except (APIError, OSError):
                if not references():
                    raise
            rows = references()
        if len(rows) != 1:
            raise Pending('Sample reference does not resolve uniquely at the registered site')
        reference = rows[0]['name']
        self.row(reader, reference)
        return reference

    @property
    def note(self):
        lot = self.lot
        return f"ERP {lot['source_type']} {lot['source_id']} row {lot['source_row']} / {self.operation('reference')}"

    def row(self, client, reference):
        doc = client.get('Stock Entry', reference)
        try:
            scope = decode_registered(doc)
            if (doc.get('name') != reference or doc.get('owner') != self.lab['reference_user']
                    or (scope != self.scope() if scope else doc.get('remarks') != self.note)):
                raise ValueError('Wrong registered sample binding')
            return reference_row(doc, self.lot, company=self.lab['company'], warehouse=self.lab['warehouse'])
        except (KeyError, TypeError, ValueError) as exc:
            raise Pending('QMS reference does not match the source-qualified sample') from exc

    def verify_sample(self, sample):
        if (not isinstance(sample, dict) or set(sample) != {'scope', 'date', 'value', 'reference'}
                or sample['scope'] != self.sample_scope or type(sample['value']) not in (int, float)
                or not isfinite(sample['value']) or round(sample['value'], 2) != sample['value']
                or not isinstance(sample['reference'], str) or not sample['reference']
                or date.fromisoformat(sample['date']).isoformat() != sample['date']):
            raise Pending('Scheduled sample belongs to a different lab or lot')

    def require_own_inspection(self, client):
        found = client.list(INSPECTION, {'batch_no': self.lot['batch']}, limit=1,
                            order='creation desc, name desc', fields=(OP,))
        if found and found[0].get(OP) != self.operation('initial'):
            raise Pending('Registered operating lab cannot supersede a separate inspection')

    def inspection(self, client, inspection, sample):
        self.verify_sample(sample)
        doc = client.get(INSPECTION, inspection)
        row = self.row(client, sample['reference'])
        operation = self.output['rework_key'] if self.output else self.operation('initial')
        dated = not self.output or doc.get('docstatus') == 1
        if (doc['name'] != inspection or doc.get('owner') != self.lab['analyst_user']
                or doc.get('inspected_by') != self.lab['analyst_user'] or doc.get(OP) != operation
                or (dated and doc.get('report_date') != sample['date'])
                or doc.get('reference_type') != 'Stock Entry'
                or doc.get('reference_name') != sample['reference']
                or doc.get('child_row_reference') != row['name']
                or (not self.output and (doc.get('custom_retest_of') or doc.get('custom_rework_execution')))
                or (doc.get('custom_rework_salvage_quantity') and (self.output or not self.physical_rework))):
            raise Pending('Inspection differs from the scheduled registered sample')
        return doc
