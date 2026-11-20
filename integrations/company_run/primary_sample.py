"""Read and reconcile ordinary ERP lot references through commissioned identities."""
from datetime import date
import json
from math import isfinite

from integrations.qms.adapter import APIError, INSPECTION, OP, QMS
from integrations.qms.sample_reference import (
    COMPANY, KEY, ORIGINS, SCOPE, WAREHOUSE, decode, reference_key, reference_row, submitted_row,
)
from integrations.revisions import RevisionMismatch
from integrations.revisions.receivers import erp_receiver_marker
from native.company_erp import Pending, digest
from .authority import qms_target
from .registered_lab import authenticated_actor, ensure_batch


class PrimarySample:
    def __init__(self, engine, cfg, lot, *, client=QMS):
        self.cfg, self.lot, self.client = cfg, lot, client
        self.lab = cfg['lab']
        self.commission = self.lab.get('sample_writer')
        self.legacy = self.lab.get('references', {}).get(lot['batch'])
        if not self.legacy and not self.commission:
            raise Pending('Primary QMS sample writer requires opt-in commissioning')
        doctype = lot['source_type']
        if doctype not in ORIGINS:
            raise Pending('Unsupported primary ERP lot source')
        erp = engine.api['BUYER' if doctype == 'Purchase Receipt' else 'WAREHOUSE']
        bound = cfg['erp']
        if (erp.base.rstrip('/'), erp.site) != (bound['base'].rstrip('/'), bound['site']):
            raise Pending('Primary ERP source endpoint differs from commissioning')
        doc = erp.get(doctype, lot['source_id'])
        try:
            submitted_row(doc, lot, engine.company)
        except ValueError as exc:
            raise Pending(str(exc)) from exc
        if self.commission:
            planner = engine.api['PLANNER']
            if (planner.base.rstrip('/'), planner.site) != (erp.base.rstrip('/'), erp.site):
                raise Pending('Primary ERP marker reader differs from the lot source')
            try:
                instance = erp_receiver_marker(planner.metadata('BOM'))
            except RevisionMismatch as exc:
                raise Pending('Primary ERP instance could not be verified') from exc
            if (instance != self.commission['erp_instance']
                    or cfg['erp_source'] != self.commission['erp_source']
                    or cfg['qms_source'] != self.commission['qms_source']
                    or qms_target(cfg['qms_reader']) != tuple(self.commission['target'])
                    or self.lab['analyst_user'] != self.commission['analyst_user']
                    or cfg['qms_approver'] != self.commission['approver_user']):
                raise Pending('Primary sample commissioning differs from the ERP or QMS instance')
            users = {self.lab['analyst_user'], cfg['qms_approver'], self.commission['user']}
            if len(users) != 3 or not all(users) or 'Administrator' in users:
                raise Pending('Primary sample writer, analyst and approver must be independent')
            self.scope = dict({k: lot[k] for k in ('source_type', 'source_id', 'source_row',
                                                   'batch', 'item', 'qty', 'uom')},
                              erp_source=cfg['erp_source'], erp_instance=instance,
                              qms_source=cfg['qms_source'], qms_instance=self.commission['instance'],
                              writer=self.commission['user'])
            self.key = digest([self.scope, self.commission['target'],
                               self.lab['analyst_user'], cfg['qms_approver']])

    @property
    def operation(self):
        if self.legacy:
            return self.lot['batch'] + ':operating-initial'
        return f'operating:{self.key}:initial'

    def actor(self, role):
        if role == 'writer':
            settings, user = self.commission['connection'], self.commission['user']
        elif role == 'reader':
            settings, user = self.cfg['qms_reader'], None
        else:
            settings = self.lab[role]
            user = self.lab['analyst_user'] if role == 'analyst' else self.cfg['qms_approver']
        if qms_target(settings) != qms_target(self.cfg['qms_reader']):
            raise Pending('Primary sample actor is configured for a different QMS site')
        actor = (self.client(**settings) if role == 'reader'
                 else authenticated_actor(self.client, settings, user))
        if self.commission:
            marker = self.commission['instance']
            if actor.get('Quality Inspection Parameter', marker).get('name') != marker:
                raise Pending('Primary sample actor could not verify the QMS site marker')
        return actor

    def row(self, actor, reference):
        doc = actor.get('Stock Entry', reference)
        lot = self.lot
        try:
            if doc.get('name') != reference:
                raise ValueError('Wrong QMS sample receipt')
            if self.legacy:
                note = f"ERP {lot['source_type']} {lot['source_id']} batch {lot['batch']}"
                if (reference != self.legacy or doc.get('remarks') != note
                        or doc.get('owner') != 'Administrator' or doc.get(KEY) or doc.get(SCOPE)):
                    raise ValueError('Wrong legacy QMS sample receipt')
            elif decode(doc) != self.scope:
                raise ValueError('Wrong QMS sample binding')
            return reference_row(doc, lot)
        except (KeyError, TypeError, ValueError) as exc:
            raise Pending('QMS sample receipt differs from the commissioned ERP lot') from exc

    def reference(self, parameter):
        analyst = self.actor('analyst')
        self.require_own_inspection(analyst)
        if self.legacy:
            self.row(analyst, self.legacy)
            return self.legacy
        reader = self.actor('reader')
        for doctype, name in (('Quality Inspection Parameter', parameter),
                              ('Quality Procedure', self.lab['procedure'])):
            if reader.get(doctype, name).get('name') != name:
                raise Pending('Primary sample assay master is unavailable')
        writer = self.actor('writer')
        lot = self.lot
        item, batch, uom = lot['item'], lot['batch'], lot['uom']
        self.ensure(writer, 'Item', {'name': item}, item_code=item, item_name=item,
                    stock_uom=uom, item_group='Raw Material', is_stock_item=1, has_batch_no=1)
        master = reader.get('Item', item)
        if (master.get('name'), master.get('stock_uom'), master.get('is_stock_item'),
                master.get('has_batch_no'), master.get('has_serial_no'), master.get('disabled')) != (
                item, uom, 1, 1, 0, 0):
            raise Pending('Primary QMS item differs from the ERP lot')
        ensure_batch(writer, batch, item)
        reference = self.ensure(writer, 'Stock Entry', {KEY: reference_key(batch)},
                                stock_entry_type='Material Receipt', company=COMPANY,
                                items=[dict(item_code=item, qty=lot['qty'], basic_rate=1, batch_no=batch,
                                            use_serial_batch_fields=1, t_warehouse=WAREHOUSE)],
                                **{KEY: reference_key(batch), SCOPE: json.dumps(self.scope, sort_keys=True)})
        self.row(analyst, reference)
        return reference

    @staticmethod
    def ensure(writer, doctype, filters, **fields):
        rows = writer.list(doctype, filters, limit=2)
        if not rows:
            try:
                writer.request('POST', writer.resource(doctype), fields)
            except (APIError, OSError):
                if not writer.list(doctype, filters, limit=2):
                    raise
            rows = writer.list(doctype, filters, limit=2)
        if len(rows) != 1:
            raise Pending('Primary QMS sample master or receipt is ambiguous')
        return rows[0]['name']

    def verify_sample(self, sample, today):
        if (not isinstance(sample, dict) or set(sample) != {'scope', 'date', 'value', 'reference'}
                or sample['scope'] != self.key or not isinstance(sample['reference'], str)
                or not sample['reference'] or type(sample['value']) not in (int, float)
                or not isfinite(sample['value']) or round(sample['value'], 2) != sample['value']
                or not isinstance(sample['date'], str)
                or not self.valid_date(sample['date'])
                or sample['date'] > today):
            raise Pending('Scheduled sample belongs to a different primary QMS lot')

    @staticmethod
    def valid_date(value):
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False

    def require_own_inspection(self, actor):
        found = actor.list(INSPECTION, {'batch_no': self.lot['batch']}, limit=2,
                           order='creation desc, name desc', fields=(OP, 'creation'))
        if found and (found[0].get(OP) != self.operation
                      or (len(found) == 2 and found[0]['creation'] == found[1]['creation'])):
            raise Pending('Primary operating lab cannot supersede a separate inspection')

    def inspection(self, actor, name, reference):
        doc = actor.get(INSPECTION, name)
        row = self.row(actor, reference)
        if (doc.get('name'), doc.get(OP), doc.get('reference_type'), doc.get('reference_name'),
                doc.get('child_row_reference'), doc.get('owner')) != (
                name, self.operation, 'Stock Entry', reference, row['name'], self.lab['analyst_user']) or any(
                    doc.get(k) for k in ('custom_retest_of', 'custom_rework_execution',
                                         'custom_rework_output_batch')):
            raise Pending('Inspection differs from the primary QMS sample receipt')
        return doc
