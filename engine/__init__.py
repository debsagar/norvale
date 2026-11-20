"""Durable minute-based dispatch through injected consumers and transports."""

from contextlib import closing, contextmanager, ExitStack
from copy import deepcopy
import fcntl
from hashlib import sha256
from heapq import heappop, heappush
import json
from pathlib import Path
import sqlite3

from . import _journal
from . import world
from ._extension import SECTIONS, name, validate_entries, validate_event, validate_json


DAY = 1440


class DeferCompletion(Exception):
    """Retry a completion edge one business day later under the same identity."""


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _matches_json(left, right):
    """Compare native JSON fields. Decimal-valued counts may read back as integers."""
    if type(left) is not type(right):
        return type(left) in (int, float) and type(right) in (int, float) and left == right
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_matches_json(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_matches_json(a, b) for a, b in zip(left, right))
    return left == right


def _command_digest(command):
    return sha256(_json(command).encode()).hexdigest()


def _order_key(number):
    # SQLite INTEGER is limited to 64 bits. Preserve Python integer ordering
    # for minutes and priorities using signed, length-prefixed magnitude.
    magnitude = abs(number).to_bytes((number.bit_length() + 7) // 8, "big")
    key = len(magnitude).to_bytes(4, "big") + magnitude
    if number < 0:
        return b"\x00" + bytes(byte ^ 255 for byte in key)
    return b"\x01" + key


def _calendars(config):
    shifts = config.get("shifts", {})
    if not isinstance(shifts, dict):
        raise ValueError("shifts must map roles to daily windows")
    calendars = {}
    for role, windows in shifts.items():
        if not isinstance(role, str) or not role.strip():
            raise ValueError("Shift roles must be nonblank strings")
        if not isinstance(windows, (list, tuple)) or not windows:
            raise ValueError(f"No shift windows for role {role!r}")
        for window in windows:
            if (not isinstance(window, (list, tuple)) or len(window) != 2
                    or any(type(minute) is not int for minute in window)
                    or not 0 <= window[0] < window[1] <= DAY):
                raise ValueError("Shifts require half-open integer windows within a day")
        ordered = sorted(tuple(window) for window in windows)
        if any(end > start for (_, end), (start, _) in zip(ordered, ordered[1:])):
            raise ValueError(f"Overlapping shift windows for role {role!r}")
        calendars[role] = ordered
    return calendars


class Engine:
    def __init__(self, directory, consumer, *, seed, native, providers,
                 monotonic, boundary=None, adapters=None, start=0, standalone_extensions=True):
        if type(start) is not int or start < 0:
            raise ValueError("start must be a nonnegative integer minute")
        self._consumer = consumer
        self._native = native
        self._boundary = boundary
        self._adapters = dict(adapters or {})
        self._transports = {}
        self.config = deepcopy(consumer.config)
        if not isinstance(self.config, dict):
            raise ValueError("Consumer configuration must be a dictionary")
        world.validate(self.config)
        self._calendars = _calendars(self.config)
        cls = type(consumer)
        identity = getattr(consumer, "resume_identity", None)
        if identity is not None and (not isinstance(identity, str) or not identity.strip()):
            raise ValueError("resume_identity must be a nonblank string")
        self._consumer_identity = [cls.__module__, cls.__qualname__, identity]
        self.seed = seed
        self.providers = providers
        self.monotonic = monotonic
        self.state = {}
        self._active = None
        self.event = None
        self.event_started_at = None
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._directory = directory
        self._database = directory / "engine.sqlite3"
        _journal.secure_database(self._database, create=True)
        with self._exclusive(), self._storage() as db, db:
            db.execute("BEGIN")
            db.execute("CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, data TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS writes (id TEXT PRIMARY KEY, data TEXT)")
            initial = {"consumer": self._consumer_identity, "now": start, "sequence": 0,
                       "queue": [], "history": [], "state": {}, "seed": seed,
                       "config": self.config}
            created = db.execute("INSERT OR IGNORE INTO checkpoint VALUES (1, ?)", (_json(initial),)).rowcount
            db.execute("CREATE TABLE IF NOT EXISTS extension_admission (id INTEGER PRIMARY KEY, standalone INTEGER NOT NULL)")
            # Only the owning runtime may enroll a checkpoint for offline commands.
            # Once it requires application validation, a reopen cannot loosen that rule.
            db.execute("INSERT INTO extension_admission VALUES (1, ?) ON CONFLICT(id) DO UPDATE "
                       "SET standalone = MIN(standalone, excluded.standalone)", (int(standalone_extensions),))
            self._restore(self._load())
            self._migrate()
            if created and self.config.get("world"):
                self._enqueue(self._scheduled_edge(f"world:week:{start}", world.TICK,
                                                    start, {}, priority=-100))
                self._save()

    @contextmanager
    def _storage(self):
        with closing(sqlite3.connect(self._database)) as db:
            self._db = db
            self._pending = {}
            try:
                yield db
            finally:
                self._pending.clear()
                self._db = None

    def _migrate(self):
        """Import the old checkpoint inside the caller's write transaction."""
        _journal.migrate(self._db)
        self._db.execute("CREATE INDEX IF NOT EXISTS writes_by_system ON writes(json_extract(data, '$.system'))")
        checkpoint = self._checkpoint
        if "queue" not in checkpoint:
            return
        db = self._db
        db.execute("CREATE TABLE event_ids (id TEXT PRIMARY KEY, sequence INTEGER)")
        db.execute("CREATE TABLE queue (sequence INTEGER PRIMARY KEY, minute BLOB NOT NULL, "
                   "priority BLOB NOT NULL, role TEXT, edge TEXT NOT NULL)")
        db.execute("CREATE INDEX queue_order ON queue (minute, priority, sequence)")
        db.execute("CREATE INDEX queue_role ON queue (role)")
        db.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, entry TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS decisions (operation INTEGER PRIMARY KEY, "
                   "event_id TEXT UNIQUE NOT NULL, data TEXT NOT NULL)")
        for edge in checkpoint["queue"]:
            db.execute("INSERT INTO event_ids VALUES (?, ?)", (_json(edge[4]), edge[2]))
            self._insert_edge(edge)
        for entry in checkpoint["history"]:
            db.execute("INSERT OR IGNORE INTO event_ids VALUES (?, NULL)", (_json(entry["id"]),))
            db.execute("INSERT INTO history (entry) VALUES (?)", (_json(entry),))
        migrated = {key: value for key, value in checkpoint.items()
                    if key not in {"queue", "history", "extensions"}}
        migrated["history_end"] = len(checkpoint["history"])
        migrated["revision"] = 0
        if self._extensions:
            migrated["extensions"] = {
                id: {**{key: command[key] for key in SECTIONS},
                     "digest": _command_digest(command)}
                for id, command in self._extensions.items()
            }
        db.execute("UPDATE checkpoint SET data = ? WHERE id = 1", (_json(migrated),))
        self._restore(migrated)

    @contextmanager
    def _lock(self, filename, *, blocking=True):
        with (self._directory / filename).open("a") as lock:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(lock, flags)
            yield

    @contextmanager
    def _exclusive(self):
        # Release the gate while processing the edge, so a waiting extension
        # can reserve the next boundary even during a long controller run.
        with ExitStack() as stack:
            with self._lock("engine-extension.lock"):
                stack.enter_context(self._lock("engine.lock"))
            yield

    def _load(self):
        return json.loads(self._db.execute("SELECT data FROM checkpoint WHERE id = 1").fetchone()[0])

    def _restore(self, checkpoint):
        if "consumer" not in checkpoint:
            raise ValueError("Legacy checkpoint has no consumer identity")
        if checkpoint["consumer"] != self._consumer_identity:
            raise ValueError("Resume requires the original consumer identity")
        if (checkpoint["seed"] != self.seed
                or _json(checkpoint["config"]) != _json(self.config)):
            raise ValueError("Resume requires the original seed and configuration")
        self.now = checkpoint["now"]
        self._sequence = checkpoint["sequence"]
        self._staff_available = deepcopy(checkpoint.get("staff_available", {}))
        self._pending.clear()
        self.state.clear()
        self.state.update(checkpoint["state"])
        self._checkpoint = deepcopy(checkpoint)
        self._extensions = deepcopy(checkpoint.get("extensions", {}))
        self._set_catalog()

    def _set_catalog(self):
        self._catalog = {}
        for section in SECTIONS:
            mapping = self._checkpoint["config"].get(section)
            self._catalog[section] = (
                deepcopy(mapping) if isinstance(mapping, dict)
                and all(isinstance(spec, dict) for spec in mapping.values()) else {}
            )
        self._added = {section: {} for section in SECTIONS}
        for entry in self._extensions.values():
            for section in SECTIONS:
                self._added[section].update(entry[section])
        for section in SECTIONS:
            self._catalog[section].update(self._added[section])
        roles = self._catalog["roles"]
        for role, windows in self._checkpoint["config"].get("shifts", {}).items():
            roles.setdefault(role, {})["shifts"] = deepcopy(windows)
        self._staffing = {role: spec["staff"] for role, spec in roles.items() if "staff" in spec}
        if any(type(n) is not int or n < 1 for n in self._staffing.values()):
            raise ValueError("Role staff must be a positive integer")
        shifts = {role: spec["shifts"] for role, spec in roles.items() if "shifts" in spec}
        shifts.update(self._checkpoint["config"].get("shifts", {}))
        self._calendars = _calendars({"shifts": shifts})

    @property
    def catalog(self):
        """Return a detached snapshot of the catalog at this object's current boundary."""
        return deepcopy(self._catalog)

    def extend(self, extension_id, *, systems=None, roles=None, products=None, events=None, validate=None):
        """Commit append-only catalog entries and events in one checkpoint update."""
        self._extend(extension_id, systems, roles, products, events, validate=validate)

    def _extend(self, extension_id, systems, roles, products, events, *, from_command=False, validate=None):
        if not name(extension_id):
            raise ValueError("Extension ID must be a nonblank string")
        if self._active is not None:
            raise RuntimeError("Submit extensions outside event handlers")
        command = {key: {} if value is None else value
                   for key, value in zip(SECTIONS, (systems, roles, products))}
        command["events"] = [] if events is None else events
        validate_json(command)
        try:
            command = json.loads(_json(command))
        except TypeError as exc:
            raise ValueError("Extension content must be JSON-compatible") from exc
        if not isinstance(command["events"], list):
            raise ValueError("events must be an array")
        with self._lock("engine-extension.lock"), self._lock("engine.lock"), self._storage() as db:
            checkpoint = self._load()
            if from_command:
                self._consumer_identity = checkpoint.get("consumer")
                self.seed = checkpoint["seed"]
                self.config = checkpoint["config"]
            if from_command or checkpoint != self._checkpoint:
                self._restore(checkpoint)
            with db:
                db.execute("BEGIN")
                self._migrate()
            digest = _command_digest(command)
            if extension_id in self._extensions:
                if self._extensions[extension_id]["digest"] != digest:
                    raise ValueError(f"Conflicting extension ID: {extension_id!r}")
                return
            if from_command:
                enrolled = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'extension_admission'").fetchone()
                if not enrolled or not db.execute("SELECT 1 FROM extension_admission WHERE id = 1 AND standalone = 1").fetchone():
                    raise ValueError("Submit new extensions through the owning application")
            validate_entries(command, self._catalog, self._calendars, self._adapters)
            if validate is not None:
                validate(self.catalog)
            if any(db.execute("SELECT 1 FROM queue WHERE role = ? LIMIT 1", (_json(role),)).fetchone()
                   for role in command["roles"]):
                raise ValueError("Extension cannot replace a role used by queued work")
            for system in command["systems"]:
                used = db.execute("SELECT 1 FROM writes WHERE json_extract(data, '$.system') = "
                                  "json_extract(?, '$') LIMIT 1", (_json(system),)).fetchone()
                if used:
                    raise ValueError("Extension cannot replace a system used by prior writes")
            try:
                self._extensions[extension_id] = {
                    **{key: command[key] for key in SECTIONS}, "digest": digest,
                }
                self._set_catalog()
                for event in command["events"]:
                    validate_event(event)
                    world.check_event_name(event["id"], event["kind"])
                    self._enqueue(self._scheduled_edge(**event))
                self._save()
            except BaseException:
                self._restore(self._load())
                raise

    def _insert_edge(self, edge):
        at, priority, sequence, _, _, _, _, _, role, _ = edge
        self._db.execute("INSERT INTO queue VALUES (?, ?, ?, ?, ?)",
                         (sequence, _order_key(at), _order_key(priority), _json(role), _json(edge)))

    def _save(self, *, remove=None, edge=None, history=None):
        checkpoint = {**self._checkpoint, "now": self.now, "sequence": self._sequence,
                      "state": self.state, "revision": self._checkpoint["revision"] + 1}
        if self._staff_available:
            checkpoint["staff_available"] = self._staff_available
        if self._extensions:
            checkpoint["extensions"] = self._extensions
        # Hold no SQLite write transaction while a handler records its
        # independent native write intents and decisions.
        db = self._db
        with db:
            if remove is not None:
                db.execute("DELETE FROM queue WHERE sequence = ?", (remove,))
            if edge is not None:
                self._insert_edge(edge)
                if edge[3] == 3:
                    db.execute("UPDATE writes SET data = json_set(data, '$.next_attempt', ?) "
                               "WHERE id GLOB ? AND json_extract(data, '$.status') = 'pending'",
                               (edge[0], f"write-{remove}-*"))
            for id, queued in self._pending.items():
                db.execute("INSERT INTO event_ids VALUES (?, ?)", (_json(id), queued[2]))
                self._insert_edge(queued)
            if history is not None:
                cursor = db.execute("INSERT INTO history (entry) VALUES (?)", (_json(history),))
                checkpoint["history_end"] = cursor.lastrowid
            data = _json(checkpoint)
            db.execute("UPDATE checkpoint SET data = ? WHERE id = 1", (data,))
        self._checkpoint = json.loads(data)
        self._pending.clear()

    def _shift_start(self, at, role=None):
        """Return the first on-shift minute at or after at, without scheduling."""
        if type(at) is not int or at < self.now:
            raise ValueError("at must be an integer minute at or after now")
        if role is not None and (not isinstance(role, str) or not role.strip()):
            raise ValueError("role must be a nonblank string or None")
        windows = self._calendars.get(role)
        if windows is None:
            return at
        day, minute = divmod(at, DAY)
        for start, end in windows:
            if minute < end:
                return day * DAY + max(start, minute)
        return (day + 1) * DAY + windows[0][0]

    shift_start = _shift_start

    def _completion(self, start, duration, role, elapsed):
        windows = self._calendars.get(role)
        if elapsed or windows is None or duration == 0:
            return start + duration
        daily_work = sum(end - begin for begin, end in windows)
        # Leave at least one work minute to find the exact closing edge.
        days, remaining = divmod(duration - 1, daily_work)
        at = start + days * DAY
        remaining += 1
        while True:
            day, minute = divmod(at, DAY)
            end = next(end for begin, end in windows if begin <= minute < end)
            available = end - minute
            if remaining <= available:
                return at + remaining
            remaining -= available
            at = self._shift_start(day * DAY + end, role)

    def shift_intervals(self, role, start, finish):
        """Scheduled work windows within a completed business-time interval."""
        if not (type(start) is int and type(finish) is int and 0 <= start <= finish):
            raise ValueError("Expected an ordered pair of business minutes")
        windows = self._calendars.get(role, ((0, DAY),))
        intervals = []
        for day in range(start // DAY, finish // DAY + 1):
            for opening, closing in windows:
                left = max(start, day * DAY + opening)
                right = min(finish, day * DAY + closing)
                if left < right:
                    intervals.append([left, right])
        return intervals

    def available_start(self, at, role):
        """Earliest start after already-booked staff work, inside the role's shift."""
        start = self._shift_start(at, role)
        slots = self._staff_available.get(role, ())
        if role in self._staffing and len(slots) >= self._staffing[role]:
            return self._shift_start(max(start, slots[0]), role)
        return start

    def _book_staff(self, at, duration, role):
        start = self.available_start(at, role)
        available = self._staff_available.setdefault(role, [])
        if len(available) >= self._staffing[role]:
            heappop(available)
        finish = self._completion(start, duration, role, False)
        heappush(available, finish)
        return start, finish

    def has_event(self, id):
        """Whether this full event ID is queued or has already dispatched."""
        if not isinstance(id, str):
            return False
        if self._db is not None:
            return id in self._pending or self._has_saved_event(self._db, id)
        with closing(sqlite3.connect(self._database)) as db:
            return self._has_saved_event(db, id)

    def _has_saved_event(self, db, id):
        # Migrated completed events have no recoverable sequence. New IDs are
        # visible to this instance up to its last synchronized boundary.
        return db.execute("SELECT 1 FROM event_ids WHERE id = ? AND "
                          "(sequence IS NULL OR sequence < ?)",
                          (_json(id), self._checkpoint["sequence"])).fetchone() is not None

    def schedule(self, id, kind, at, *, payload, priority=0, duration=0, role=None,
                 elapsed=False, book_staff=False):
        """Enqueue work. Role work counts shift minutes unless elapsed is true."""
        world.check_event_name(id, kind)
        return self._schedule(id, kind, at, payload=payload, priority=priority,
                              duration=duration, role=role, elapsed=elapsed, book_staff=book_staff)

    def _schedule(self, id, kind, at, *, payload, priority=0, duration=0, role=None,
                  elapsed=False, book_staff=False):
        params = (id, kind, at, payload, priority, duration, role, elapsed)
        if self._active is not None:
            return self._enqueue(self._scheduled_edge(*params), book_staff=book_staff)
        if book_staff:
            raise ValueError("Booking staff requires an active event")
        with self._lock("engine-controller.lock", blocking=False), self._exclusive(), self._storage():
            if self._load() != self._checkpoint:
                raise RuntimeError("Checkpoint changed; reopen the engine")
            try:
                sequence = self._enqueue(self._scheduled_edge(*params))
                self._save()
            except BaseException:
                self._restore(self._load())
                raise
        return sequence

    def _scheduled_edge(self, id, kind, at, payload, priority=0, duration=0, role=None,
                        elapsed=False):
        if not isinstance(id, str) or not id.strip():
            raise ValueError("Event ID must be a nonblank string")
        if type(priority) is not int or type(duration) is not int or duration < 0:
            raise ValueError("priority and duration must be integers, with duration nonnegative")
        if type(elapsed) is not bool:
            raise ValueError("elapsed must be a boolean")
        start = self._shift_start(at, role)
        # Phase 2 admits work to a staffed queue. Phase 0 starts admitted work.
        phase = 2 if duration and not elapsed and role in self._staffing else 0
        return (start, priority, self._sequence, phase, id, kind,
                json.loads(_json(payload)), duration, role,
                self._completion(start, duration, role, elapsed))

    def _enqueue(self, edge, *, book_staff=False):
        id = edge[4]
        if self.has_event(id):
            raise ValueError(f"Duplicate event ID: {id!r}")
        if book_staff and edge[3] == 2:
            start, finish = self._book_staff(edge[0], edge[7], edge[8])
            edge = (start, edge[1], edge[2], 0, *edge[4:9], finish)
        self._pending[id] = edge
        self._sequence += 1
        return edge[2]

    def run(self, *, until, advance=False):
        """Process edges through the inclusive cutoff, optionally recording idle time."""
        if type(until) is not int or until < 0:
            raise ValueError("until must be a nonnegative integer minute")
        if self._active is not None:
            raise RuntimeError("Cannot dispatch recursively")
        with self._lock("engine-controller.lock", blocking=False), self._storage() as db:
            while True:
                with self._exclusive():
                    checkpoint = self._load()
                    if self._checkpoint != checkpoint:
                        if self._extensions == checkpoint.get("extensions", {}):
                            raise RuntimeError("Checkpoint changed; reopen the engine")
                        self._restore(checkpoint)
                    row = db.execute("SELECT edge FROM queue WHERE minute <= ? "
                                     "ORDER BY minute, priority, sequence LIMIT 1",
                                     (_order_key(until),)).fetchone()
                    try:
                        if row is not None:
                            self._step(json.loads(row[0]))
                        else:
                            if advance and until > self.now:
                                self.now = until
                                self._save()
                            return
                    except BaseException:
                        self._restore(self._load())
                        raise

    def _step(self, edge):
        self.now, priority, seq, phase, id, kind, payload, duration, role, other_at = edge
        if phase == 2:
            # Admit staffed work in arrival order, including any earlier bookings.
            at, other_at = self._book_staff(self.now, duration, role)
            if at > self.now:
                self._save(remove=seq, edge=(at, priority, seq, 0, id, kind, payload,
                                             duration, role, other_at))
                return
            phase = 0
        if phase == 0:
            self._save(remove=seq,
                       edge=(other_at, priority, seq, 1, id, kind, payload, duration, role, self.now),
                       history={"id": id, "phase": "start", "at": self.now})
            return
        self._active = seq
        self._active_id = id
        self._active_role = role
        # A deferred edge retains the two original business timestamps.
        self.event_started_at, self._completion_at = other_at if phase == 3 else (other_at, self.now)
        self.event = dict(id=id, sequence=seq, kind=kind, role=role, duration=duration,
                          at=self.event_started_at, priority=priority)
        self._write_index = self._provider_index = self._observation_index = 0
        try:
            if kind in (world.TICK, world.CHECK, world.ARRIVAL):
                world.handle(self, kind, deepcopy(payload))
            elif kind != "noop":
                self._consumer.handle(self, kind, deepcopy(payload))
            if kind != "noop":
                self._check_writes()
        except DeferCompletion:
            at = self.now
            timestamps = (self.event_started_at, self._completion_at)
            self._restore(self._checkpoint)
            self.now = at
            self._save(remove=seq, edge=(at + DAY, priority, seq, 3, id, kind, payload,
                                         duration, role, timestamps))
            return
        finally:
            self._active = self._active_id = self._active_role = None
            self.event = None
            self.event_started_at = None
        self._save(remove=seq, history={"id": id, "phase": "complete", "at": self.now})

    def history(self):
        with closing(sqlite3.connect(self._database)) as db:
            return [json.loads(entry) for entry, in db.execute(
                "SELECT entry FROM history WHERE id <= ? ORDER BY id",
                (self._checkpoint["history_end"],))]

    def _transport(self, system):
        spec = self._added["systems"].get(system)
        if spec is None:
            return self._native
        if system not in self._transports:
            factory = self._adapters.get(spec["adapter"])
            if factory is None:
                raise RuntimeError(f"No host transport for extension system {system!r}")
            self._transports[system] = factory(system, deepcopy(spec))
        return self._transports[system]

    def _scopes(self, role):
        scopes = self._catalog["roles"].get(role, {}).get("permissions")
        return scopes if isinstance(scopes, list) else []

    def observe(self, role, *, as_of):
        """Read transport records visible to a role at the given business minute."""
        if type(as_of) is not int or as_of < 0:
            raise ValueError("as_of must be a nonnegative integer minute")
        if self._active is not None:
            if role is None or role != self._active_role:
                raise ValueError("Observation requires the active event role")
            if as_of > self.now:
                raise ValueError("An active event cannot observe future records")
        scopes = self._scopes(role)
        base_visible = role not in self._added["roles"] or "observe" in scopes
        selected = {scope.removeprefix("observe:") for scope in scopes
                    if isinstance(scope, str) and scope.startswith("observe:")}
        added_systems = self._added["systems"]

        def visible(transport, allowed):
            return [row for row in transport.records()
                    if allowed(row) and row["at"] <= as_of
                    and ("roles" not in row or (isinstance(row["roles"], list) and role in row["roles"]))]

        def read(system, transport, allowed):
            if self._active is None:
                return visible(transport, allowed)
            # Persist only the authorized projection of a bulk response.
            self._observation_index += 1
            return _journal.invoke(
                self._database, self._trace_event(), f"observation:{self._observation_index}",
                {"tier": "observation", "method": "records", "system": system, "as_of": as_of},
                lambda: visible(transport, allowed), self.monotonic, replay=True,
            )

        rows = []
        if base_visible or selected.difference(added_systems):
            rows.extend(read(None, self._native, lambda row: row.get("system") not in added_systems
                             and (base_visible or row.get("system") in selected)))
        for system in added_systems:
            if system in selected:
                rows.extend(read(system, self._transport(system),
                                 lambda row: row.get("system", system) == system))
        return deepcopy(rows)

    def _trace_event(self):
        return {**self.event, "completed_at": self.now}

    def call_provider(self, tier, prompt, tools):
        if self._active is None or tier not in {"jev", "model"}:
            raise ValueError("Provider calls require an active event and a supported tier")
        if self._write_index:
            raise RuntimeError("Provider calls must precede native writes")
        self._provider_index += 1

        def request():
            return self.providers.call(tier, prompt, deepcopy(tools))

        return _journal.invoke(
            self._database, self._trace_event(), f"provider:{self._provider_index}",
            {"tier": tier, "prompt": prompt, "tools": deepcopy(tools)},
            request if self.providers is not None else None, self.monotonic,
            replay=True, retry_timeout=True, replay_failure=True,
        )

    def _api(self, transport, operation, method, key, *args):
        system = operation["system"]
        retry_id = operation["retry_id"]
        return _journal.invoke(
            self._database, self._trace_event(), f"{retry_id}:{method}",
            {"tier": "api", "system": system, "operation": retry_id,
             "method": method, "key": key, "arguments": [system, key, *deepcopy(args)]},
            lambda: getattr(transport, method)(system, key, *args), self.monotonic,
        )

    def _inspection(self):
        if self._active is not None:
            raise PermissionError("Call evidence is available outside event handlers")
        return _journal.read(self._database)

    def trace(self):
        """Return private operator call evidence, linked to confirmed readbacks."""
        return self._inspection()[0]

    def metrics(self):
        return _journal.metrics(self._inspection(), DAY)

    def viewer(self, include_private=False):
        return _journal.viewer(self._inspection(),
                               private_truth=self.private_truth() if include_private else None)

    def _decision_key(self, event_id):
        if self._active is None:
            raise RuntimeError("Consumer decisions require an active event handler")
        if event_id != self._active_id:
            raise ValueError("Decision event ID must match the active event")
        return self._active, event_id

    def load_consumer_decision(self, event_id):
        """Return the committed decision for this active event, if any."""
        key = self._decision_key(event_id)
        with closing(sqlite3.connect(self._database)) as db:
            row = db.execute("SELECT data FROM decisions WHERE operation = ? AND event_id = ?",
                             key).fetchone()
        return json.loads(row[0]) if row is not None else None

    def save_consumer_decision(self, event_id, decision):
        """Commit a decision and its input before output, or return the committed value."""
        existing = self.load_consumer_decision(event_id)
        if existing is not None:
            return existing
        if not isinstance(decision, dict) or "input" not in decision:
            raise ValueError("Decision must include its complete input")
        key = self._decision_key(event_id)
        with closing(sqlite3.connect(self._database)) as db, db:
            if self._write_index or db.execute("SELECT 1 FROM writes WHERE id GLOB ? LIMIT 1",
                                               (f"write-{self._active}-*",)).fetchone():
                raise RuntimeError("Commit the consumer decision before native writes")
            encoded = _json(decision)
            db.execute("INSERT INTO decisions VALUES (?, ?, ?)", (*key, encoded))
        return json.loads(encoded)

    def consumer_write_prefix(self, event_id):
        """Return this active event's ordered immutable intents for legacy replay."""
        self._decision_key(event_id)
        prefix = f"write-{self._active}-"
        with closing(sqlite3.connect(self._database)) as db:
            operations = [json.loads(data) for data, in db.execute(
                "SELECT data FROM writes WHERE id GLOB ?", (prefix + "*",))]
        operations.sort(key=lambda op: int(op["retry_id"][len(prefix):]))
        if any(op["retry_id"] != f"{prefix}{index}" for index, op in enumerate(operations, 1)):
            raise RuntimeError("Incomplete native write prefix")
        return operations

    def _extractor_snapshot(self, as_of):
        if self._active is not None:
            raise PermissionError("Extractor inspection is available outside event handlers")
        with _journal.snapshot(self._database) as db:
            checkpoint = json.loads(db.execute("SELECT data FROM checkpoint WHERE id = 1").fetchone()[0])
            cutoff = checkpoint["now"] if as_of is None else as_of
            if type(cutoff) is not int or not 0 <= cutoff <= checkpoint["now"]:
                raise ValueError("Extractor cutoff must be a committed business minute")
            grade = getattr(self._consumer, "grade_extraction", None)
            if grade is None:
                return (), {}, checkpoint, cutoff
            completed = {
                entry["id"] for data, in db.execute(
                    "SELECT entry FROM history WHERE id <= ? ORDER BY id", (checkpoint["history_end"],))
                if (entry := json.loads(data))["phase"] == "complete" and entry["at"] <= cutoff
            }
            records = [
                operation["record"] for data, in db.execute("SELECT data FROM writes ORDER BY rowid")
                if (operation := json.loads(data))["status"] == "confirmed"
                and operation.get("event", {}).get("id") in completed and operation["record"]["at"] <= cutoff
            ]
        questions, answers = grade(records, f"business minute {cutoff}")
        return questions, answers, checkpoint, cutoff

    def extractor_questions(self, *, as_of=None):
        """Return public question IDs and text through the requested cutoff."""
        questions, _, _, _ = self._extractor_snapshot(as_of)
        return list(questions)

    def private_truth(self, *, as_of=None):
        """Owner-only evaluator state through the requested cutoff."""
        _, answers, checkpoint, cutoff = self._extractor_snapshot(as_of)
        return {**world.truth(checkpoint["state"], self.config, cutoff), "answers": answers}

    def export(self):
        """Export the native records confirmed by this engine, in write order."""
        with closing(sqlite3.connect(self._database)) as db:
            operations = (json.loads(data) for data, in db.execute(
                "SELECT data FROM writes ORDER BY rowid"))
            records = []
            for operation in operations:
                if operation["status"] == "confirmed":
                    record = operation["record"]
                    record.pop("retry_id", None)
                    records.append(record)
            return {"records": records}

    def _check_writes(self):
        prefix = f"write-{self._active}-"
        with closing(sqlite3.connect(self._database)) as db:
            rows = db.execute("SELECT data FROM writes WHERE id GLOB ?", (prefix + "*",))
            operations = [json.loads(data) for data, in rows]
        if len(operations) != self._write_index or any(
            operation["status"] != "confirmed" for operation in operations
        ):
            raise RuntimeError("Handler must replay and confirm all of its writes")

    def _save_operation(self, operation):
        with closing(sqlite3.connect(self._database)) as db, db:
            db.execute("INSERT INTO writes VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                       (operation["retry_id"], _json(operation)))

    def _notify(self, stage, operation):
        if self._boundary is not None:
            self._boundary(stage, deepcopy(operation))

    def write(self, system, kind, fields):
        """Persist a handler write intent, reconcile it and return verified readback."""
        return self._record(system, kind, fields)

    def confirm(self, system, kind, native_id, fields):
        """Journal readback of an existing native record without sending a write."""
        if not name(native_id):
            raise ValueError("A native record ID is required")
        at = fields.get("at", self.now)
        if type(at) is not int or not 0 <= at <= self.now:
            raise ValueError("Readback time must be an observable business minute")
        return self._record(system, kind, fields, native_id=native_id, at=at)

    def _record(self, system, kind, fields, *, native_id=None, at=None):
        if self._active is None:
            raise RuntimeError("write requires an active event handler")
        role = self._active_role
        if role in self._added["roles"] or system in self._added["systems"]:
            scopes = self._scopes(role)
            if (f"write:{system}" not in scopes
                    and (":" in system or f"write:{system}:{kind}" not in scopes)):
                raise PermissionError(f"Role {role!r} cannot write {system}/{kind}")
        transport = self._transport(system)
        self._write_index += 1
        retry_id = f"write-{self._active}-{self._write_index}"
        encoded = _json({**fields, "kind": kind, "at": self._completion_at if at is None else at})
        document = json.loads(encoded)
        with closing(sqlite3.connect(self._database)) as db:
            row = db.execute("SELECT data FROM writes WHERE id = ?", (retry_id,)).fetchone()
        if row is None:
            operation = {
                "retry_id": retry_id, "system": system, "action": document,
                "event": {**self.event, "completed_at": self._completion_at},
                "source_version": sha256(encoded.encode()).hexdigest(),
                "evidence": document, "target_id": None, "status": "pending",
                "next_attempt": self.now,
            }
            if native_id is not None:
                operation["read_target"] = native_id
            self._save_operation(operation)
            self._notify("intent_persisted", operation)
            target = native_id
        else:
            operation = json.loads(row[0])
            if (operation["system"] != system or operation.get("read_target") != native_id
                    or _json(operation["action"]) != encoded):
                raise RuntimeError(f"Handler replay changed write {retry_id}")
            if operation["status"] == "confirmed":
                return operation["record"]
            target = native_id if native_id is not None else self._api(transport, operation, "find", retry_id)

        if target is None:
            self._notify("before_send", operation)
            try:
                target = self._api(transport, operation, "send", retry_id, deepcopy(document))
            except TimeoutError:
                target = self._api(transport, operation, "find", retry_id)
                if target is None:
                    raise
            operation["target_id"] = target
            self._notify("after_send", operation)

        record = json.loads(_json(self._api(transport, operation, "read", target)))
        if not isinstance(record, dict) or not all(
            key in record and _matches_json(value, record[key]) for key, value in document.items()
        ):
            raise RuntimeError(f"Native readback mismatch for {system}/{target}")
        for key, value in (("native_id", target), ("system", system)):
            if record.setdefault(key, value) != value:
                raise RuntimeError(f"Native readback {key} mismatch for {system}/{target}")
        operation.setdefault("event", {**self.event, "completed_at": self._completion_at})
        operation.update(target_id=target, record=record)
        self._notify("after_readback", operation)
        operation.update(status="confirmed", next_attempt=None)
        self._save_operation(operation)
        self._notify("result_persisted", operation)
        return deepcopy(record)
