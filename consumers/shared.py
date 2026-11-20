"""Shared review and approval behavior for company consumers."""
import json
import math

from engine._journal import CallFailed


def native_id(record):
    value = record.get("native_id") if isinstance(record, dict) else record
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Engine write must return a native ID or read-back record")
    return value


def review_policy(engine, config, payload):
    role = engine.event['role']
    proposed = payload.get('effective_at')
    review = payload.get('review_at')
    evidence = payload.get('evidence')
    if (role != 'director' or payload.get('trigger') not in ('audit', 'incident', 'scheduled_review')
            or not isinstance(evidence, list) or not evidence
            or any(not isinstance(ref, str) or not ref.strip() for ref in evidence)
            or type(proposed) is not int or type(review) is not int or not engine.now <= proposed < review):
        return
    event_id = engine.event['id']
    decision = engine.load_consumer_decision(event_id)
    if decision is None:
        prompt = json.dumps(dict(role=role, scope='approval_request', trigger=payload['trigger'],
                                 evidence=evidence, effective_at=proposed, review_at=review), sort_keys=True)
        try:
            reply = json.loads(engine.call_provider('model', prompt, [])['raw'])
            limit = reply.get('limit')
            valid = (reply.get('action') == 'propose' and type(limit) in (int, float)
                     and math.isfinite(limit) and limit >= 0)
        except Exception:
            valid = False
        decision = engine.save_consumer_decision(event_id, dict(input=json.loads(prompt), limit=limit if valid else None))
    if decision['limit'] is None:
        engine.write('erp', 'decision_pending', dict(trigger=payload['trigger'], evidence=evidence,
                                                    reason='No valid policy proposal'))
        return
    updates = engine.state.setdefault('approved_policies', [])
    policies = [*config.get('policies', []), *updates]
    version = 1 + max((p.get('version', 0) for p in policies if p.get('scope') == 'approval_request'), default=0)
    policy = dict(scope='approval_request', version=version, approver=role, evidence=evidence,
                  effective_at=proposed, review_at=review, limit=decision['limit'])
    note_id = native_id(engine.write('erp', 'policy_note', {**policy, 'trigger': payload['trigger']}))
    native_id(engine.write('erp', 'policy_setting', {**policy, 'note_id': note_id}))
    updates.append(policy)


def handle(engine, config, kind, payload):
    if kind == "management_review":
        case = payload["case"]
        review_id = native_id(engine.write("erp", "management_review", {"case": case}))
        followup_id = native_id(engine.write("tickets", "review_followup", {"case": case, "review_id": review_id}))
        native_id(engine.write("mail", "review_notice", {"case": case, "review_id": review_id, "followup_id": followup_id}))
        return True
    if kind != "approval_request":
        return False

    role = engine.event.get("role")
    if role != "manager":
        raise ValueError("Approval requires an authorized manager event role")
    amount, quote, commitments = (payload.get(k) for k in ("amount", "quote", "open_commitments"))
    if (type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0
            or not isinstance(quote, str) or not quote.strip()
            or type(commitments) not in (int, float) or not math.isfinite(commitments) or commitments < 0):
        raise ValueError("Invalid approval decision fields")
    event_id = engine.event.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("Approval requires a stable event ID")
    # The engine must persist the decision and its input before any native output.
    load = getattr(engine, "load_consumer_decision", None)
    save = getattr(engine, "save_consumer_decision", None)
    if not callable(load) or not callable(save):
        raise RuntimeError("Durable consumer decision persistence is required")
    request = json.dumps([event_id, kind, role, engine.event_started_at, payload],
                         sort_keys=True, allow_nan=False)
    decision = load(event_id)
    if decision is None:
        as_of = engine.event_started_at
        evidence = {"amount": amount, "quote": quote, "open_commitments": commitments}
        observed = [r for r in engine.observe(role, as_of=as_of)
                    if r.get("kind") not in ("approval", "decision_pending")]
        policies = [*config.get("policies", []), *engine.state.get("approved_policies", [])]
        started = [p for p in policies if p.get("scope") == kind and p.get("effective_at", 0) <= as_of]
        policy = max(started, key=lambda p: (p.get("version", 0), p.get("effective_at", 0)), default=None)
        if policy and (as_of >= policy.get("review_at", 0) or not policy.get("approver")
                       or not policy.get("evidence") or amount > policy.get("limit", -1)):
            policy = None
        decision_input = {"role": role, "evidence": evidence, "observations": observed, "policy": policy}
        key = json.dumps(decision_input, sort_keys=True, allow_nan=False)
        decision_input = json.loads(key)
        cache = engine.state.setdefault("consumer_decision_cache", {})
        action = cache.get(key) if policy else None
        route = "cache"
        if action is None:
            call = getattr(engine, "call_provider", None)
            if call is None:
                call = engine.providers.call
            prompt = json.dumps({**decision_input, "as_of": as_of}, sort_keys=True, allow_nan=False)
            tier = "jev"
            try:
                result = json.loads(call(tier, prompt, [])["raw"])
                if not isinstance(result.get("judgment"), bool):
                    raise ValueError("invalid routing response")
                if result["judgment"]:
                    tier = "model"
                    result = json.loads(call(tier, prompt, [])["raw"])
                    action = result.get("action")
                    if action not in ("approve", "defer", "reject", "investigate"):
                        raise ValueError("invalid decision response")
                else:
                    action = "approve" if policy else "defer"
            except (CallFailed, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
                action = "pending"
                decision = {"input": decision_input, "action": action, "reason": f"{tier}: decision unavailable"}
            route = tier
            if policy and action == "approve" and tier == "jev":
                decision = {"input": decision_input, "action": action, "cache_key": key}
        if decision is None:
            decision = {"input": decision_input, "action": action}
        # Must be atomic, immutable by event ID, and return the persisted value.
        decision = save(event_id, {**decision, "request": request, "route": route})
    if decision.get("request") != request:
        raise ValueError("Saved approval belongs to a different request")
    if "cache_key" in decision:
        engine.state.setdefault("consumer_decision_cache", {})[decision["cache_key"]] = decision["action"]
    action = decision["action"]
    if action not in ("approve", "defer", "reject", "investigate", "pending"):
        raise ValueError("Invalid persisted decision")
    fields = {"case": payload["case"]}
    if action == "approve":
        fields["action"] = action
        policy = decision["input"].get("policy")
        if policy and "version" in policy:
            fields["policy_version"] = policy["version"]
        native_id(engine.write("erp", "approval", fields))
    else:
        fields["reason"] = decision.get("reason", action)
        native_id(engine.write("erp", "decision_pending", fields))
    return True
