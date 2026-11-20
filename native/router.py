"""JEV routes evidence to judgment; it never selects business actions."""
import json
import os
import urllib.request
import urllib.error
import time
from pathlib import Path

class ProviderUnavailable(RuntimeError):
    """The routing or judgment endpoint exhausted its transport retries."""


def provider_json(provider, request, on_attempt):
    try:
        return request_json(request, on_attempt)
    except urllib.error.HTTPError as exc:
        if exc.code not in (408, 429, 500, 502, 503, 504):
            raise
        raise ProviderUnavailable(f'{provider} unavailable (HTTP {exc.code})') from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise ProviderUnavailable(f'{provider} unavailable ({type(exc).__name__})') from exc


def route(evidence, memory, policies, on_attempt=None):
    # Routing needs effective rules and current observations, not recursive policy provenance.
    policies=[{k:p.get(k) for k in ['role','effective_date','review_date','approval_limit','reorder_target','suppliers','items']} for p in policies]
    memory=[{'as_of':m.get('as_of'),'action':m.get('output',{}).get('action'),'reason':m.get('output',{}).get('reason','')[:240]} for m in memory[-6:]]
    if not os.environ.get('TYPESAFE_API_KEY'):
        raise RuntimeError('Set TYPESAFE_API_KEY before generation')
    body = {'model': 'jev-latest', 'questions': {'judgment': {
        'type': 'noul', 'instructions': 'Route this decision case: unfamiliar or exceptional cases need role judgment; cases covered by an explicit authorized policy without contradictory evidence use deterministic rules. No matching authorized policy means a new judgment case. Do not select a business action. Decisions before a policy effective date are superseded.',
        'criteria': {'true': 'No applicable authorized policy exists, or a specific observed fact contradicts the policy premise: quote/PO price or quantity mismatch, duplicate open order for the same items, released stock above a positive reorder target without demand justification, or another named contradiction',
                     'false': 'Evidence clearly satisfies an explicit effective standing policy'}}},
        'state': {'evidence': evidence, 'cases': memory, 'policies': policies}}
    req = urllib.request.Request('https://api.typesafe.ai/v1/systemone', json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + os.environ['TYPESAFE_API_KEY'], 'Content-Type': 'application/json'})
    try:raw=provider_json("JEV",req,on_attempt)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'JEV HTTP {exc.code}: {exc.read().decode()[:300]}; request bytes={len(req.data)}') from None
    probability = raw['answers']['judgment']['noul']
    if type(probability) not in (float, int) or not 0 <= probability <= 1:
        raise ValueError('Invalid JEV probability')
    return {'judgment': probability >= .5, 'probability': probability, 'request': body, 'response': raw}


def request_json(request,on_attempt=None,*,attempts=4):
    for attempt in range(attempts):
        if on_attempt:on_attempt(attempt+1)
        try:
            with urllib.request.urlopen(request,timeout=90) as response:
                return None if getattr(response, 'status', None) == 204 else json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (408,429,500,502,503,504) or attempt==attempts-1:raise
        except (urllib.error.URLError,TimeoutError):
            if attempt==attempts-1:raise
        time.sleep(2**attempt)
