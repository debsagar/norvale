"""Read physical rework authority and create a separate, unmeasured retest."""

from .adapter import ACTION, CASE, INSPECTION, STATE
from .measurements import numeric_results


PH_LIMITS = (5, 6)
PROCEDURE = 'QC-ADJUST-1'


def rework_assay(lab, item):
    assay = lab['assays'].get(item)
    if (assay and lab['procedure'] == PROCEDURE and
            (assay['parameter'], assay['min'], assay['max']) == ('pH', *PH_LIMITS)):
        return assay
    return None


def proposed_salvage(lot, reading):
    """Work instruction QC-ADJUST-1 covers one moderate high-side failure."""
    eligible = (lot['kind'] == 'finished' and not lot.get('rework_key')
                and lot['uom'] == 'Nos' and float(lot['qty']).is_integer()
                and lot['qty'] >= 2 and PH_LIMITS[1] < reading <= 6.60)
    return lot['qty'] - 1 if eligible else 0


class ReworkQuality:
    def __init__(self, reader, analyst, analyst_user, approver_user, *, assay=None, procedure=None):
        self.reader = reader
        self.analyst = analyst
        self.analyst_user = analyst_user
        self.approver_user = approver_user
        self.assay = assay
        self.procedure = procedure

    def latest(self, batch):
        rows = self.reader.list(INSPECTION, {"batch_no": batch}, limit=2,
                                order="creation desc, name desc", fields=("name", "creation"))
        if not rows or (len(rows) == 2 and rows[0]["creation"] == rows[1]["creation"]):
            return None
        return self.reader.get(INSPECTION, rows[0]["name"])

    def authorization(self, batch, inspection, item, revision, total, uom, erp_batch, as_of):
        failure = self.latest(batch)
        if (not failure or failure["name"] != inspection or failure[STATE] != "Rework Authorized"
                or failure["docstatus"] != 1 or failure["status"] != "Rejected"
                or failure["modified_by"] != self.approver_user
                or False not in numeric_results(failure).values()
                or not failure.get("report_date") or failure["report_date"] > as_of
                or (failure["batch_no"], failure["item_code"], failure["custom_spec_revision"],
                    failure["custom_lot_quantity"], failure["custom_lot_uom"])
                != (batch, item, revision, total, uom)
                or failure.get("custom_rework_input_batch") != erp_batch
                or not 0 < (failure.get("custom_rework_salvage_quantity") or 0) <= total):
            return None
        cases = self.reader.list(CASE, {"custom_qms_batch": batch,
                                       "custom_failed_inspection": inspection}, limit=2)
        if len(cases) != 1:
            return None
        if self.assay is not None:
            assay = self.assay
            case = self.reader.get(CASE, cases[0]['name'])
            corrective = self.reader.list(ACTION, {
                'custom_qms_batch': batch, 'custom_non_conformance': case['name'],
                'procedure': self.procedure, 'corrective_preventive': 'Corrective',
            }, limit=2)
            if (case.get('procedure') != self.procedure or len(corrective) != 1
                    or failure.get('inspected_by') != self.analyst_user
                    or numeric_results(failure) != {(assay['parameter'], assay['min'], assay['max']): False}):
                return None
            reading = float(failure['readings'][0]['reading_1'])
            if failure['custom_rework_salvage_quantity'] != proposed_salvage(
                    {'kind': 'finished', 'qty': total, 'uom': uom}, reading):
                return None
        return {"batch": batch, "inspection": inspection, "version": failure["modified"],
                "approver": failure["modified_by"], "case": cases[0]["name"],
                "quantity": failure["custom_rework_salvage_quantity"]}

    def verifies_retest(self, retest, authority):
        """Compare the native measurements with the failed inspection that authorized the repair."""
        failure = self.reader.get(INSPECTION, authority['inspection'])
        if (failure.get('name') != authority['inspection'] or failure.get('modified') != authority['version']
                or failure.get('docstatus') != 1 or failure.get('status') != 'Rejected'
                or failure.get(STATE) != 'Rework Authorized' or failure.get('modified_by') != authority['approver']
                or retest.get('batch_no') != authority['batch'] or retest.get('inspected_by') != self.analyst_user
                or any(failure.get(k) != retest.get(k) for k in ('batch_no', 'item_code', 'custom_spec_revision'))):
            return False
        original, repeated = numeric_results(failure), numeric_results(retest)
        return False in original.values() and repeated.keys() == original.keys() and all(repeated.values())

    def request_retest(self, operation, authority, execution, output, lot, day):
        failure = self.reader.get(INSPECTION, authority["inspection"])
        fields = {
            "inspection_type": failure["inspection_type"], "reference_type": failure["reference_type"],
            "reference_name": failure["reference_name"], "item_code": lot["item"],
            "batch_no": authority["batch"], "sample_size": 1, "inspected_by": self.analyst_user,
            "report_date": day, "status": "Pending", "custom_spec_revision": lot["revision"]["inspection_spec"],
            "custom_lot_quantity": lot["qty"], "custom_lot_uom": lot["uom"],
            "custom_retest_of": authority["inspection"], "custom_rework_execution": execution,
            "custom_rework_output_batch": output,
        }
        if failure.get("child_row_reference"):
            fields["child_row_reference"] = failure["child_row_reference"]
        # A technician can add measurements after this draft exists. Replays
        # verify immutable scope without requiring the readings to stay empty.
        return self.analyst.create(INSPECTION, operation, **fields)
