# Seeded world processes

Set `world: true` in a new engine consumer configuration. For real recurring Norvale, `generate.py --provision --world --run <fresh-directory>` opts in during provisioning. Resume with the saved configuration. The recurring company checkpoint binds the seed and fixture at the first persist, and the calendar checkpoint binds the complete consumer configuration. Leave the option absent for the existing recurring behavior. The 120-day dataset and fixed-scenario run are protected by the existing run-path checks.

## Calendar and role evidence

- The calendar generates a sample at run start and every 10,080 simulated minutes. Disruption and supplier reliability each use a separate random stream derived from the seed and the week's calendar minute. Neither transition reads company actions or the other process. The default initial conditions are calm and reliable. Test fixtures may select the opening disruption and force a delivery delay.
- Both consumers get the same carrier bulletin through native mail records, scoped to the manager. The first week's incident text is identical across a false alarm, short disruption and long disruption. Subsequent bulletins reflect conditions as they develop. The carrier bulletin conveys uncertainty without quoting hidden labels or forecasts.
- Norvale's recurring procurement (operating-loop blocks A and B) calls the same delivery boundary from `Engine.schedule_supplier_arrival` after submitted native PO readback. It takes the latest promised date across the submitted PO lines, with the existing header fallback. On the promised day, the delivery checkpoint reads that week's conditions. Approval on or after the promised day sets the checkpoint to the next day while keeping the original PO date for the notice. A delay sends date-stamped mail to SCM_MGR and WAREHOUSE before the calendar posts the physical arrival. A bulletin goes to BUYER, SCM_MGR and WAREHOUSE. Stable mail keys reuse the controller's existing recipient readback and recovery logic.
- A delay notice identifies the PO and its original promised date and says revised timing is pending. The role evidence for subsequent approvals uses confirmed mail readbacks through `native.company_world.evidence`, including the recipient and the as-of date. The queued arrival time stays in the evaluator checkpoint. The existing warehouse phase maps and submits the Purchase Receipt after its configured booking lag, then carries on with quarantine, QA and billing.
- Delay magnitudes are Norvale scenario assumptions. A disruption adds seven days and the supplier chain adds 0, 3, 7 or 14 days according to reliability. The last value models substitution delay after supplier failure. The checkpoint schedules one arrival per commitment. This increment uses one representative reliability process with a single checkpoint per shipment and retains the usual receipt quantity and pricing paths.

## Viewer integration

`Engine.private_truth()` returns a detached evaluator snapshot with `world` samples and, after transit evaluation, `deliveries` keyed by commitment. The handoff is `Engine.viewer(include_private=True)` → `_journal.viewer(..., private_truth=...)` → the escaped `#private-world` section with an explicit private label. The default viewer passes `None`. Keep this argument out of role observations, providers, exports and the default viewer. Public bulletin and notice writes appear through the existing record → event and action → call attempts links. Native recurring notices use the native trace's `native_mail_readback` and the world event ID. The offline call-journal viewer covers journaled engine transports.

## STOCKTAKE provenance

The transition laws and calibrated defaults were adapted from the local STOCKTAKE reproducibility release at `backend/src/world/`. The matching STOCKTAKE release commit is `5b90dac`. The source archive has no Git metadata. These are the SHA-256 identifiers for the inspected files.

| Source | SHA-256 |
| --- | --- |
| `config.py` | `9dbce979a1db5f15febfac2532190dd783abfb767638a73c3e7c673153359d53` |
| `modules/disruption/factor.py` | `8223eb70b41961c69fa661c9952a2cf492df570488c0f076cfb21bc7bb4f4b68` |
| `modules/supplier/factor.py` | `5ee169f9f8e6fc3ae29ade4f7e9b31382e23321fae0fe97fad000ab9fad9ff3d` |
| `modules/supplier/config.py` | `9c6ac65a057887a83a8563570d82818d45a8c9b7c73d8786be4e929b31b30458` |

`engine/world.py` ports the two transition laws, including age limits, disruption-type choice, the local congestion coin and the absorbing supplier state. It uses newly written bulletin text with the source's shared first-warning pattern. The independent seed derivation and delivery calendar belong to this implementation. The port has no runtime dependency on the archive.

## Offline proof

Run the Milestone 3 goals and the recurring controller and privacy checks.

```sh
python3 -m unittest discover -s tests -p test_engine_goals.py -k Milestone3 -v
python3 -m unittest discover -s tests -p test_world_processes.py -v
```

The recurring test drives the real procurement methods and calendar with injected ERP and mail endpoints. It checks a restart between recipient notices, the absence of an early receipt and a single warehouse booking after arrival. These tests use local temporary checkpoints and cost-free provider doubles.
