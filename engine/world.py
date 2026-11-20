"""Weekly exogenous processes and the calendar boundary for deliveries.

Transition laws adapted from the local STOCKTAKE reproducibility release.
See docs/WORLD-PROCESSES.md for source hashes and adaptation details.
"""
from copy import deepcopy
from hashlib import sha256
from random import Random

WEEK = 7 * 1440
TICK = "_world_tick"
CHECK = "_world_transit"
ARRIVAL = "_world_arrival"
INITIAL = {
    "calm": ("calm", None),
    "false_alarm": ("false_alarm", None),
    "short_onset": ("disruption", "short"),
    "long_onset": ("disruption", "long"),
}
BULLETINS = {
    "calm": "Carriers report normal transit conditions.",
    "watch": "Carriers report elevated risk and some sailing holds.",
    "warning": "Carriers are pausing transits pending assessment of an unconfirmed incident.",
    "disruption": "Carriers report sustained transit interruptions.",
    "recovery": "Carriers report services resuming while clearing backlogs.",
}


def check_event_name(id, kind):
    if kind in (TICK, CHECK, ARRIVAL) or (isinstance(id, str) and id.startswith("world:")):
        raise ValueError("Reserved world event")


def validate(config):
    fixture = config.get("world_fixture", {})
    if "world" in config and type(config["world"]) is not bool:
        raise ValueError("world must be a boolean")
    if (not isinstance(fixture, dict) or set(fixture) - {"initial_disruption", "transit_delay_minutes"}
            or ("world_fixture" in config and not config.get("world"))
            or fixture.get("initial_disruption", "calm") not in tuple(INITIAL)
            or type(fixture.get("transit_delay_minutes", 0)) is not int
            or fixture.get("transit_delay_minutes", 0) < 0):
        raise ValueError("Invalid world fixture")


def stream(seed, factor, at):
    # Draws belong to a factor and a calendar week, never to company activity.
    return Random(sha256(f"{seed}:{factor}:{at}".encode()).digest())


def disruption(previous, rng, initial):
    if not previous:
        state, dtype = INITIAL[initial]
        return dict(event_state=state, event_age=0, disruption_type=dtype,
                    local_congestion=False)
    state, age, dtype = (previous[k] for k in ("event_state", "event_age", "disruption_type"))
    next_state, next_type = state, dtype
    if state == "calm":
        if rng.random() < .08:
            next_state = "watch"
    elif state == "watch":
        coin = rng.random()
        if coin < .50:
            next_state, next_type = "disruption", "short" if rng.random() < .70 else "long"
        elif coin < .70:
            next_state = "false_alarm"
        elif coin < .85:
            next_state = "calm"
    elif state == "disruption":
        ends = (age + 1 >= 2 or rng.random() > .50) if dtype == "short" else rng.random() > .92
        if ends:
            next_state, next_type = "recovery", None
    elif state == "false_alarm" or age + 1 >= 3 or rng.random() > .50:
        next_state = "calm"
    return dict(event_state=next_state, event_age=age + 1 if next_state == state else 0,
                disruption_type=next_type, local_congestion=rng.random() < .08)


def reliability(previous, rng):
    state = previous["state"] if previous else "reliable"
    age = previous["age"] if previous else 0
    if state == "defunct":
        return previous
    next_state = state
    if previous:
        if state == "reliable":
            next_state = "wobbling" if rng.random() < .10 else state
        elif state == "wobbling":
            coin = rng.random()
            next_state = "degraded" if coin < .45 else "reliable" if coin < .80 else state
        elif state == "degraded":
            if rng.random() < .06:
                next_state = "defunct"
            elif age + 1 >= 4 or rng.random() > .70:
                next_state = "reliable"
        age = age + 1 if next_state == state else 0
    return dict(state=next_state, age=age)


def truth(state, config, cutoff):
    rows = [row for row in state.get("_world", []) if row["at"] <= cutoff]
    envelope = {"world": deepcopy(rows)}
    deliveries = {key: row for key, row in state.get("_world_deliveries", {}).items()
                  if row["checked_at"] <= cutoff}
    if deliveries:
        envelope["deliveries"] = deepcopy(deliveries)
    if "private_marker" in config:
        envelope["marker"] = deepcopy(config["private_marker"])
    return envelope


def tick(engine):
    rows = engine.state.setdefault("_world", [])
    prior = rows[-1] if rows else {}
    at = engine.now
    lane = disruption(prior.get("disruption"), stream(engine.seed, "disruption", at),
                      engine.config.get("world_fixture", {}).get("initial_disruption", "calm"))
    supplier = reliability(prior.get("supplier_reliability"), stream(engine.seed, "supplier", at))
    rows.append(dict(at=at, disruption=lane, supplier_reliability=supplier))
    state = lane["event_state"]
    band = "warning" if state == "false_alarm" or (state == "disruption" and lane["event_age"] == 0) else state
    fields = {"text": BULLETINS[band]}
    publish = getattr(engine._consumer, "publish_world_bulletin", None)
    if publish is None:
        engine.write("mail", "world_bulletin", {**fields, "roles": ["manager"]})
    else:
        publish(engine, fields)
    schedule_tick(engine, at + WEEK)


def schedule_tick(engine, at):
    engine._schedule(f"world:week:{at}", TICK, at, priority=-100, payload={})


def dispatch(engine, key, eta, fields, *, earliest=None):
    if not engine.config.get("world"):
        raise ValueError("Delivery simulation requires world opt-in")
    if type(eta) is not int or eta < 0:
        raise ValueError("Delivery commitment must be a nonnegative minute")
    at = eta if earliest is None else max(eta, earliest)
    if at < engine.now:
        raise ValueError("Delivery checkpoint must be at or after dispatch")
    event = f"world:transit:{key}"
    if not engine.has_event(event):
        engine._schedule(event, CHECK, at, priority=-90,
                        payload={"key": key, "previous_eta": eta, "fields": fields})


def complete_transit(engine, payload):
    current = engine.state["_world"][-1]
    eta = payload["previous_eta"]
    # A shipment checks conditions at its committed transit checkpoint. Its
    # reported disruption and supplier drift contribute separate delays.
    lane = current["disruption"]
    rel = current["supplier_reliability"]["state"]
    slip = {"reliable": 0, "wobbling": 3, "degraded": 7, "defunct": 14}[rel]
    delay = (WEEK if lane["event_state"] == "disruption" else 0) + slip * 1440
    delay = engine.config.get("world_fixture", {}).get("transit_delay_minutes", delay)
    if delay:
        engine._consumer.delivery_notice(engine, payload["fields"], eta)
    actual = engine.now + delay
    engine.state.setdefault("_world_deliveries", {})[payload["key"]] = {
        "checked_at": engine.now, "previous_eta": eta, "actual_arrival_at": actual,
    }
    engine._schedule(f"world:arrival:{payload['key']}", ARRIVAL, actual,
                     priority=-89, payload=payload["fields"])


def handle(engine, kind, payload):
    if kind == TICK:
        # Reserved names are checked against their canonical shape. A second
        # tick at the same minute would change the latent trajectory.
        if engine.event["id"] != f"world:week:{engine.now}" or (
                engine.state.get("_world") and engine.state["_world"][-1]["at"] + WEEK != engine.now):
            raise ValueError("Unexpected world tick")
        tick(engine)
    elif kind == CHECK:
        complete_transit(engine, payload)
    else:
        engine._consumer.delivery_arrival(engine, payload)
