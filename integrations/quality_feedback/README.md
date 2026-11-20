# Shipment feedback and corrective effectiveness

`investigate` reads the submitted ERP Delivery Note and its outbound serial-and-batch bundle. It records the shipment source, customer, line and batch in a native QMS Non Conformance. Its operation ID must remain stable across retries. `corrective_action` links a native Quality Action to that case. `contain` calls the QMS approver's release revocation workflow. The recurring ERP lot synchronizer consumes the updated disposition and rechecks eligibility before submitting a delivery.

The QMS provisioning configuration installs a native Before Save rule on Quality Action. A completed action needs a documented assessment and a later, released inspection of the same batch. The approving user needs the QMS approver role. Completed actions retain their evidence. The rule uses Frappe Server Scripts on the dedicated QMS bench.

Run the isolated proof on a fresh dedicated QMS site with its fixture configured, a
writable temporary mount at `/data` in the Bench container, and the lake
dependencies installed in the proof runner:

```sh
PYTHONPATH=. python3 -m integrations.quality_feedback.prove "$runtime" "$runtime/feedback-proof"
```

The proof places an ERP shipment and QMS on one isolated site to exercise role-scoped native APIs. Production callers may supply distinct ERP and QMS clients. The inspection, customer complaint and historical shipment remain independently readable.

For an existing recurring company configured with QMS credentials, use
`python3 generate.py feedback --run RUN report --operation ID --delivery DN --line ROW --batch BATCH --description TEXT`.
The command requires a confirmed shipment from that run, verifies the native
outbound lot bundle, and opens the linked complaint and corrective action.
The allocation quantity must match the shipped row in stock units. Its
inspection version and whole-lot quantity are checked against the submitted
QMS dispatch lease. Older shipments can use the still-current native release.
After revocation, an older shipment without a lease needs the same allocation
in its confirmed fulfillment checkpoint.
Use `contain --complaint CASE --release INSPECTION` for an authorized revocation
and immediate ERP stock reconciliation. Use `close --action ACTION --inspection INSPECTION --assessment TEXT`
to submit an effectiveness assessment. These commands acquire the recurring
controller's execution lock and use its existing application connections.

The earlier live proof on ERPNext 15.124.1 recorded complaint `QA-NC-00005` for
two units on `MAT-DN-2026-00001`. Its separate corrective action stayed
open when the approver supplied a pre-complaint inspection. The earlier
process corrective action closed against independent inspection
`MAT-QA-2026-00004`. Revocation preserved the delivery. After the isolated
services restarted, the QMS export was identical and the lot remained held.

## Customer withdrawal and receipt

On existing QMS sites, the site operator installs the additive native case
fields, approval guard and approver permission using
`integrations/qms/provision.sh withdrawal-only QMS_RUNTIME`. Fresh QMS setup
includes these changes. The case decision is tied to the revoked release
pinned on the original outbound Delivery Note. Approval keeps the shipment
and invoice in place and creates no stock posting.

```sh
python3 generate.py feedback --run RUN authorize-withdrawal \
  --complaint CASE --release INSPECTION --reason 'Collect affected units'
python3 generate.py feedback --run RUN receive-return \
  --complaint CASE --delivery DN --line ROW --batch LOT \
  --quantity 2 --observation RECEIPT_ID --observed-on YYYY-MM-DD
```

Use one stable warehouse receipt reference per observed arrival against a
single Delivery Note row and its single non-serialized lot. Quantities use
stock UOM. Each receipt gets a submitted native return Delivery Note with
an inward batch bundle and stock ledger row in Quarantine. The verified
observations, return documents and QMS case links are available in filtered
lake scans. Customer-return stock is reserved from normal lot release.
ERPNext may update the outbound note's returned quantities and generate
its ordinary inventory valuation entries. Customer credits, refunds and
other commercial or accounting adjustments require separate authorization
and have no command here.

If the result is uncertain, rerun the same receipt reference and identical
evidence. The checkpoint and ERPNext's unique operation field recover a
committed draft or submission. A different receipt for that shipment row
waits until the first is reconciled. This serializes engine-controlled
receipts within the recurring run. External ERP writers remain subject to
native ERPNext validation and are outside the run lock. Earlier return notes
for the same item without a source row require reconciliation. Returned units
stay reserved in Quarantine until a separate disposition process handles them.

### Feedback on a registered QMS site

For each additional QMS accepting complaints, set `feedback_sites[QMS_SOURCE]`
in the private run file `env/company-sources.json`. Each entry uses the
existing `qms_reader` and `lab` structure, including the analyst, approver
and complaint procedure for that site. The reader and actors must address
the QMS site registered for the production family. Keep these credentials
in the private run file.

Report and return commands obtain the QMS source from the release pinned on
the submitted Delivery Note. When additional feedback sites are configured,
pass `--qms-source SOURCE` after `contain`, `authorize-withdrawal` or
`close` to identify the site owning the case or action. Command output
includes the QMS source with its native IDs.

The `--batch` on report and receipt is the ERP batch in the posted outbound
bundle. For a physical rework shipment, the case separately carries the
original QMS Batch, the ERP salvage batch, the registered QMS source and the
pinned release inspection. Containment reconciles that salvage batch in ERP.
Effectiveness needs a later released inspection of the same QMS and ERP batch
scope. Repeating a report with the same operation and complaint intent
preserves the corrective action and its assessment after completion.

Install the updated guards with the existing `withdrawal-only` setup on each
participating QMS site. An older pin in structured Delivery Note remarks can
be used if the posted bundle, one release allocation, registered lot
authority and native inspection agree. An approver can fill the missing case
binding once during withdrawal or closure. An already posted ordinary-lot
return can still be replayed with its original receipt evidence.
