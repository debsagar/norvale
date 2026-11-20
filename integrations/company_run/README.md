# Isolated recurring acceptance run

Run from a checkout containing this directory. The preparation command copies the integrated release and its adapters into a new private directory. The ERP planner reads the native BOM identified by the company authority before allocating inputs. A run uses its own Compose projects and databases. The company clock, recurring phases, document writer, ERP posting, and execution lock come from the recurring engine. The primary revision bridge uses the authenticated planner to publish and verify ERP BOMs. It resolves the current planner credential from the run on each connection, including when reopening older runs.

```bash
export NORVALE_CLOCK_LIB=/absolute/path/to/libfaketime
python3 -m pip install -r integrations/lake/requirements.txt
demo=/data/company-accept-$(date +%s)
bash integrations/company_run/prepare.sh "$demo"
bash "$demo/release/integrations/company_run/provision.sh" "$demo" >"$demo/provision.log" 2>&1
cd "$demo/release"
bash integrations/company_run/accept.sh "$demo" >"$demo/accept.log" 2>&1
```

Use Python with PyArrow and the pinned Delta Lake requirements. Provisioning uses locally cached images, Docker Compose, the release fake-clock library and the integrated product adapters. Keep runtime credentials and business checkpoints in the private demo directory.

The acceptance entry point uses synthetic measurement inputs, an engineering review and product approval, and deterministic purchasing and finance decisions. Each role writes through the application client. The test process exits after an initial QMS release, then a second process resumes the same engine state. The first eligible outbound draft triggers a QMS revocation before the normal ERP submission guard. The independent lake process scans once per minute and the final scan checks for duplicate snapshots. inspect the source watermarks in lake/state.json, engine phases in business/state.json, native operations in business/trace.jsonl, and final assertions in acceptance.json. The exit status reports acceptance only after all required assertions pass.

For a prepared, isolated **ordinary operating run**, set `run` to the runtime directory containing `business/config.json` and execute the following from the release root. For the combined 14-day qualification, configure the horizon and commission a bounded production target before dispatch (see “Bounded production” below). Use demand larger than that target and authorize backorder continuation within the horizon. The verifier checks that a native partial order actually continued to completion.

On the first invocation the injector stops the generator after observing a full native QMS release and before its ERP transfer. A new process resumes the same checkpoint. This sequence stops on an unexpected error and preserves the first process log. Repeating it on a run with a saved interruption goes directly to recovery and verification.

```bash
(
  set -eu
  umask 077
  export NORVALE_OFFLINE=1 PYTHONPATH=".:native:integrations/lake:${PYTHONPATH:-}"
  business="${run:?set run to the prepared runtime}/business"
  test -f "$business/config.json"
  if [[ ! -f "$business/ordinary-release-interruption.json" ]]; then
    first_log=$(mktemp "$run/release-interruption.XXXXXX.log")
    if python3 -m integrations.company_run.release_recovery "$business" >"$first_log" 2>&1; then
      echo "Expected the first process to stop at the release. See $first_log" >&2
      exit 1
    fi
    grep -F 'InterruptedError: Qualification interrupted after QMS release and before ERP stock transfer:' "$first_log"
    test -s "$business/ordinary-release-interruption.json"
  fi
  python3 generate.py --run "$business"
  python3 -m integrations.company_run.verify "$run"
)
```

The verifier checks the recorded interruption, the original disposition, every submitted ERP transfer of that lot out of quarantine, and the active ledger entries and batch bundles on both sides. It records the recovered Stock Entry in `acceptance.json`. Other acceptance checks still require their own native evidence.


Settlement acceptance uses Finance invoice, payment, GL and bank records alongside
the independent ERP archive. Each invoice row must identify its exact submitted
delivery or receipt row. `acceptance.json` lists verified cash allocations,
installments, outstanding balances and bank evidence. Ordinary acceptance
requires one fully cleared customer bill and one fully cleared supplier bill.
Open balances can remain, and each verified payment in the ordinary run needs
a matching bank reconciliation. Legacy scripted runs with older archive settings
use scoped native reads for the evidence their archive did not collect. Their
cash postings can satisfy settlement without a bank clearance claim.


```bash
export PYTHONPATH=".:native:integrations/lake"
python3 -m unittest integrations.company_run.test_company_run
python3 -m integrations.company_run "$demo/business" --through 2
python3 -m integrations.company_run.lake "$demo/analytics.json" "$demo/lake"
python3 -m integrations.company_run.verify "$demo"
```

A–L responsibilities include native product and identity setup in A, the original clock and partial fill in B/C, observed measurements and cases in D/E, local acceptance inputs in F–H, revision, lot, and dispatch checks in I, and native writes, readback, independent history, and resume in J–L. Keep the inherited model path for ordinary engine runs.

When run with `--run`, the collector reloads the company’s PLM families on each
pass. It reads the native revisions of those families, including revisions
created since startup. The primary PLM reader and publisher use the same
configured source name and application URL. Standalone BOM scans use the
family IDs in the analytics settings’ `plm.parts` list.

The final acceptance scan includes a publisher-authorized export of the
approved PLM definitions into the private archive. The ordinary PLM reader
can see locked parts and component rows. InvenTree 1.3.6 requires change
permission to read approval metadata. The publisher export uses only reads
and projects business fields into the archive. Expose this archive only to an
audience authorized for the approval data. The shipment check independently
follows native lot bundles and cited record versions for every submitted
delivery.

The lake collector accepts an explicit private Compose origin for the operational
ERP, which the company Stack reaches over its dedicated Docker network.
The scanner otherwise requires HTTPS or loopback HTTP. Stored source
settings identify the ERP instance; an HTTP redirect aborts the scan.

## Production and laboratory timing

Fresh full-company runs use the recurring controller's durable
`business/scheduled-additions/engine.sqlite3` queue. Day zero is January 1,
2027. Daily business phases begin at 08:00 UTC. The final phase drains due
work through 23:59, so `--through N` includes that day's timed work. Later
edges stay queued for resume.

The default `company_timing` config sets PRODUCTION to 08:00–16:00 and LAB
to 09:00–17:00, with daily half-open windows expressed in minutes since
midnight. Each center has one running operation. Different centers can
run concurrently. The qualification run still executes half of each MES
order, leaving a native backorder. Its staffed duration is the rounded-up
product of actual output quantity and `shift_minutes / capacity_units`.
Staffed duration skips breaks and overnight gaps. The lab starts samples
on shift, then the default 90-minute unattended turnaround runs
continuously. The result is recorded at the first LAB minute at or after
turnaround. The existing QA credential still controls disposition.

The trace records the full event ID, business minute and start minute.
Those minutes govern work and survive retries. Native server timestamps
remain timestamps of the API operations. Staged checkpoints whose config
predates `company_timing` can resume with their original daily behavior.
Use fresh directories for timed runs.

Run the entry-point re-open test without services or paid calls.

```bash
PYTHONPATH=.:native:tests python3 -m unittest integrations.company_run.test_timing
```

## Bulk manufacture and packing

Register the bulk and finished products in the run's `products` settings
using the existing revision bridge mapping or registered production extension.
Author a formulation-only PLM draft with `material_kind="intermediate"` and
publish it through the existing approval and revision workflow. Mark the bulk profile
`"material_kind": "intermediate"` and set its positive numeric `stock_step`
(for example, `0.01` kg). Use a native, batch-tracked ERP Item with a decimal
stock UOM and a lot-tracked Odoo product at compatible precision. Register
a QMS method and an approved routed recipe for each product. In the packing
recipe, refer to that intermediate and to the individual packaging materials.
The engine detects the stage links through these registered products and
reads their approved ERP BOMs. For example, an approved bulk BOM producing
0.75 kg from 1 kg of inputs has a 75% yield. A packing BOM using 0.25 kg and
one bottle per unit creates demand for 0.75 kg and three bottles for three units.
Use separate native work centers for mass-based and unit-based capacity.

The recurring planner (B/C) nets open packing requirements against released
bulk, outstanding manufacture, and posted lots awaiting first disposition.
It creates internal bulk Work Orders and reports purchased component shortages.
Within I–K, bulk manufacture posts into ERP quarantine by batch, QMS releases
the pinned inspection scope, and the Odoo planner transfers the original
output lot from hold before packing dispatch. Packing consumes that lot and
the allocated packaging lots. The ERP manufacture entry records the original
bulk batch, Odoo lot identity, and consuming move identity. Replay (L) reconciles
the existing Work Orders, completions and transfer. Bulk requires full-lot QMS
release in this adapter. The flow supports one intermediate stage and uses
existing single-stage profiles and work checkpoints as-is.

The default acceptance fixture retains its single-stage recipes. The staged
engine test supplies both profiles through the standard authority and substitutes
transport responses for ERP, QMS, PLM and MES. The private Odoo fixture can
independently exercise the physical transfer and two native MOs.

```bash
PYTHONPATH=.:native python3 -m unittest discover -s tests -p 'test_company*.py'
# Against a fresh isolated integrations/mes fixture with MES_TEST_* set.
python3 -m integrations.mes.test_bulk_live /path/to/fixture.json
```

## Ordinary company run

Set `NORVALE_COMPANY_OPERATING=1` for the fresh isolated provisioning command
above. After provisioning, use the ordinary generator from the prepared release.

```bash
NORVALE_OFFLINE=1 python3 generate.py --run "$demo/business"
```

The operating mode persists in the run configuration. When commissioning the
sources separately, `generate.py --provision --company-operating` selects the
same controller mode for a fresh business stack. Attach the native sources
before running it. The company source configuration provisions private tickets
and the staffed maintenance identities. To opt in an existing dedicated native
company run, use the same private business directory.

```bash
PYTHONPATH=.:native python3 -m integrations.company_run.provision_maintenance "$demo/business"
```

Keep `NORVALE_OFFLINE=1` on each invocation to use local purchasing and finance
rules and defer cases outside their scope.
The qualification entry retains its scripted fault inputs and requires its own
run directory.

The recurring controller schedules MES work on the production shift. The lab
samples actual posted lots on its own shift and records readings after
turnaround. A separate QA review checks the native measurement and investigation
against the rework work instruction. Approved rework consumes the original held
lot, posts a new held lot and records any scrap. The lab measures the new lot
before QA considers release. Cases outside the instruction remain held.

Ordinary acceptance reconciles submitted Repack entries directly from the archive,
including both batch bundles and their active stock ledger movements, the pinned
MES execution and scrap, and the source-qualified QMS authorization and measured
retest. It does not require a delivery. A matching active release is required for
the ordinary rework acceptance check; a generic retest alone cannot satisfy it.
The reconciliation reports pending authorization or unmeasured output, a prior
release subsequently revoked, and broken evidence separately. New Repack remarks
include the QMS source; older submitted receipts can be resolved only when the
archived authority uniquely identifies one source. Drafts must retain current
provenance. Legacy qualification keeps its generic retest check. Archive evidence
is limited to what was scanned and does not establish unobserved native state.

New operating runs assume one lab analyst and one QA reviewer per shift.
Sampling takes 15 staffed minutes, recording takes 10, and review takes 15.
The 90-minute assay turnaround can overlap other samples. These assumptions
are in `company_timing`. A review due 24 simulated hours after the batch
enters the lab queue creates a planner ToDo if still outstanding at the
deadline. The task cites the native lot posting so the planner can check
affected commitments. These timing changes apply to newly provisioned runs.

### Planner backlog

On the existing management-report cadence, the generator publishes a private
**Production backlog YYYY-MM-DD** Note in ERPNext under the planner account.
Open Notes with that account. The report gives the business cutoff, the source
instance and the last successful Work Order scan in wall-clock UTC. It counts
active, submitted orders planned to start by the cutoff and shows remaining
production by item and stock unit. Each contributing Work Order is listed by
its native ID and source version.

For a new isolated operating run, configuration creates the binding automatically.
Ordinary generation uses the configured analytical credentials to scan immediately
before daily management and after the final scheduled work of the day. Install
the pinned Lake dependencies in the generator environment. The run checkpoint's
`lake_scans` records the attempted business minute, source errors and each
source's last successful business minute. The private archive records per-entity
wall-clock scan times and native versions. A failed source can recover on the
next scan or when the completed horizon resumes. Source versions overwritten
between scans may remain unavailable. Scans take wall-clock time at the current
business minute. Additional independent collectors may still run separately.

Fresh company provisioning creates a native Odoo `analytics` account with a
separate `mes-analytics-password`. The collector checks the Odoo database
identity, reader group, and commissioned company before reading completion,
labor, or input staging records. Run the collector with `--run "$demo/business"`
to check the private company binding as well. Existing primary settings and
registered profiles without reader commissioning retain compatibility with
their original credentials. Registered scopes on a commissioned primary site
use that site's dedicated reader.

To opt in a separate registered MES site, load the current MES addon and
supply `MES_ANALYTICS_PASSWORD` when loading its fresh Odoo fixture. In that
site's private production profile, set `mes_reader` to an object with
`login: "analytics"`, a dedicated `password_env`, and `instance` set to
`analytics_instance` from its fixture output. Keep the password in that
environment variable. The existing `mes` connection supplies the endpoint,
database, and source ID. The registered product's mapped Odoo company must
match the reader's company. Commission the profile with those fields present
so they are included in its catalog fingerprint.

To opt in an existing checkpoint, create `business/env/planning-report.json`
with keys `settings` and `archive` set to those absolute paths. This takes
effect at the next scheduled scan. Use the site's Work Order
reader in the Lake settings. Its ERP endpoint and site must match the planner
session, and the Work Order scan must
finish successfully within five minutes. If the scan fails, ages out, or
differs from the planner's current view, the Note shows **STALE** and the last
successful scan time, with figures withheld. Notes are dated snapshots. ERPNext
Work Orders remain the operational record. The archive stays operator-only.

The acceptance verifier reads each dispatched Work Order's pinned release
date and definition, checks PLM approval and both receiver acknowledgements,
reads both installed BOMs, and checks the native MES order's work reference
and execution revision. It compares all archived MES completions for the
submitted ERP work orders with the submitted ERP manufacture postings and
reports missing, duplicate and mismatched postings in
`native_completion_reconciliation` in the acceptance output.

### MES outages in ordinary operating runs

Start and finish can reconcile once immediately after a lost transport reply.
If reconciliation is still unavailable, the durable queue retries on the
following business date at the next eligible production minute. The
controller can continue processing other centers, sites and lab events.

A start failure before the execution RPC creates no center reservation.
Before that RPC, the controller saves the original start event and intended
minute, dispatch, native work order and input lot IDs. An ambiguous reply
keeps that center assigned to the original job across restarts. Recovery
rechecks maintenance, material authorization and the native reservation.
Staffed work starts from the confirmed start on the business clock, so
unconfirmed shifts between the intended start and recovery contribute zero
minutes.

At the original finish edge, the controller saves the completion event,
worked shift intervals and minute total before requesting native completion.
It also pins consumption and the native output lot before the completion RPC.
Pending finish retries occupy the center and add zero staffed minutes.
MES verifies the original operator's timer, posted output and consumption
before ERP can collect the result. ERP's existing posting reconciliation
handles replay. The intervals model labor between the confirmed start and
the original finish edge. A failed contact at finish cannot establish when
connectivity first went down or how much physical work occurred. Persistently
unverifiable work remains pending at cutoff and can resume if the run's
horizon is extended.

## Operating a hot-added MES

For an approved `company-production` host profile in an ordinary
`--company-operating` run, include `"mes_operator": {"login": "operator",
"password_env": "EAST_MES_OPERATOR_PASSWORD"}` before registering it.
The operator uses the profile's MES URL, database and source ID. Commission it
with manufacturing execution rights in the company on the installed BoM. The
existing site planner credential still handles dispatch, lot creation and raw
stock receipts. The approved item and component mappings supply the native
product, stock UOM and company. The dispatch supplies the native source stock
location and hold destination. Profiles that omit `mes_operator` continue to
accept external execution and completion readback.

The ordinary production shift starts and completes the authorized quantity.
MES retains native backorder and movement history, and the existing ERP
posting path reads native completions into quarantine. Production resume
reuses the original operation and labor reservation. Keep the same named
operator on the site through completion and replay.

Commission the QMS workflow separately. Before dispatching manufacture,
released input lots need current, synchronized QMS dispositions. After
manufacture, the site's lab needs the ERP Manufacture entry and row, batch,
item, quantity, stock UOM, pinned inspection specification and source-qualified
MES completion and lot references. Authorized QA must record the disposition
in the profile's QMS site before ERP can release the lot from quarantine.
For customer shipment, supply the site's `qms_dispatcher` credential for
the same QMS endpoint. The primary lab does not schedule samples for these
registered works.

## Definition changes for hot-added production

To use the ordinary definition-change commands on an already registered
family, add a private `RUN/env/definition-changes.json` binding keyed by ERP
item. Copy the `source` and integer `family` from its registered host
profile. This activates managed changes without editing that profile.

```json
{
  "NV-EAST-BULK": {
    "source": "east-plm",
    "family": 12,
    "engineering": {"decision_actor": "east-engineer", "token_env": "EAST_PLM_ENGINEER_TOKEN"},
    "approver": {"decision_actor": "east-approver", "token_env": "EAST_PLM_APPROVER_TOKEN"}
  }
}
```

The PLM source must already have the native change guard enabled and the
publisher, engineering and approver accounts provisioned with their scoped
groups. Supply each decision token through its configured environment
variable. Proposals use the registered publisher and the company ERP
planner's submitted BOM readback. A formulation-only intermediate stays an
intermediate when its component quantities change.

```sh
python3 -m integrations.company_run.definition_change RUN propose NV-EAST-BULK B BOM-ID
python3 -m integrations.company_run.definition_change RUN review 42 REVIEW-REF --family NV-EAST-BULK
python3 -m integrations.company_run.definition_change RUN approve 42 DECISION-REF --family NV-EAST-BULK --effective 2027-02-01
```

Use `--family ITEM` on registered-family decisions (including `reject`).
Optionally add `--source east-plm` to verify the configured PLM source.
Decisions without a family selector address the original primary PLM and
use `PLM_DECISION_TOKEN`. Part IDs are local to that selected PLM.

The ordinary production phase reconciles guarded, approved proposals before
planning, using each family's registered bridge mapping and receiver
credentials. An approval can be delivered before its effective date.
Effective work requires both dated receiver acknowledgments. Existing work
retains its original PLM and receiver identities. Families without a binding
continue to accept externally managed definitions and acknowledgments.

## Bounded production for registered MES operators

A registered production profile with a commissioned MES operator can set
`"production_batch": {"target": 54, "backorder_not_before": 4}`.
For a run provisioned with `generate.py --provision --company-operating`,
the ordinary `generate.py --run` entry point reads this setting. The target
is in the product's ERP stock UOM. Each staffed operation is
capped by the target, the native MO remainder and the configured shift
capacity. Commission a target compatible with the output increment and
whole-number components. The existing timed production path executes the
full quantity for profiles without this setting.

The example authorizes continuation starting on simulation day 4 (January 5,
2027). Omit `backorder_not_before` to leave a partial backorder open. A
continuation can be scheduled on a later production day after ERP has posted
the earlier operations and native readback identifies one open remainder in
the original MO's procurement group. The submitted ERP Work Order and any
linked sales line must still cover the remaining work. Each operation keeps
its own source-qualified MO, staffed intervals and native operator readback.
The continuation reserves the original input lots less their posted
consumption. Its output goes through the existing ERP hold and QMS path.

For a commissioned primary ordinary site, the same policy can be placed in
`business/env/company-sources.json` as `production_batch` before starting a
new run. An in-place extension may add it before dispatching new work; work
with an existing scheduled start or execution journal retains its original
shape. Existing bounded operations keep their saved target if the policy is
later changed or removed. Do not change the primary MES source, operator,
stock location, or existing native records to enable the policy.

Fresh ordinary runs sample purchased lots using the supplier and receipt week.
The private process uses the weekly transition and observation probabilities in
STOCKTAKE's supplier-quality module. Norvale maps its observation bands onto
its own incoming-quality index, with centers 5.45, 5.95 and 6.25 and bounded
lot and measurement variation. Those index values are simulation assumptions.
Analysts record the measured index in QMS. A failed incoming lot follows
nonconformance and QA review. The receipt itself can still be invoiced.
The saved `supplier_quality` setting preserves the earlier sampling behavior
when resuming a run created before this change.

### Primary QMS sample references

Fresh ordinary company setup provisions a dedicated QMS sample writer, the incoming assay parameter, and a native QMS site marker. The writer can create stock-item and batch masters and a single-line draft receipt in the QMS quarantine warehouse. The permissions exclude submit, update, cancel, and delete. The operating clock reads the submitted ERP source row through the Buyer or Warehouse account and the ERP site marker through the Planner account. It reconciles the draft by a unique native batch key and reads it back as the commissioned analyst. LAB and QA recheck the source, site, owner, quantity, UOM, and native child row before using the inspection. Physical rework continues to use the retest bridge. Scripted qualification continues to use its fixture path.

For an older **private ordinary run**, opt in to commissioning new references. Stop its ordinary clock and run these commands from the updated release against that run's isolated runtime.

    bash integrations/qms/provision.sh sample-writer-only /absolute/runtime/qms
    PYTHONPATH=.:native:integrations/lake python3 -m integrations.company_run.configure /absolute/runtime --scoped-receipts --sample-writer

The first command commissions the isolated QMS site and writes private sample-writer credentials in qms/sample-writer.json. The second binds the credentials to the operating ERP and refreshes the analytics settings. Without the binding, lots that lack a previously commissioned reference stay pending. Legacy drafts retain their original owner and mapping. They contain the ERP document and batch in remarks and lack historical ERP instance and row markers. Each use rechecks the submitted ERP row and the independently read QMS draft. Changes of ERP instance, QMS instance, or commissioned identities require separate reconciliation.

The scheduled QMS scan follows the native child-row link from submitted inspections, including rejected lots, to writer-owned drafts. It archives exact ERP source and row keys, lot scope, and the QMS draft and child IDs. The projection excludes connection settings, instance markers, writer credentials, and raw draft remarks. The archive resolves the ERP edge when the scoped native ERP row is present. This works without a release transfer.
