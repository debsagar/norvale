# Recurring QMS for registered production

On a `--company-operating` run, the recurring inspections phase can queue a posted
finished or intermediate lot for a hot-added family. Set `recurring_lab` on that
family's entry in the operator-controlled `NORVALE_CONNECTIONS_FILE` before
registration. The operator's profile fingerprint covers the lab scope. The
extension command still references a named connection and contains no credentials
or assay overrides. The first regular inspections phase that sees the posted lot
can queue it. LAB sampling, elapsed incubation, LAB result entry and QA review use
the existing company clock, staff and shifts.

A registered profile without `recurring_lab` retains the external QMS workflow.
For an opted-in profile, operators can still record inspections manually. The
operating lab leaves a separate current inspection pending for reconciliation.
The primary site's configured inbound specifications take precedence. An add-on
can also opt in mapped input items with explicit assays. When multiple profiles
cover an inbound item, their QMS scope and assay must agree for automatic routing.

## Host registry and commissioned site

The following `recurring_lab` object illustrates the production profile settings.

```json
{
  "model": "norvale-quality-index-v1",
  "analyst": {"api_key_env": "EAST_LAB_KEY", "api_secret_env": "EAST_LAB_SECRET"},
  "approver": {"api_key_env": "EAST_QA_KEY", "api_secret_env": "EAST_QA_SECRET"},
  "reference_writer": {"api_key_env": "EAST_REF_KEY", "api_secret_env": "EAST_REF_SECRET"},
  "analyst_user": "east-lab@example.test",
  "reference_user": "east-references@example.test",
  "company": "East Lab",
  "warehouse": "QMS Quarantine - EL",
  "procedure": "East Lot Investigation",
  "assays": {
    "EAST-ITEM": {"method": "EAST-QC-1", "parameter": "East Quality Index", "min": 5, "max": 6}
  }
}
```

The product requires an assay. Each method must match the item's entry in
`specifications`. Manufactured lots must carry the matching inspection
specification from their pinned PLM definition and a confirmed, source-matched
MES completion posted into ERP. The registry owns the ordered finite numeric
limits and the parameter name. The supported synthetic model produces a single
quality-index reading to two decimal places from the run seed and confirmed
material genealogy. The model has no general unit conversion, multivariate
assay, formula, or instrument input. Configure limits in the model's scale.

Use the existing QMS schema and workflow. Commission the site, procedure,
parameter, lot-tracked item with matching stock UOM, company, and quarantine
warehouse in advance. The three actor credentials inherit the URL and Frappe
site from `qms_reader`. The QA credential must authenticate as the top-level
`qms_approver`. LAB and QA need the existing submit and review permissions. The
dedicated reference writer needs read/create on Batch and draft Stock Entry,
with access to the requisite masters. Batch naming must preserve the source ERP
batch ID. The controller creates draft references and never submits them as QMS
inventory movements. Provisioning the dedicated account and site-specific masters
is an operator step. Keep credentials in environment variables.

## Evidence and qualification limits

Blocks B, D, G and I–L cover the shift queue, confirmed ERP/MES/PLM lot scope,
numeric assay, role-scoped native records and readback, and pending work. The
sample and result queue payloads identify their lab and lot scope. Replays
reconcile the native Batch and draft reference, inspection, nonconformance and
corrective action on that site. QA checks the current submitted inspection,
observed reading and signer. QA releases in-range results and rejects
out-of-range results with linked investigation and corrective action.
### Commissioned physical rework

A registered finished-product profile can opt in by adding
`"physical_rework": {"login": "rework", "password_env": "EAST_MES_REWORK_PASSWORD"}`
before registration. The bridge inherits the registered MES URL, database and
source ID. It needs the native `norvale_mes.group_rework_bridge` permission and
separate credentials from the reader, production operator and maintenance roles.
The profile uses its own QMS analyst, QA approver and release dispatcher.

The automatic work instruction is `QC-ADJUST-1` for a pinned pH assay with limits
5 and 6. A moderate high-side failure (above 6 and at most 6.60) of a whole-unit
finished lot of at least two `Nos` can qualify to salvage all but one unit. QA
requires the submitted assay, linked nonconformance and corrective action. The
bridge checks the original completion version and current local QMS authorization
before consuming the held lot in MES. ERP posts a Repack with a distinct held
output batch. LAB separately measures a new retest and QA approves it on the
original QMS site before the release dispatcher can authorize an ERP transfer.
Profiles without compatible commissioning retain ordinary rejection. A missing
native bridge permission or QMS approver blocks the affected lot at QA.

At draft creation, the operating lab reads the exact submitted ERP child row
under the appropriate native ERP role. The QMS reference records the ERP
source, site, document and child row, the QMS source and site, and the
commissioned reference writer and analyst. It uses the existing Stock Entry
remarks field and draft permissions. The analytical reader checks these
bindings against the committed profiles and verifies the native reference
owner, inspection author, operation and child row. The ordinary scheduled
Delta scan adds inspection → QMS Sample Reference → ERP row links for
submitted measurements, including rejected lots that remain quarantined.
A salvage retest links to the failed inspection on the same QMS site. The collector checks the retest analyst, the shared receipt row and the authorized salvage quantity before following the original inspection to ERP. The existing primary commissioned sample path uses the same collector.

Older registered draft remarks remain usable by the operating controller with
their original operation keys. They omit an explicit ERP source and site.
The scheduled collector leaves those references unlinked because matching
local IDs alone cannot establish the source.

Offline qualification runs the recurring inspections phase and durable shift
queue from posted-lot fixtures, with separate in-memory QMS deployments behind
the real REST clients. It checks source-qualified genealogy in the ordinary
scheduled Delta archive, including a site outage, same-cutoff restart, and
colliding native IDs. It also covers registered inbound assays, a profile
without opt-in, missing parameter, mismatched QA identity, a lost draft-reference
reply, and restart after a QA release committed. Live Frappe permissions and
stock-ledger posting are outside this offline qualification. Draft recovery
assumes one controller writer and read visibility after commit.

```sh
PYTHONPATH=.:native python3 -m unittest integrations.company_run.test_registered_lab integrations.company_run.test_primary_sample integrations.company_run.test_scheduled_lake
```
