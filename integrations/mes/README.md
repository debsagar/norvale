# Norvale MES integration

The dedicated MES deployment uses the Manufacturing and Inventory apps in
Odoo Community, with its own PostgreSQL database and Docker volumes. Qualified version: Odoo Server **18.0-20261112**,
PostgreSQL **16.16**. Compose pins both official image digests. Native XML-RPC
authentication and `execute_kw` work in this self-hosted Community image. The
test installs the open-source `mrp` module and the local bridge addon. [Odoo Community license](https://github.com/odoo/odoo/blob/18.0/LICENSE)

- [Official Odoo 18 RPC documentation](https://www.odoo.com/documentation/18.0/developer/reference/external_api.html)
- [Official manufacturing lot workflow](https://www.odoo.com/documentation/18.0/applications/inventory_and_mrp/manufacturing/workflows/manufacture_lots_serials.html)
- [Official manufacturing split workflow](https://www.odoo.com/documentation/18.0/applications/inventory_and_mrp/manufacturing/workflows/split_merge.html)
- [Official Community image definition](https://github.com/odoo/docker/blob/master/18.0/Dockerfile)
- [Upstream manufacturing order code](https://github.com/odoo/odoo/blob/18.0/addons/mrp/models/mrp_production.py) and [native backorder wizard](https://github.com/odoo/odoo/blob/18.0/addons/mrp/wizard/mrp_production_backorder.py)
- [Upstream work order code](https://github.com/odoo/odoo/blob/18.0/addons/mrp/models/mrp_workorder.py), [work center productivity code](https://github.com/odoo/odoo/blob/18.0/addons/mrp/models/mrp_workcenter.py) and [stock move line relations](https://github.com/odoo/odoo/blob/18.0/addons/stock/models/stock_move_line.py)

## Native model and engine handoff

| A–L blocks | Engine responsibility | Adapter and resulting evidence |
| --- | --- | --- |
| A, D | Provision role-specific MES identities, lot-tracked SKUs, native lots, BoMs, per-equipment work centers, dedicated hold location and released material stock mirror | Odoo manufacturing manager and operator use native ACLs. Fixture bootstrap illustrates the native records. |
| B, C, I | Read the effective PLM revision, authorize ERP work, reserve equipment time, compute actual lot movements and recheck QMS dispositions | `acknowledge` fingerprints the approved `mrp.bom` definition. `dispatch` pins that value and the BoM on `mrp.production`. `start` rechecks the definition, reserves specified lot quantities and starts `mrp.workorder`. |
| C, I, J | Feed observed equipment faults and an authorized restart reference | `fault` and `restart` open and close an availability-loss `mrp.workcenter.productivity` record. `availability` also checks for an occupying work order. |
| J, K, L | Journal the requested completion before transport. Post the read-back result in ERP hold stock once, then request QMS testing | `complete` marks the native MO done, uses native backorders for remaining quantity, and checks the resulting `stock.move.line` records and both directions of their lot genealogy. `completion` independently reconstructs the posting payload. `export` returns work, labor and equipment evidence with native IDs and URLs. |

The addon groups Odoo operations into native database transactions. It adds
one field, `mrp.production.norvale_revision`, which is carried to native
backorders. Odoo supplies the manufacturing orders, work orders, reference
uniqueness, inventory and reservation rules, backorders and genealogy. Odoo's
initial native reference is derived from the ERP work reference. The native
ID and origin remain stable when Odoo adds backorder suffixes.

## Callable interface

From the release root:

```python
from integrations.mes import Mes

planner = Mes(url, "norvale_mes", planner_login, planner_secret, "norvale-mes-run-42")
operator = Mes(url, "norvale_mes", operator_login, operator_secret, "norvale-mes-run-42")
ack = planner.acknowledge(bom_id)  # native BoM ID, after validating PLM effectivity
mo = planner.dispatch(erp_work_ref, product_id, ack["bom_id"], ack["revision"],
                      planned_qty, hold_location_id)
wo = operator.start(mo["id"], planned_input_lots, released_lot_ids)
report = operator.complete(mo["id"], output_lot_id, finished_qty,
                           actual_input_lots, released_lot_ids)
same_report = operator.completion(mo["id"])
observed = operator.export([erp_work_ref], [center_id])

fault = planner.fault(center_id, maintenance_ticket_ref)
planner.restart(center_id, fault["native_id"], authorized_restart_evidence_ref)
availability = operator.availability(center_id)
```

`planned_input_lots` and `actual_input_lots` contain rows of
`{"lot_id": native_id, "quantity": measured_amount}` in each product's native
unit. Use all component products at start, with total reservations equal to
the plan. Completion accepts actual quantities and requires a fresh list of
released MES lot IDs. The controller supplies that list only after reading
authoritative disposition and mapping it to native MES lots. Use distinct
output lots per completed MO or backorder. ERP should map item and lot keys
from the returned product/item and lot refs. The payload carries quantities,
units, move line references, hold location, ERP work reference, pinned
revision and any open successor. Server timestamps are in UTC. The engine
supplies its own business-effective minute.

Keep `source_id` stable for the lifetime of the MES database. Use
`report["completion_ref"]` as the unique ERP posting key. On timeout, read
`completion(mo_id)` or retry `complete` with the original lot/movement payload.
An existing completion is checked against the request and returned without
reposting or rechecking a now-changed release list. A different request fails.
Dispatch replay checks the original requested quantity across the split
family. When ERP fails, retain the MES completion and retry ERP posting using
the same key. Neither this adapter nor its tests write to ERP.

## Isolated live verification

Docker Compose, Python 3, curl and OpenSSL are required. For the standard restart
test, install the Python packages in `integrations/lake/requirements.txt`. Choose a unique
project and free loopback port. The harness uses separate project-scoped
volumes, an unpublished PostgreSQL service, caps of 1.5 GiB and 2 CPUs across
both containers, and generated fixture passwords. It checks resource
availability, port and project ownership before starting. It installs and
seeds the isolated database, runs the first process, stops Odoo and restarts
PostgreSQL, then runs the second process to reconcile the discarded completion
reply. The harness stops both services on exit and keeps private evidence
and volumes. Review its printed directory for command logs, native output and
the fixture checkpoint. The same restart test discards a staging receipt reply.
It verifies one two-unit credit against the original manufacturing order, checks
that a changed order or quantity is rejected, and writes the independently read
move, line, lot and balance to `staging-recovery.json`. The analytics account
must be denied a stock posting and a direct lot edit. The test runs the ordinary
MES lake collector using that account before and after restart and compares its
receipt with the native warehouse readback. Repeating the scan must leave the
Delta version unchanged.

```sh
MES_TEST_SOURCE=nvmes-local-your-suffix MES_PORT=18680 bash integrations/mes/verify.sh
```

For an engine-operated instance, set `MES_DB_PASSWORD` and `MES_PORT` yourself,
then use the following from `integrations/mes`. Provision the isolated shop
via the native ORM shell or administrator before opening the application.
`fixture.py` is an executable example of the required native models and roles.

```sh
export MES_TEST_SOURCE=nvmes-engine-your-suffix
docker compose -p "$MES_TEST_SOURCE" run --rm -T odoo \
  --database=norvale_mes --init=norvale_mes --without-demo=all --stop-after-init --no-http
# Provision native records and rotate the initial admin password through:
# docker compose -p "$MES_TEST_SOURCE" run --rm -T odoo odoo shell -d norvale_mes --no-http
docker compose -p "$MES_TEST_SOURCE" up -d odoo
docker compose -p "$MES_TEST_SOURCE" stop odoo db
```

## Observed results

The live fixture dispatches eight shampoo units on filler A and two on filler B.
Filler B completes while A is blocked. After verified restart, A completes
three units using 1.5 kg from `BULK-104`, 0.25 kg from `BULK-105` and four
`BOTTLE-663` units. The five-unit backorder retains the originating ERP work
reference and pinned revision. The replay after service restart recovers the same
native completion and leaves stock unchanged. All 10 completed units are in the
native hold location. The export contains three completed MOs with operations,
labor, lot genealogy and source URLs. The observer cannot read manufacturing
orders or start work, the operator cannot declare a fault, and stale revisions,
held or exhausted inputs, conflicting replay and occupied equipment all fail.
Equipment restart restores the equipment's health while interrupted work
remains pending batch disposition.

## Scheduled operating labor

The timed operating participant passes the first completion edge's persisted
production envelope to `norvale_complete_scheduled`. The operator who opened
the original native productive timer must post the output. Odoo commits the
output and its business-minute envelope together and returns the native
`mrp.workcenter.productivity` ID. Replaying the same output and envelope returns
that ID. A backorder requires its own timer and completion event. Previously
completed work cannot acquire an envelope through replay.

`integrations/lake/mes_reader.py --labor` reads these rows under native source
permissions. The recurring lake scan also archives them as
`mrp.workcenter.productivity` snapshots keyed by source and native ID. The
allowlisted payload includes MO, operation, work center, user and output IDs,
the event fingerprint, and worked intervals in business-clock minutes. The
native Odoo timer timestamps and duration are exported separately.

## Remaining integration scope

- The caller owns PLM approval/effectivity, QMS revocation and authoritative
  release/receipt synchronization, ERP stock posting and the shared engine's
  simulated clock. The fixture seeds native quants through Odoo's ORM.
- Execution currently supports one routed operation, one output lot per
  completion, a single capacity slot per work center, tracked components in
  their base units and a dedicated hold location configured by the
  caller. Remaining work splits through Odoo when actual output is partial.
  Explicit scrap, byproducts, advance splitting, rescheduling and multi-step
  routing require additional flows.
- Faulted in-flight work needs a separate batch disposition. The adapter
  leaves that work pending after an equipment restart. The verified restart
  reference is asserted by the authorized caller and recorded as evidence.
- Use the bridge as the sole serialized writer for a given shop. Odoo's native
  manufacturing roles retain their native UI/API powers. The cross-system
  guards apply through this bridge. Concurrent manual edits, duplicate output
  lot selection across independent controllers and full-shift scheduling
  remain outside this qualification.
