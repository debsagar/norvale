# PLM definition handoff

InvenTree 1.3.6 (MIT), upstream commit
`f3cc71022b14face3cc3dd9b3eaa9d4d63352379`, provides the independently stored
product definition. The qualification image reports API version 477. Each
revision is a native assembly Part with native BOM entries for formulation and
packaging. The first revision is the root Part and later Parts reference it with
`revision_of`. The assembly name retains the source product ID, the Part
`revision` retains the source revision, and input Part `IPN` retains the
source item ID. BOM rows retain native row and component IDs, quantities,
units, and specification notes.

InvenTree's model metadata API holds Norvale draft digests, review and approval
evidence, effective dates and receiver receipts. The adapter enforces those
transitions and verifies a native assembly lock on approval. InvenTree enforces
the lock on BOM edits and its own group permissions on the native endpoints.
Approval is a Norvale workflow in supported metadata. The adapter verifies
content on publication and on resolving a pin.

- [InvenTree Part API](https://docs.inventree.org/en/1.3.x/api/schema/part/)
  and [BOM API](https://docs.inventree.org/en/1.3.x/api/schema/bom/).
- [Official metadata contract](https://docs.inventree.org/en/1.3.x/plugins/metadata/).
- Pinned upstream [Part/BOM endpoints](https://github.com/inventree/InvenTree/blob/1.3.6/src/backend/InvenTree/part/api.py),
  [Part/BOM models and lock checks](https://github.com/inventree/InvenTree/blob/1.3.6/src/backend/InvenTree/part/models.py),
  [metadata routes](https://github.com/inventree/InvenTree/blob/1.3.6/src/backend/InvenTree/common/api.py),
  [metadata merge semantics](https://github.com/inventree/InvenTree/blob/1.3.6/src/backend/InvenTree/plugin/serializers.py),
  and [native permissions](https://github.com/inventree/InvenTree/blob/1.3.6/src/backend/InvenTree/InvenTree/permissions.py).

## Controller interface

Import `PLM` and `Blocked` from `integrations.plm`. Construct
`PLM(url, token, publisher="norvale-publisher")` with the publisher's native
API token. The publisher identity is checked against `GET /api/user/me/`.
Call dates are business dates (`YYYY-MM-DD`). Use the existing controller
clock, authority checks and checkpoint.

| Blocks | Callable | Evidence and outcome |
| --- | --- | --- |
| A/J/K | `draft(product, revision, lines, base_id=None)` | Returns an InvenTree Part integer ID. Lines have `source_id`, `unit`, `kind` (`formulation` or `packaging`), `quantity` and `specification`. Include both kinds and each input item once. Replays reconcile the Part and BOM against the same intent. |
| I/J/K | `review(pk, on=..., evidence=...)`, then `approve(pk, on=..., effective=..., evidence=...)` | Pass actual authorized review and approval references. Assign an effective date on or after approval. Approvals lock the native assembly, bind its content digest and return a publication. |
| J/K/L | `publish_to(pk, system, on=..., ensure_target=..., read_target=...)` | `system` is `erp` or `mes`. `ensure_target(publication, retry_id)` reconciles or creates an import and returns a nonempty string native ID. The retry key is stable for a given receiver and source version. Use it within the run's receiver namespace. `read_target(target_id)` must verify the imported content against native readback and return `native_id`, `plm_native_id` and `plm_source_version`. The adapter records the receipt only after checking these against the PLM publication. Use `acknowledge` with the same readback contract when a receiving record already exists. |
| D/I/L | `pin(base_id, work_id, on=...)` | Selects the latest approved effective revision and requires both receipts as of the release date. A newly effective revision with missing receipts blocks new release. Persist the returned JSON, including both receiving IDs, before any native work-order writes. |
| D/I/K | `resolve_pin(pin, work_id=..., on=...)` | For retries and for work already released, verifies the recorded ID and digest. Use the returned definition and the receiver's corresponding imported record when starting or completing that work. |
| K | `export(base_id, on=...)` | Returns JSON-ready native Part and BOM IDs, links, versions, specs, approval evidence and receipts observed by the cutoff. Feed these records into the existing export path. |

For the shared `native/work_revision.py` helper, map publication
`native_id` to the string revision ID, `source_version` to version, and
`definition.name` to product. Convert approval effectivity and receipt dates
using the engine clock. The next approved effective revision supplies the
supersession boundary for new work. Keep the originating ERP/MES work
reference as the checkpoint key. The lake reader can consume `publication(pk)`
directly.

The controller must keep `Blocked`, HTTP failures and uncertain writes pending
and use the same intent on replay. Serialize all calls that write to this
store. Native metadata PATCH merges top-level keys and offers no conditional
write. On a client timeout, reconcile after the original native request has
finished before deciding an absent BOM row can be retried. The qualification
test exercises loss after confirmed commit. Simultaneous writers and still
running requests require additional native idempotency or operational
reconciliation before resubmission.

The pinned product is a single-level definition. BOM substitutions and
quantity modifiers are blocked. The publisher is a trusted service account
with native Part view/add/change, no delete, and can write metadata directly.
InvenTree 1.3.6 requires Part change permission even for metadata GET, so
serve approved projections through the controller when roles need this
evidence. Authorizing specific human reviewers/approvers remains the
controller's responsibility. Preserve the receipt IDs in each receiving
system and its crosslink to the exact source revision. The adapter cannot
prevent a privileged writer from directly changing approved metadata.

## Isolated qualification

Use a fresh Compose project, unused port, and private temporary directory.
The only store is the project-scoped volume containing InvenTree SQLite.
Compose binds the web port to loopback and caps the API service at 768 MiB
and one CPU. The commands use the upstream migration and Django shell to set
up fixture identities. Neither a queue worker nor a front-end proxy is required
for these API checks.

```sh
export PLM_PROJECT=norvale-plm-$(openssl rand -hex 4)
export PLM_PORT=18357
export PLM_URL=http://127.0.0.1:$PLM_PORT
export PLM_PUBLISHER_PASSWORD=$(openssl rand -hex 24)
export PLM_READER_PASSWORD=$(openssl rand -hex 24)
plm_evidence=$(mktemp)
plm_compose() { docker compose -p "$PLM_PROJECT" -f integrations/plm/compose.yaml "$@"; }
free -h
df -h /data
ss -ltn "sport = :$PLM_PORT"
plm_compose run --rm --no-deps plm python3 src/backend/InvenTree/manage.py migrate --noinput
plm_compose up -d
curl -fsS --retry 60 --retry-all-errors --retry-delay 1 --max-time 5 "$PLM_URL/api/"
plm_compose exec -T -e PLM_PUBLISHER_PASSWORD -e PLM_READER_PASSWORD plm \
    python3 src/backend/InvenTree/manage.py shell < integrations/plm/provision.py
python3 -m integrations.plm.check_live create "$plm_evidence"
plm_compose restart plm
curl -fsS --retry 60 --retry-all-errors --retry-delay 1 --max-time 5 "$PLM_URL/api/"
python3 -m integrations.plm.check_live restart "$plm_evidence"
plm_compose down
```

The isolated test uses native Part records as receiver fixtures for ERP and
MES callback verification. The parent integration still needs to wire the
actual ERP and MES import adapters and demonstrate their native readback,
work-order dispatch and restart through the shared controller. The standalone
fixture runs through real PLM persistence and native permissions.

### Observed results

The local API reported InvenTree **1.3.6**, API **477**. The final run used
Compose project `norvale-plm-47697459`, loopback port `18357`, and
`PLM_FIXTURE_PRODUCT=FG-LOTION-RETEST` because the same volume already held
the first qualification. The live commands above printed:

```text
create: 2 revisions, old Part 10, new Part 13
restart: 2 revisions, old Part 10, new Part 13
```

- A and B retained their native BOM rows (5–6 and 7–8 respectively).
  Readback and JSON export preserved `RAW-BASE`, `PKG-A` and `PKG-B`.
- Dropped responses after native Part creation, BOM creation and approval
  reconciled on retry. The read-only account could read the Part and received
  HTTP 403 for both Part unlock and approval metadata PATCH. The publisher
  received HTTP 400 when changing a BOM row on an approved locked assembly.
- A remained pinned for work released on January 4, 2027. B was approved that
  day with February 1 effectivity. Before February 1, new work selected A.
  On February 1, new release was blocked until both receipts for B were
  recorded. A receipt for A was rejected as proof for B. After restarting the
  API and client, both pins resolved to their original native revisions and
  receipt IDs, and export matched the saved pre-restart snapshot.
- Release offline checks passed (`scripts/check_data.py`,
  `native/check_core.py`, `native/check_routing.py`, `native/check_retry.py`,
  and `generate.py --help`). Frozen business exports were left intact.
