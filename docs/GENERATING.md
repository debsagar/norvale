# Generate a new native run

Inspection of the bundled data needs only Python 3. Generation additionally needs a Linux Docker host, Docker Compose, OpenSSL, network access to the model providers, and sufficient memory/disk for ERPNext and its supporting services. Python generation code uses the standard library.

The bootstrap is for an isolated development stack: it inherits fixed `admin` ERP/database bootstrap passwords. Keep services on their Docker network; do not expose this stack to the internet. Calendar and Matrix are provisioning dependencies; the delivered business evidence focuses on ERP, Gitea, and mail.

## Prepare images and the shared clock

From the repository root:

```sh
docker build -t nv11-erp:15.124.1 -f norvale/environment/images/Dockerfile.erp .
docker build -t nv11-calendar:3.9.0 -f norvale/environment/images/Dockerfile.calendar .
mkdir -p env-cache/lib
clock_container=$(docker create nv11-erp:15.124.1)
docker cp "$clock_container:/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1" env-cache/lib/
docker rm "$clock_container"
```

The library must match the host/container architecture. `NORVALE_CLOCK_LIB` can point to another directory containing `libfaketime.so.1`. Remaining service images are pinned in `norvale/environment/docker.py`; Gitea is pinned in `native/provision_tickets_v2.py`.

## Configure model access

Export `OPENROUTER_API_KEY` and `TYPESAFE_API_KEY` in your shell. Optional `NORVALE_MODEL` defaults to `openai/gpt-4.1-mini` for OpenRouter. Alternatively set `OPENAI_API_KEY`, with optional `OPENAI_BASE_URL` and an appropriate `NORVALE_MODEL`. `.env.example` lists variable names; files are not automatically loaded. No API keys are shipped and no machine-local credential file is consulted.

Generation makes paid network calls. There is no built-in spending ceiling. Missing credentials stop judgment explicitly.

## Provision, attach tickets, then execute

Use a new empty run directory; never point this at the published `data/` snapshot or another existing run.

```sh
python3 generate.py --run "$PWD/runs/my-run" --provision --days 120
python3 native/provision_tickets_v2.py "$PWD/runs/my-run"
python3 generate.py --run "$PWD/runs/my-run"
python3 native/verify_run.py "$PWD/runs/my-run"
```

Provisioning initializes services; the subsequent command advances the modeled company. Resume with the same final generation command. The verifier connects to services and writes new exports; it is not an offline reader. All `runs/` contents are ignored by Git, including credentials, private trace/state, raw SQL and mail archives. Keep them private.

A fresh run is not guaranteed to reproduce the delivered trajectory byte-for-byte: real provider judgments and service behavior vary. This release was checked offline and against saved completed-run evidence; its bootstrap was not re-provisioned during packaging.

## Add software, roles and products to a recurring run

Submit from a second shell, including while the ordinary recurring controller is running.

```sh
python3 generate.py --run runs/my-run --extend additions.json
```

The operator-only command uses the existing stack identity and writes an
append-only SQLite catalog and queue under the run's `scheduled-additions/`
directory. Keep it with `config.json`, `state.json` and the environment files
when backing up the run. Existing recurring checkpoints can acquire this
catalog in place. The command commits the entire batch together, leaves the
regular checkpoint and configuration alone, and does not start a controller.
Replaying the same batch ID with the same contents is safe. Use a new ID for a
new batch. Existing catalog names and queued event IDs are reserved.

For example, with `east-sales` in the trusted `NORVALE_CONNECTIONS_FILE` registry:

```json
{
  "id": "open-east-sales",
  "systems": {"east": {"connection": "east-sales"}},
  "roles": {"east-rep": {"shifts": [[780, 1020]], "permissions": ["write:east:order"]}},
  "products": {"east-lotion": {"system": "east", "item": "LOTION-2"}},
  "events": [
    {"id": "east-first-order", "kind": "order", "at": 750, "role": "east-rep",
     "duration": 15, "payload": {"product": "east-lotion", "case": "PO-EAST-1",
                                "quantity": 24, "rate": 2.45}}
  ]
}
```

Registry entries use `{"adapter":"erpnext-sales","token_env":"VARIABLE","erp":{...}}`
with the ERP target fields shown in the incremental example below. The target
must already have the product, customer, warehouse, unique operation field,
and an ERPNext account allowed to create draft Sales Orders. The default account
is `sales@norvale.test` and accepts new events only for the `sales` role. Existing
queued events and exact replays retain their original authorization. To bind a sales role to its own native account, add
`"employee":{"role":"east-rep","user":"east-rep@norvale.test"}` to the host
registry entry. Set its token variable to that employee’s credential. Only the
commissioned role can place orders through that connection. Readback verifies
the authenticated user and the native order owner. Both the submission and running controller
need the host profile and token variable. The target profile is pinned on
acceptance. The adapter verifies the token's ERP identity when it first acts.
Trust only operators to submit batches because they can grant role scopes.

Business minutes count from January 1, 2027 at 00:00 UTC. An older
checkpoint begins extension scheduling at noon on its recorded day, and a
new run starts accepting requests at day-zero noon. The example starts at
13:00 and completes at 13:15 after its daily shift opens. The controller drains
eligible actions between regular phases at the daily noon boundary and
through the last evening of the run. Submission never interrupts a phase.
A connection failure or retryable HTTP error on an added sales site defers
that order's completion to the same minute of the next business day. Other
eligible events continue. The retry uses the original write identity, employee
scope, and order dates. Other adapter errors still surface to the operator.
An action arriving at the final boundary can require a resume. Actions with
completion or retry minutes beyond the active horizon wait for a horizon
extension within the generator's 365-day limit.

**B, I, J, K and L.** Scheduled actions use the scoped role and pinned adapter
to create and verify draft ERPNext Sales Orders. The order carries the
catalogued native item, business date, role and case reference. The shared
engine persists write intent and reconciles retries using the native
operation marker. Added roles are scheduling and permission entries.
Added products reference existing application masters. The recurring
demand sampler, application provisioning, and submitted order-to-cash
workflow continue through their existing paths.

## Add a production family and its first order

Use an initialized recurring run with
`company_authority` set to `integrations.company_run.authority:Authority`.
Production add-ons use the same `--extend` command. Each profile represents one
ERP item. Its system ID is `production-<item>`. For example:

```json
{
  "id": "add-lotion-2",
  "systems": {"production-LOTION-2": {"connection": "approved-lotion"}},
  "roles": {"lotion-sales": {"shifts": [[540, 720]], "permissions": ["write:production-LOTION-2:order"]}},
  "products": {"lotion-2": {"system": "production-LOTION-2", "item": "LOTION-2"}},
  "events": [
    {"id": "lotion-first-order", "kind": "order", "at": 2040, "role": "lotion-sales",
     "payload": {"product": "lotion-2", "case": "PO-LOTION-2", "quantity": 24, "rate": 2.45}}
  ]
}
```

In the operator-held connection registry, each production entry contains:

| Field | Value |
| --- | --- |
| `adapter`, `item`, `customer`, `order_roles` | `company-production`, the native batch-tracked item code, the installed ERP customer name, and the operator-approved scheduling role names |
| `source`, `family`, `mapping` | Stable PLM deployment ID, native base part ID, and the family receiver mapping shown in [the revision bridge example](../integrations/revisions/config.example.json) |
| `hold_location_id`, `units` | Native MES hold location ID and map from MES UOM names to ERP stock UOMs |
| `plm` | `url`, `publisher`, `token_env` |
| `erp` | `base`, `site` matching the recurring ERP in `env/endpoints.json`, and `token_env` for native master readback |
| `mes` | `url`, `db`, `login`, `source_id`, `password_env` |
| `qms_reader`, `qms_approver` | Reader settings `url`, `site`, `api_key_env`, `api_secret_env`, and the authorized native approver username. Optional `qms_source` identifies the QMS deployment |
| `specifications` | ERP item code to inspection method version for the output, and to inspection specification for any inbound material handled by this QMS |

Use the same source ID and native target for families sharing a deployment.
Install and approve the PLM revision and both receiver BOMs before extension.
Registration verifies those existing records and the ERP item/customer and
MES hold location. It stores the named connection and a profile fingerprint
with the catalog and events. It reads credential values from the named
environment variables. Both the submitting and running environments need
access to the operator registry and variables.

**A, B, C, D, I, J, K and L.** The scheduled role must have both a catalog
scope and profile approval. The recurring Sales User submits the native order
and verifies it before it enters `ops.orders`. The next regular production
planning pass uses the acknowledged BOM, released ERP inputs, native work
center availability and normal capacity checks. Choose an event completion
before a remaining planning pass. MES dispatch and completion carry the
source instance, and finished-lot QMS readback uses the inspection specification
pinned to the approved revision. Materials without a recurring supplier policy
require operator supply.

### Maintain separately registered MES sites

**C, D, I and J.** The ordinary daily service phase checks native faults on
acknowledged routes and meters posted production by MES source and work center.
A center reaching `maintenance_units` gets a durable case and a native MES
fault. The maintenance workflow creates the Gitea issue in the registered site
repository, performs staffed diagnosis and repair, records an independent
functional test, and verifies restart before clearing the case. Failed tests
require corrective service and a new test. Native fault and issue IDs are local
to their source, so two sites can use the same numeric IDs. Existing primary
MES maintenance continues to use its original configuration and checkpoints.

For a separate MES deployment, optionally include this `maintenance` object in
its operator-held production profile at registration. Use the same object
across families sharing the deployment.

```json
{
  "login": "maintenance",
  "password_env": "EAST_MES_MAINTENANCE_PASSWORD",
  "ticket_url": "https://east-tickets.example",
  "ticket_repo": "plant/maintenance",
  "ticket_operator_env": "EAST_MAINTENANCE_ISSUES_TOKEN",
  "technician_env": "EAST_TECHNICIAN_TOKEN",
  "tester_env": "EAST_TESTER_TOKEN",
  "verifier_env": "EAST_VERIFIER_TOKEN",
  "reader_env": "EAST_MAINTENANCE_READER_TOKEN"
}
```

Provision the MES maintenance login on the profile's MES URL and database
with authority for the native fault, intervention, restart, and cancellation
operations. The profile's existing MES login supplies operational readback.
Provision a Gitea issue operator, the distinct
`norvale-maintenance-technician`, `norvale-maintenance-tester` and
`norvale-maintenance-verifier` identities, and a read-only identity scoped to
the repository. Supply distinct tokens to the recurring process. The profile
fingerprint includes the maintenance scope and variable names. Credential
values can be supplied or rotated through those variables when the process
resumes. Profiles registered without this object retain their original
fingerprint. Editing a committed profile's maintenance scope changes its
fingerprint and the controller rejects the edited profile.

Missing or denied maintenance authority leaves the affected case pending.
At the usage limit the controller blocks work on that center even if it
cannot yet write the native fault. Other centers continue their recurring
work. The service adopts open native faults on the registered equipment into
cases. Automatic staffed repair applies to its own usage-triggered faults.
Externally reported faults require operator handling. The add-on workflow
does not automatically clear their cases.

Interrupted work stays outside ERP finished stock. The existing disposition
gate requires an authorized rejection and linked case in the interrupted item's
registered QMS, followed by readback of cancellation on the same MES.
Equipment verification and interrupted-work disposition are independent.

### Archive newly connected production systems

Run the existing lake collector with the recurring run directory. The full-company
acceptance script supplies this argument automatically.

```bash
python3 -m integrations.company_run.lake "$runtime/analytics.json" "$runtime/lake" --run "$runtime/business" --interval 60
```

The collector reloads the committed catalog each pass. Give its process access
to the same host registry and credential environment as the recurring controller.
It reads completed MES work for the registered company and product, approved
publications for its PLM family, and the QMS records visible to the configured
reader. The archive records source identity alongside each native ID. Multiple
families sharing a QMS endpoint and site share its identity. A snapshot failure
is recorded for that source and the collector continues reading its peers.

## Run an incremental event plan

`generate.py incremental` runs the integrated minute-based engine against a
separate checkpoint. Use a private, isolated ERPNext installation with the
Norvale master data, the existing unique `Sales Order.custom_norvale_operation`
field, and a token for the seeded `sales@norvale.test` Sales User. This command
does not provision services. Set `NORVALE_INCREMENTAL_SALES_TOKEN` in your
environment. Keep credentials out of the plan.

For example, save this as a private `incremental-orders.json`. Replace the
origin and site with your isolated ERPNext connection and confirm the master
record names there.

```json
{
  "consumer": "norvale",
  "seed": 41,
  "config": {},
  "erp": {
    "url": "http://127.0.0.1:8080",
    "site": "erp.norvale.test",
    "company": "Norvale Consumer Care BV",
    "customer": "Corner Stores Group",
    "item": "SKU-30004",
    "warehouse": "Finished Goods - NCC"
  },
  "events": [
    {
      "id": "order-rotterdam-812",
      "kind": "order",
      "role": "sales",
      "at": 540,
      "duration": 15,
      "payload": {"case": "PO-ROT-812", "quantity": 240, "rate": 2.45}
    }
  ]
}
```

```sh
# Create with a path that does not exist. The order starts at minute 540.
python3 generate.py incremental --run runs/orders-jan04 --plan incremental-orders.json --until 540

# Resume the in-flight order at its completion minute without adding events.
python3 generate.py incremental --run runs/orders-jan04 --until 555
```

Minutes start at Monday, January 4, 2027 00:00 UTC. The cutoff is inclusive.
The plan, target and run identity are stored with the checkpoint. Keep the
directory private and use one controller per run. Resume uses the original
plan and the same service records. The command prints confirmed records as
JSON, including ERP document IDs and URLs. It uses no model providers.

- **B, I, J, K and L.** The queue dispatches the order at completion. The Sales
  User's token authorizes a single-item draft Sales Order. The customer PO
  field carries the case reference. The order carries the configured customer,
  item and warehouse, the simulated transaction date and delivery seven days
  later. A separate ERP GET verifies the draft, owner, references, dates,
  quantity and rate. A failed write or readback leaves the event pending.
- **D and K.** Adapter observations query only this run's draft Sales Orders
  with the same Sales User credential. The engine filters projections to
  `sales` and the requested business minute. Queries use current document
  values and provide no point-in-time history after manual edits. Retry markers
  stay out of the projection. A run namespace and deterministic write identity
  scope lookup, and the ERP field's uniqueness protects identical late retries.
  Preserve the marker, source records and uniqueness constraint when resuming.
- **Scope.** The native command accepts Norvale `order` events with empty
  consumer config. Submission, production, fulfillment, managed-services
  actions, Gitea, mail and judgment are outside this mapping. The native
  command rejects unsupported plans. The existing `--run` legacy command is
  still the full-company generator.

Run the CLI integration checks offline from the release root.

```sh
python3 -m unittest discover -s tests -p test_incremental_cli.py -v
```

The Norvale tests exercise the existing Frappe client through an in-process
HTTP response fixture backed by a temporary store. The managed-services test
resumes a contract → issue → response plan through a separate injected
transport double and checks the record links. Neither test starts services
or verifies a live ERPNext, Gitea or mail deployment.

## Rebuild inspection artifacts

The HTML record viewer uses only the standard library:

```sh
python3 scripts/build_viewer.py
```

To rebuild the executed notebook over bundled data:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python native/build_behavior_notebook.py --out notebooks
```

Open notebooks from `notebooks/`; their data path is `../data/observable`. Run All only reads files. To inspect a new run, pass `--run runs/my-run/export`; the generated notebook uses a relative path to that export's `observable/` directory. `native/build_evidence.py` separately builds development reference questions from private trace/state of a new run; private traces are not included in this release.
