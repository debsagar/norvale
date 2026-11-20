"""Record shipment feedback and preserve its native corrective-action history."""
from integrations.qms.adapter import ACTION, CASE, INSPECTION, OP
from .scope import inspection_matches, legacy_case


def investigate(analyst, *, source, note, row, scope, complaint, procedure, operation):
    if not all(isinstance(s, str) and s.strip() for s in (source, complaint, procedure, operation)):
        raise ValueError("A shipment, complaint and stable operation are required")
    fields = dict(subject="Customer complaint for " + note["name"], procedure=procedure, details=complaint,
                  custom_delivery_source=source, custom_delivery_note=note["name"],
                  custom_delivery_row=row["name"], custom_delivery_customer=note["customer"])
    existing = analyst.list(CASE, {OP: operation}, limit=2)
    if existing:
        record, = existing
        case = analyst.get(CASE, record["name"])
        if legacy_case(case, scope):
            return analyst.verify_intent(case, operation, {**fields, "custom_qms_batch": scope["custom_qms_batch"]})
    return analyst.create(CASE, operation, **fields, **scope)


def corrective_action(analyst, case_id, operation, problem):
    case = analyst.get(CASE, case_id)
    if not case.get("custom_delivery_note") or (
            case["status"] != "Open" and not analyst.list(ACTION, {OP: operation}, limit=1)):
        raise ValueError("Expected an open shipment complaint or an existing corrective action")
    fields = dict(corrective_preventive="Corrective", procedure=case["procedure"],
                  custom_qms_batch=case["custom_qms_batch"], custom_non_conformance=case_id)
    return analyst.create(ACTION, operation, **fields, resolutions=[{"problem": problem, "status": "Open"}],
                          verify_fields={**fields, "resolutions": [{"problem": problem}]})


def contain(approver, case, release, scope):
    if (case["status"] != "Open" or release["name"] != scope["custom_delivery_inspection"]
            or not inspection_matches(release, scope)):
        raise ValueError("Containment requires an open shipment complaint and its lot release")
    return approver.transition(release["name"], "Revoke Release")


def close_action(approver, action_id, inspection_id, result, *, scope=None, bind_case=None):
    if not isinstance(result, str) or not result.strip():
        raise ValueError("A documented effectiveness assessment is required")
    action = approver.get(ACTION, action_id)
    if scope:
        if action["custom_qms_batch"] != scope["custom_qms_batch"]:
            raise ValueError("Corrective action belongs to another original QMS batch")
        sample = approver.get(INSPECTION, inspection_id)
        if (sample["name"] != inspection_id or inspection_id == scope["custom_delivery_inspection"]
                or not inspection_matches(sample, scope)):
            raise ValueError("Effectiveness inspection differs from the shipped lot")
    if action["status"] != "Completed":
        if bind_case:
            saved = approver.update(CASE, bind_case["name"], **scope)
            if legacy_case(saved, scope):
                raise ValueError("Native complaint lost the recovered shipment release binding")
        approver.update(ACTION, action_id, custom_effectiveness_inspection=inspection_id,
                        custom_effectiveness_result=result,
                        resolutions=[{**row, "status": "Completed"} for row in action["resolutions"]])
        action = approver.get(ACTION, action_id)
    if (action["status"] != "Completed" or action["custom_effectiveness_inspection"] != inspection_id
            or action["custom_effectiveness_result"] != result):
        raise ValueError("Corrective action closure differs from the intended assessment")
    return action
