"""Private call evidence and offline projections of a run checkpoint."""

from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from html import escape
import json
from math import ceil
import os
from pathlib import Path
import sqlite3
from stat import S_ISREG
from statistics import median


class CallFailed(Exception):
    """A recorded provider failure, including failure replay."""


def secure_database(path, *, create=False):
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0)
    with os.fdopen(os.open(path, os.O_RDONLY | flags, 0o600), "rb") as file:
        info = os.fstat(file.fileno())
        if info.st_uid != os.geteuid() or not S_ISREG(info.st_mode):
            raise PermissionError("Checkpoint must be a regular file owned by the run operator")
        os.fchmod(file.fileno(), 0o600)


@contextmanager
def snapshot(path):
    info = path.lstat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077 or not S_ISREG(info.st_mode):
        raise PermissionError("Inspection requires an owner-only checkpoint")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        yield db


def migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS call_journal ("
               "id INTEGER PRIMARY KEY, event INTEGER NOT NULL, slot TEXT NOT NULL, "
               "attempt INTEGER NOT NULL, data TEXT NOT NULL, UNIQUE(event, slot, attempt))")


def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def attempts(path, event, slot):
    with closing(sqlite3.connect(path)) as db:
        return [json.loads(data) for data, in db.execute(
            "SELECT data FROM call_journal WHERE event = ? AND slot = ? ORDER BY attempt",
            (event, slot))]


def invoke(path, event, slot, request, call, clock, *, replay=False, retry_timeout=False, replay_failure=False):
    prior = attempts(path, event["sequence"], slot)
    attempt = len(prior) + 1
    if replay and prior:
        last = prior[-1]
        if any(key not in last or last[key] != value for key, value in request.items()):
            raise RuntimeError("Handler replay changed a journaled call")
        if last["status"] == "returned":
            return last["response"]
        if replay_failure and last["status"] == "failed" and not (
            retry_timeout and last["timeout"] and last["attempt"] == 1
        ):
            raise CallFailed(last["error_type"])
    if call is None:
        raise CallFailed("Provider unavailable")
    row = {"event": event, "role": event["role"], **request,
           "attempt": attempt, "status": "started", "latency_seconds": None}
    with closing(sqlite3.connect(path)) as db:
        with db:
            cursor = db.execute("INSERT INTO call_journal (event, slot, attempt, data) "
                                "VALUES (?, ?, ?, ?)",
                                (event["sequence"], slot, attempt, encode(row)))
        started = clock()
        try:
            result = call()
        except Exception as exc:
            elapsed = clock() - started
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}", error_type=type(exc).__name__,
                       timeout=isinstance(exc, TimeoutError))
            finish(db, cursor.lastrowid, row, elapsed)
            if retry_timeout and attempt == 1 and row["timeout"]:
                return invoke(path, event, slot, request, call, clock,
                              replay=replay, retry_timeout=True, replay_failure=replay_failure)
            if replay_failure:
                raise CallFailed(row["error_type"]) from exc
            raise
        elapsed = clock() - started
        row.update(status="returned", response=result)
        finish(db, cursor.lastrowid, row, elapsed)
        return result


def finish(db, id, row, elapsed):
    row["latency_seconds"] = elapsed
    with db:
        db.execute("UPDATE call_journal SET data = ? WHERE id = ?", (encode(row), id))


def read(path):
    with snapshot(Path(path)) as db:
        tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        calls = [json.loads(data) for data, in db.execute(
            "SELECT data FROM call_journal ORDER BY id")] if "call_journal" in tables else []
        records = [operation for data, in db.execute("SELECT data FROM writes ORDER BY rowid")
                   if (operation := json.loads(data))["status"] == "confirmed"]
        decisions = {seq: json.loads(data) for seq, data in db.execute(
            "SELECT operation, data FROM decisions")} if "decisions" in tables else {}

    by_write = {operation["retry_id"]: operation for operation in records}
    by_event = defaultdict(list)
    for operation in records:
        by_event[int(operation["retry_id"].split("-")[1])].append(operation)
    for call in calls:
        if "response" in call:
            result = call.pop("response")
            if call["tier"] in {"jev", "model"} and isinstance(result, dict):
                call.update(response=result, raw=result.get("raw"), tokens=result.get("tokens"),
                            cost=result.get("cost"))
            else:
                call["raw"] = result
        related = by_event.get(call["event"]["sequence"], [])
        if call["tier"] == "api":
            operation = by_write.get(call["operation"])
            related = [operation] if operation is not None else []
        call["native_ids"] = [operation["target_id"] for operation in related]
        call["native_records"] = [{"system": operation["system"], "native_id": operation["target_id"]}
                                  for operation in related]
    return calls, records, decisions


def metrics(data, day_minutes):
    calls, records, _ = data
    systems = Counter()
    records_by_day = defaultdict(Counter)
    counts = Counter()
    unresolved = Counter()
    latencies = defaultdict(list)
    wall = defaultdict(float)
    costs = defaultdict(list)
    tokens = 0
    for operation in records:
        record = operation["record"]
        systems[operation["system"]] += 1
        records_by_day[str(record["at"] // day_minutes)][record["kind"]] += 1
    for call in calls:
        tier = call["tier"]
        latency = call["latency_seconds"]
        if latency is None:
            unresolved[tier] += 1
            continue
        counts[tier] += 1
        latencies[tier].append(latency)
        wall[str(call["event"]["completed_at"] // day_minutes)] += latency
        if tier in {"jev", "model"}:
            costs[tier].append(call.get("cost"))
            tokens += call.get("tokens") or 0
    return {
        "records_by_system": dict(systems), "records_by_day": dict(records_by_day),
        "calls_by_tier": dict(counts), "unresolved_attempts_by_tier": dict(unresolved),
        "provider_cost": {tier: sum(cost for cost in values if cost is not None)
                          if any(cost is not None for cost in values) else None
                          for tier, values in costs.items()},
        "provider_cost_unknown_attempts": {tier: values.count(None) for tier, values in costs.items()},
        "tokens": tokens, "wall_seconds_by_day": dict(wall),
        "call_latency_seconds": {
            tier: {"median": median(values), "p95": sorted(values)[ceil(.95 * len(values)) - 1]}
            for tier, values in latencies.items()
        },
    }


def viewer(data, *, private_truth=None):
    calls, records, decisions = data
    by_event = defaultdict(list)
    for call in calls:
        by_event[call["event"]["sequence"]].append(call)

    def link(label, target, caption):
        return f'<a data-link="{escape(label)}" href="#{target}">{escape(caption)}</a>'

    def show(value):
        # A prompt is already text. Display it verbatim instead of JSON-encoding
        # it a second time inside the attempt.
        return f"<pre>{escape(value if isinstance(value, str) else encode(value))}</pre>"

    sections = []
    timeline = []
    if private_truth is not None:
        timeline.append('<li>' + link("private", "private-world", "Private evaluator truth") + "</li>")
        sections.append('<section id="private-world"><h2>Private evaluator truth</h2>'
                        + show(private_truth) + "</section>")
    used_ids = Counter(op["target_id"] for op in records)
    for index, operation in enumerate(records):
        seq = int(operation["retry_id"].split("-")[1])
        record = operation["record"]
        id = operation["target_id"]
        system = operation["system"]
        label = id if used_ids[id] == 1 else f"{system}/{id}"
        if used_ids[id] > 1:
            label += f" ({index + 1})"
        timeline.append(f'<li>{record["at"]} · ' + link(
            label, f"record-{index}", f"{system}/{id} · {record['kind']}") + "</li>")
        related = [call for call in by_event[seq]
                   if call["tier"] != "api" or call["operation"] == operation["retry_id"]]
        event = operation.get("event") or next((call["event"] for call in related), None)
        decision = decisions.get(seq)
        route = decision.get("route") if decision else ("rule" if event else None)
        if route is None:
            route = next((call["tier"] for call in reversed(related)
                          if call["tier"] in {"jev", "model"}), "unavailable")
        explanation = "".join(
            f"<h3>{escape(call['tier'])} attempt {call['attempt']}</h3>"
            + (show(call["prompt"]) if "prompt" in call else "")
            + show({key: value for key, value in call.items() if key != "prompt"})
            for call in related)
        sections.extend([
            f'<section id="record-{index}"><h2>Verified readback</h2>{show(record)}'
            + link("decision", f"decision-{index}", "Decision and evidence") + "</section>",
            f'<section id="decision-{index}"><h2>Event and action</h2>'
            + show({"event": event, "route": route or "unavailable",
                    "input": decision["input"] if decision else operation["action"],
                    "action": operation["action"],
                    "observations": [call["raw"] for call in related
                                     if call["tier"] == "observation" and "raw" in call]})
            + link("call", f"call-{index}", "Call attempts") + "</section>",
            f'<section id="call-{index}"><h2>Call attempts</h2>'
            + (explanation or "<p>No call evidence was recorded for this record.</p>") + "</section>",
        ])
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
            '<title>Run evidence</title><style>body{font:16px system-ui;margin:2rem auto;max-width:75rem;'
            'padding:0 1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}section{display:none}'
            '#timeline,section:target{display:block}section{padding-bottom:1rem}</style>'
            '<h1>Run evidence</h1><p>Private operator evidence</p>'
            '<section id="timeline"><h2>Timeline</h2><ol>' + "".join(timeline)
            + '</ol></section>' + "".join(sections) + '</html>')
