# Durable engine runtime

From the release root, import `Engine` from `engine`. For the staged package,
add `engine_build` to the import path from outside the release root.

```python
from time import monotonic

from engine import Engine

run = Engine(run_directory, consumer, seed=41, native=transport,
             providers=providers, monotonic=monotonic)
run.schedule("clock-check", "noop", 540, payload={}, duration=15)
run.run(until=555)
assert run.history()[-1] == {"id": "clock-check", "phase": "complete", "at": 555}
```

Resume against the same directory and native records with the original seed,
configuration and consumer identity. Identity includes the consumer class's
module and qualified name and, when supplied, its `resume_identity` string.
Use that string to version deployments of the same class when handler or
configuration semantics change. Identity is a compatibility check for trusted
callers. Checkpoints created without identity fail closed because their original
consumer and duration semantics cannot be inferred. Keep those runs with their
original engine. Queued work and in-flight completions created by this engine
resume automatically.

- **B — Clock and dispatch.** Integer minutes start at Monday, 2027-01-04 00:00
  UTC. Edges order by minute, lower numeric priority, then insertion sequence.
  The cutoff is inclusive and `run.now` records the latest processed edge.
  Event IDs are nonblank strings unique within a run, including completed
  events. Keep the ID stable when replaying a handler that schedules follow-on
  events. `schedule` returns the dispatch sequence, also available to
  handlers as `run.event["sequence"]`.
- **B/I — Shifts and duration.** `config["shifts"]` maps roles to one or more
  half-open integer windows within a day. Windows can touch, must not overlap
  and repeat daily. Split overnight shifts at midnight. Missing roles have
  continuous availability. All configured calendars are validated at open,
  including roles with no queued events. `run.shift_start(at, role)`
  predicts the start of work at or after `at` without reserving capacity or
  touching the queue. Work assigned to a role consumes only working minutes
  across gaps and days. It can complete exactly at closing. Role-free work
  consumes elapsed minutes. Use `elapsed=True` for unattended work that needs
  a role's shift to start and then continues in elapsed time. For a role with a positive integer `staff` count, duration-bearing tasks
  occupy one position each. Requests are admitted in business-minute order,
  using priority and then queue sequence for simultaneous arrivals. The clock
  saves each reservation across restart. A handler that must promise a start
  before issuing a native order can read `available_start(at, role)` and
  schedule it with `book_staff=True` in that event. Unattended work releases the person
  during its elapsed duration. Calendars repeat daily.
- **D — Evidence.** `run.observe(role, as_of=minute)` includes dated adapter
  rows through that business minute. A handler can observe only as its
  scheduled role and through the current business minute. Use
  `run.event_started_at` for start-time evidence, including when working
  minutes spanned shift gaps. An omitted `roles` field makes a row visible to
  every role. When present, `roles` must be a list of allowed role IDs.
  Historical values require dated versions in the adapter's query surface. Outside a handler, callers are responsible for the role and cutoff.
- **E — Consumer state and decisions.** `consumer.handle(run, kind, payload)`
  runs at completion, except for `noop`. `run.state` holds JSON data with
  string keys. Its edits and follow-on events commit with the completion edge.
  Treat `run.config` as read-only. Invoke providers through
  `run.call_provider(tier, prompt, tools)` so responses and timing are journaled.
  Provider latency contributes zero business minutes.
  Within a handler, call `run.load_consumer_decision(run.event["id"])` before
  invoking a provider. If it returns `None`, use
  `run.save_consumer_decision(event_id, decision)` with a JSON object containing
  its complete `input` before any native write. Always use the returned value.
  The first committed value is immutable and survives a failed or interrupted
  handler. The journal binds the decision to the dispatch sequence and event
  ID. Rebuild any derived cache state from the committed decision during
  replay, since ordinary state edits roll back with the handler.
- **J/K/L — Write and recovery.** `run.write(system, kind, fields)` stamps
  the completion minute, persists a write intent, sends, verifies native
  readback and returns that record. A handler replays the same native writes
  in order, using the same dispatch sequence and write index as retry IDs.
  Confirmed writes are reused. Uncertain sends look up the native record by
  retry ID before resending. Failures leave the completion pending.
  `run.export()` projects confirmed records with their system and native ID.
  The checkpoint, decisions and operations stay in the private run directory.
  The optional `boundary(stage, operation)` hook exposes intent, send,
  readback and result checkpoints to diagnostics and crash tests.

## Extend an existing run

`run.extend(extension_id, *, systems=..., roles=..., products=..., events=...)`
appends named catalog entries and schedules a batch of events in one SQLite
checkpoint update. Omitted catalog objects and the event list default to empty.
`run.catalog` returns a detached dictionary with `systems`, `roles` and
`products` dictionaries keyed by ID. Object-valued base mappings under those
configuration keys are included. The base configuration, including legacy
`config["shifts"]`, stays fixed. The catalog is available to trusted hosts and
handlers and is separate from the business-record export.

- A repeated extension ID with the same JSON content succeeds without adding
  work. Object key order has no effect. Event list order is significant.
  Reusing an ID with different content, replacing a committed name or
  reusing an event ID raises `ValueError`. A failed batch leaves the catalog,
  dispatch sequence and queue at their previous boundary.
- A system entry can declare `adapter: "erpnext-sales"` with an `erp` object
  containing `url`, `site`, `company`, `customer`, `item` and `warehouse`, plus
  `token_env` naming an environment variable. The descriptor contains no
  token value. To use a system at runtime, the trusted host supplies an
  `Engine(..., adapters={adapter_name: factory})` factory accepting
  `(system_id, spec)` and returning a transport. Supply the same routing on
  restart and scope retry IDs to the run and system. Other adapter names
  must be registered at submission. Factories run lazily when a permitted
  operation first uses the system. The engine persists descriptors and
  dispatches through host transports. The host handles credential retrieval,
  target authorization and application provisioning.
- A role entry specifies `shifts` in the existing daily format. Its optional
  `permissions` list defaults to no scopes. New roles need `observe` for
  legacy records or `observe:<system>` for a specific system, plus
  `write:<system>` or `write:<system>:<kind>` for writes. Added systems require
  an explicit scope for every role. Existing row-level observation filters
  still apply. The host must authorize extension producers because extension
  files can grant scopes and queue application commands. Providers and
  application-specific action authority remain part of trusted host and
  consumer policy.
- A product entry has `{"system": system_id, "item": native_item}`. The system
  must exist in the catalog or be added in the same batch. Products are
  catalog references that consumers can resolve to native item IDs.
- An extension event uses the keyword fields of `run.schedule`, including
  `id`, `kind`, `at`, and an object-valued `payload`. Its requested minute
  must be at or after the current boundary, even when a shift would otherwise
  roll it forward. The new shift definitions apply to the added events.
  Queued work keeps its existing edges and dispatch sequence. Roles assigned
  to queued work and systems recorded in prior writes are reserved.

Submit between handler calls on the engine object, or use the offline command
from another process while a controller runs.

```sh
python3 -m engine --run /path/to/run --extend /path/to/extension.json
```

The file contains `id` and optionally `systems`, `roles`, `products` and
`events`. The command requires an existing engine checkpoint. It validates
and commits the batch without launching a controller or modifying the base
manifest. A trusted host can invoke `engine.__main__.main(..., adapters=...)`
to allow additional adapter names.

A command arriving during an edge waits for that edge to finish. It holds the
boundary gate while waiting, so the controller sees accepted additions before
starting another edge, even during a long run. In-range additions can dispatch
in the same run call. When submission lands at the controller's final boundary
or the events fall beyond its cutoff, the work remains durable for the next
run. An event already in progress must return or fail before a waiting command
can commit. An extension from a stale engine object loads the current
checkpoint before validation. Use `catalog` after that synchronization or
after reopening to read external additions. Submit through a separate engine
object or the CLI when another thread owns the controller.

## Integration limits

- The host needs Python with SQLite, POSIX file locking and a local filesystem.
  Run one controller per directory and call it from one thread.
- The transport implements `send(system, retry_id, document) -> native_id`,
  `find(system, retry_id) -> native_id | None`, `read(system, native_id) -> dict`
  and `records() -> iterable[dict]`. Scope retry IDs to the run at the transport
  boundary. Lookup must reliably find committed writes before any resend.
  With late commits or eventually consistent lookup, the transport must also
  enforce idempotency for that retry ID. Keep private fields out of business
  records. Native writes become visible individually. A crash can leave a
  partially visible group until completion is replayed. A provider may be
  called again if the handler stops before the decision is saved.
- This runtime supplies generic timing, decision persistence and native
  recovery. Manufacturing configuration, capacity or material reservation,
  production completion, live adapters and legacy generator integration are
  separate responsibilities.

## Offline checks

Run from the release root after applying the patch.

```sh
python3 -m unittest discover -s tests -p test_engine_extension.py -v
python3 -m unittest discover -s tests -p test_engine_resume_shifts.py -v
python3 -m unittest discover -s tests -p test_engine_goals.py -k Milestone0
python3 -m unittest discover -s tests -p test_engine_goals.py -k Milestone1
PYTHONPATH=. python3 tests/test_engine_resume_shifts.py --benchmark 100 500 1000 2000
```

The existing Milestone 1 shift case fits inside one shift after rolling to its
opening. The dedicated tests cover actual work spanning gaps and days, elapsed
time, persisted start context, decision replay and the native write envelope.

### Checkpoint storage and scaling

The clock stores pending edges, event IDs and ordered history in indexed
SQLite tables. An edge commits its queue changes, history, staff bookings and
consumer state in one transaction. Native operation and decision commits
retain their independent recovery boundaries. The queue index keeps dispatch
work independent of the amount of history already recorded. Reading
`history()` materializes the history through the engine's current boundary.

Opening an identity-bearing legacy checkpoint, including via `--extend`,
imports its queue and history in a one-time transaction. Dispatch sequences,
write and decision journals, and the catalog survive the import. The old
runtime cannot open the upgraded format. The read-only production-profile
lookup can read the catalog in both formats.

The offline benchmark preloads no-op events in one handler and times
dispatch of their start and completion edges. It excludes native operations,
provider calls and consumer-state growth. Run it with:
```sh
TMPDIR=/data/your-scratch PYTHONPATH=. python3 tests/test_engine_resume_shifts.py --benchmark 1000 10000
```

Software registration checks proposed instance names against an index over
native write intents, including pending writes. The first open after upgrade
builds that index from the journal. Subsequent additions look up each proposed
instance by name while holding the extension lock.

Each edge still commits durably using SQLite's configured sync behavior.
The checkpoint still serializes consumer state, staff availability and
catalog metadata. A consumer that grows its JSON state with every event can
therefore still incur quadratic work. The durable event-ID and history tables
grow with use, and materializing history takes time and memory proportional
to its size.

## Inspect a run offline

From the release root, run as the checkpoint owner:

```sh
python3 -m engine --run /path/to/run --viewer /path/to/evidence.html
python3 -m engine --run /path/to/run --metrics
```

Open the HTML locally. Select a native record on the timeline, then
**Decision and evidence**, then **Call attempts**. The journal and viewer
contain private operator evidence, including exact provider input and raw
responses. The checkpoint and newly created HTML are owner-only. The
inspection commands use a read-only SQLite snapshot and need no adapters.
Standard business exports exclude the journal, and hidden simulation truth is
outside the viewer.

- **D–H/J–L.** The integrated engine journals each provider invocation and
  each adapter `send`, `find` and `read`, including failures. Bulk evidence
  reads store only the role- and cutoff-filtered projection. Approval
  evidence fields are amount, quote and open commitments, plus scoped
  observations and applicable policy. Completed provider responses and
  scoped observations replay from SQLite. A provider timeout receives one
  retry on the first attempt. An interrupted intent retains unknown outcome
  and latency. Confirmed operations link to their verified adapter record.
- Metrics count completed attempts, including failures. Interrupted intents
  are counted separately. Wall seconds by day are the sum of measured calls
  by business completion day. Known cost sums and missing-cost counts are
  tracked separately. None of these measurements advances business time.
- Extension and resume migrate the schema. Earlier records remain
  inspectable with whatever evidence exists. Calls that predate the journal
  have no attempt history.
- Coverage is at the integrated engine adapter boundary. The ordinary
  `generate.py incremental` ERP path currently supplies sales operations
  and has no configured provider. Per-request HTTP/SMTP internals and
  the separate legacy daily generator use their own paths. The two-consumer
  provider checks use injected offline adapters.

## Extractor grading

`run.extractor_questions(as_of=minute)` returns only `id` and `question`.
`run.private_truth(as_of=minute)["answers"]` maps those IDs to owner-only
reference values. Omit `as_of` for the latest committed minute. Both read
confirmed native write versions linked to completed events through the
inclusive cutoff. An interrupted handler's confirmed writes enter the
grading snapshot after its completion commits. The default viewer and
business export retain their existing projections.

The consumers join billed quantities and posted payments through their native
links. Amounts represent recorded invoice totals or payment allocations.
They do not derive receipts from changes to outstanding balances. A null
answer marks inadequate or conflicting evidence, including an order with no
linked payment evidence. Values retain decimal precision, with a decimal
string used when a JSON number would lose precision in the engine's Python
representation.

For the recurring Norvale workflow, `native.build_evidence.sales_facts`
projects observable ERPNext Sales Orders, Delivery Notes, Sales Invoices and
Payment Entries for the Norvale consumer's `grade_extraction` method. Use a
snapshot retained at the intended cutoff and load monetary fields with
`json.load(..., parse_float=Decimal)`. The final frozen export supports the
April 30, 2027 cutoff. Earlier cutoffs require retained versions. The projection
covers submitted non-return invoices linked to orders and full, matched
delivery lines. It leaves mixed-currency totals, unallocated cross-order
totals and missing links unresolved. Posted allocations alone establish
neither bank clearance nor physical delivery.

The operating recurring catalog also accepts an internal
`company-sales-readback` event with role `FINANCE`. It journals submitted
Delivery Notes, their Sales Invoices, and confirmed bank allocations from
the ordinary company checkpoint using ERPNext document IDs. The transport
reads those documents back without issuing a new business write. A record
becomes eligible for extraction when the readback event commits. The
Milestone 5/6 fixture drives the ordinary phases and schedules these captures
through the same recurring consumer. The fixture uses isolated transport
substitutes for ERP, MES and QMS.
