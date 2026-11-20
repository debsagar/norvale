# PLM revision receiver bridge

The controller imports an approved InvenTree revision as a submitted ERPNext BOM and a routed Odoo Manufacturing BOM. It reads both BOMs back, records both acknowledgements on the InvenTree revision, and checkpoints the receiver evidence with the released work. Dispatch calls the MES adapter with the original Odoo BOM ID and its native execution fingerprint.

The PLM fingerprint covers the locked definition and carries source identity. Each receiver independently checks its stored import key, output basis, exact item and unit identities, all line quantities, and applicable BOM options. Odoo also checks routing and obtains its execution fingerprint through `Mes.acknowledge`. That fingerprint is passed to `Mes.dispatch`, which checks it in the manufacturing transaction for new work orders. A copied source fingerprint alone cannot authorize a receipt.

## Installation

Apply the release-relative patch from the release root with `git apply /path/to/revisions.patch`. Overlay the PLM and MES builders' `integrations/plm` and `integrations/mes` packages first. The bridge uses `PLM.publish_to`, `PLM.pin`, `PLM.resolve_pin`, the MES client and `norvale.loader.erp_client.ErpClient`.

- Install `integrations/revisions/odoo_addons/norvale_revision_receiver` on Odoo 18 alongside `norvale_mes`. The addon gives imported native BOMs a database-unique import key. The Odoo account needs local manufacturing manager access for BOM creation and dispatch.
- Run the one-time `provision-erp` command with a customization account on ERPNext 15. It installs a unique, non-copyable BOM custom field. The runtime ERPNext credential needs read access to Company, Item and UOM records and read, create and submit access to BOMs.
- Use a PLM publisher credential. Run one controller per InvenTree store, as required by the PLM adapter. Set `bridge.source` to a stable unique name for the deployment and use the same checkpoint file across runs. Back up that SQLite file with the controller state. Give each work order a stable work ID qualified by its originating system if needed.

The products, stock units, hold location and work center must exist before publication. Keys in `mapping` are the PLM product name and the component IPNs as returned by `PLM.publication`. For each receiver, set the source component's exact unit and kind, the destination item and stock unit, and a numeric `factor` in destination units per source unit. For example, 250 g times 0.001 yields 0.25 kg. The published lines describe one BOM basis. Set `output.qty` to the native output quantity produced by those lines because the PLM interface has no separate finished-output quantity or unit. All component quantities must fit the destination units and survive native readback exactly. The bridge uses direct stock-unit inputs, no substitute or exploded BOM lines, and one explicit Odoo operation.

See `config.example.json` for the complete mapping structure. Credentials come from `REVISIONS_PLM_TOKEN`, `REVISIONS_ERP_TOKEN` and `REVISIONS_MES_PASSWORD`. The ERP token is `api_key:api_secret`. Use `plm.basic: true` and a `login:password` credential only for isolated PLM qualification.

```sh
python3 -m unittest -v integrations.revisions.test_bridge
python3 -m integrations.revisions --config config.json provision-erp
python3 -m integrations.revisions --config config.json publish 12 --on 2027-01-04
python3 -m integrations.revisions --config config.json release 12 erp:WO-0001 --on 2027-01-04
python3 -m integrations.revisions --config config.json dispatch erp:WO-0001 4 19 --on 2027-02-01
```

`publish` takes the approved native revision ID. `release` takes the original Part ID of the revision family. A retry must use the original definition and release date. A partial publication can leave a verified draft or one receipt. Rerun `publish` with the original mapping to resume. If a destination BOM differs, publication and dispatch stop with an error. A later effective revision must have its own two receipts before new work can release against it. Existing work keeps its earlier native BOM.

From Python, construct `Bridge(plm, ERPNext(erp), Odoo(mes), source=..., mapping=..., checkpoint=...)` and call `publish`, `release` and `dispatch` with the same arguments. Treat exceptions as blocked work. Record ERP work release only after `release` returns. The caller verifies that the originating work order matches the mapped product and owns the native ERP work-order lifecycle and controller scheduling.

## Receiver-scoped receipts

Set `bridge.scoped_receipts` to `true` before publishing new revisions. Fresh company-run provisioning enables scoped receipts. For an existing company, the equivalent is `configure --scoped-receipts` or `scoped_receipts: true` in an operator production profile after commissioning. For opted-in sites, run ERPNext provisioning and install or upgrade `norvale_revision_receiver` to 18.0.1.1.0. ERPNext provisions a non-copyable, read-only BOM field with a random, once-per-site UUID default. The bridge reads the submitted BOM field and the live BOM metadata through its ordinary runtime credential, which needs no Custom Field administration permissions. The two values must match. On Odoo, the Manufacturing role can read a computed digest of the native database UUID through the native BOM. ORM create and write reject supplied values for that field.

PLM stores only the two observed opaque markers with new scoped acknowledgements. The pin preserves the native BOM IDs and the markers, and both installations are checked again during release and dispatch. Existing acknowledgements and work pins are immutable. A replay uses the original receipt format, including when a revision has receipts from both sides of the opt-in. Legacy receipts establish BOM and content matches using the configured connections. Independent receiver identity is established only for receipts with observed markers. The lake retains markers on receipts, work definitions, ERP BOMs and MES completions. A qualified shipment link requires each recorded marker to agree with the corresponding independent native observation.

A database clone carries its identity unless separately recommissioned. Identical clones that preserve the ERP metadata default and Odoo database UUID are indistinguishable by these markers. Use fresh, separately commissioned receiver databases for independent sites. A clone intended to become a distinct receiver requires a new identity under operator control and new scoped publications. Receipts for the original database remain bound to its original marker. Existing ERP BOMs are never backfilled by provisioning. Publish a new revision for fresh scoped evidence when native records predate commissioning.

## Isolated qualification

Use fresh local InvenTree, ERPNext and Odoo services with separate databases and volumes. Start InvenTree from the PLM builder's compose and run its database migrations and `provision.py`. Start Odoo from the MES builder's compose with both addons in the mounted addons directory. Install `norvale_revision_receiver` into the empty database and run the MES builder's `fixture.py` through Odoo's ORM shell. Complete the setup wizard on a clean ERPNext site, then provision an API token allowed to customize BOMs and create example records. In a copy of the config, use the local service endpoints and set `example.workcenter_id` and `example.hold_location_id` from the MES fixture output. The live check builds its own explicit mapping and creates a dedicated company, example ERP items, two PLM revisions, four native BOMs, and one Odoo manufacturing order. It expects the example ERP items to be absent before the run.

```sh
python3 -m integrations.revisions.check_live isolated.json /path/to/new/qualification.sqlite3 > qualification.json
```

Norvale blocks A and D supply role identities, product masters and approved revision evidence. Blocks I and J enforce effectivity, mappings and the pinned dispatch. Blocks K and L preserve native acknowledgements, replay and explicit failures. ERPNext can promote the first submitted active BOM for an item to its default. A work-order caller must pass the explicit ERP BOM ID from the release record. This bridge handles flat BOMs and one Odoo routing operation. Plant-wide rollout, multi-operation routing, ERP work-order release orchestration and engine scheduling are separate work.
