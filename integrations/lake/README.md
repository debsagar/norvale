# Local Delta reader boundary

A–L blocks D/K: this is an independent, single-writer analytics process; operational decisions continue to read their authoritative systems. Run the collector independently so an analytics retry leaves operational processing available. The dedicated QMS uses ERPNext Quality with role-controlled workflows; its live qualification and release semantics are in `integrations/qms/README.md`.

`boundary.py` reads Frappe v1 REST resources with a dedicated user's API key/secret in `FRAPPE_API_KEY` and `FRAPPE_API_SECRET` environment variables. Give that user only the source DocTypes and records it may read. HTTPS is required except loopback. [Frappe's official REST documentation](https://docs.frappe.io/framework/user/en/api/rest) documents list pagination and document reads; [authentication documentation](https://docs.frappe.io/framework-git/rest-api/authentication) documents token headers and user permissions. The live QMS reader collected inspections, nonconformances, quality actions and document-scoped workflow history from an isolated ERPNext 15.124.1 site.

Only the explicit parent/child fields in `FIELDS`, `CHILD` and `CHILD_FOR` enter Delta. This is a fixed analytical scope, not arbitrary row-level access control. The legacy `adapter.py` is a storage primitive, not a security boundary; do not call `Lake.ingest` on unfiltered records or rely on its role argument for authorization.

Use an OS account dedicated to the permitted analytics audience and a private local filesystem. The root directory is forced to mode 0700, state to 0600, and the Delta files inherit access through the private parent. No untrusted user may share that account, be root, or access a privileged backup. Separate audiences need separate OS accounts and lake directories; no per-role service is supplied. A different OS account was denied access in the local check. Ensure destination and parents are trusted, and do not publish exports to a broader audience.

```
python3 -m venv /tmp/norvale-lake-venv
/tmp/norvale-lake-venv/bin/pip install -r integrations/lake/requirements.txt
P=/tmp/norvale-lake-venv/bin/python
$P integrations/lake/test_boundary.py
$P integrations/lake/boundary.py --root /tmp/norvale-lake ingest --offline data/observable/native-records.json
# Or: set FRAPPE_API_KEY and FRAPPE_API_SECRET, then use ingest --url https://isolated-frappe.example
$P integrations/lake/boundary.py --root /tmp/norvale-lake status --max-age 86400
$P integrations/lake/boundary.py --root /tmp/norvale-lake export --entity 'Delivery Note' > delivery-history.json
$P integrations/lake/boundary.py --root /tmp/norvale-lake trace > links.json
```

Each scan reads full documents, projects before storage, and appends distinct content hashes with source, ID, modified, effective and ingestion times. State atomically records the greatest observed source modified value, last success/attempt, error class, and count per entity. Status computes age and staleness from the last success; source dates may be simulated and are not used as wall-clock freshness. A failure leaves the last successful watermark and marks stale; retry the command. A writer lock serializes scans. A crash after Delta append but before checkpoint is safe to replay. Delta history retains observed changed snapshots including cancellation (`docstatus=2`); it cannot recover intermediate versions never observed. If a previously archived Frappe record disappears from a list, the collector requests its known native URL with the same reader identity. A native document-specific missing-record response appends a `source_absent` observation. Its ingestion timestamp records when absence was observed. Access denials and generic HTTP errors do not establish deletion. A record that later reappears is archived again. Full rescans capture late corrections. Each page starts after the last native name read. An insert behind that cursor becomes visible on the next scan. Filtered manufacturing and registered-family publication scans retain their own freshness and error state, even when their records overlap another scan. Publication snapshots carry the native business-effective date. A full scan is intentionally simple and has a throughput ceiling.

Trace exports only explicit source references: work order, sales order, inspection reference, and child batch numbers. It does not infer genealogy from item/time. The supplied export lacks batch identifiers on many delivery rows, so it cannot prove a complete input-lot-to-shipment path. When a source exports a Serial and Batch Bundle, trace follows the delivery or stock line to that bundle and its native batch entries. Export retains signed quantities and warehouses. `export` contains all observed versions; `trace` uses the most recently appended distinct observation, not a historical as-of query. An observed change back to earlier content appends a new snapshot. Repeating the latest content is deduplicated. Never use either as an operational eligibility decision.

For InvenTree, use a scoped API token in PLM_TOKEN and run
`PYTHONPATH=. python3 integrations/lake/plm_reader.py --root DIR --source INSTANCE --url ORIGIN --publisher PUBLISHER --part ID --view bom`.
The BOM view only needs permission to read assemblies, component Parts and bill-of-material rows. The approved view additionally reads protected approval metadata and verifies the approved definition through the PLM adapter. Use a directory whose OS permissions match that audience. A denied approved scan marks that source/entity stale and never substitutes the lock flag for an approval.

The recurring publication scan also archives employee revision actions in
`InvenTree Change`. Each entry identifies the native Part and decision field,
the business date, and the ERP BOM version cited by the proposal. Reviews and
decisions retain their recorded actor, rationale and definition digest. An
approval keeps its separate effective date. A rejected Part remains visible in
this history and cannot supply a publication for a new work order. Access to
the publication and decision archive requires native decision-metadata read
permission. The basic BOM reader has a separate scope.

For a dedicated ERPNext Quality site, run the authenticated reader against
the site's loopback or HTTPS endpoint with a read-only account. Include
`--site SITE --source INSTANCE` and repeat `--entity` for
`Quality Inspection`, `Quality Inspection Workflow`, `Non Conformance`, and
`Quality Action`. The workflow scan reads each visible inspection's native
Desk timeline and retains the workflow comment ID, parent inspection,
timestamp and state. These entries preserve transition order, but have no
inspection version or lot scope.

New QMS sites record `Norvale QMS Disposition` in the same transaction as
Released saves and the transition from Released to Revoked. Each entry retains
the saved inspection version, actor, item, original and output batches,
specification, quantity and UOM. The company scheduler scans this ledger
with the site's reader, and standalone collectors can include it with
`--entity 'Norvale QMS Disposition'`. The archive keys events by source and
native ID and rejects changes to already observed entries. Older sites
retain their workflow comments and previously scanned inspection versions.
A missing historical version stays unresolved.

Use the PLM reader's `--view publication --on YYYY-MM-DD` with a family root in `--part` to archive review, approval, effectivity, receiver receipts and component lines as observed on the business date. Receipts identify the receiver as reported by PLM. Confirm the receiving instance and its native record before deriving a cross-system link.

Run `boundary.py --root DIR crosslinks` over a private archive containing the
participating sources. It compares native ERP work-order pins with PLM
publication IDs and hashes, stock-entry completion references with MES
completion IDs and recorded times, and shipment release references with
historical QMS inspection IDs, lot, item and version. Each edge names both
source instances. Unresolved references report how many candidates the archive
contains. Missing source history or overlapping IDs must be reconciled before
using those references in an explanation. This command does not consult
controller checkpoints.

MES completion ingestion retains each native manufacturing order, the IDs of
its execution operations, its hold location and any next backorder. Partial
and remaining quantities stay on their own orders. Use `trace` to follow the
backorder edge and the actual input and output stock-move lines.

To inspect a specific shipment, run
`boundary.py --root DIR genealogy --source ERP_INSTANCE --delivery DELIVERY_ID`.

For a released Material Transfer, use `boundary.py --root DIR genealogy --source ERP_INSTANCE --transfer STOCK_ENTRY_ID`. The result names the original QMS site, inspection and release version, the stock quantity and unit, both warehouses, and the native ledger entry and batch bundle for each side. The command exits with status 1 if a required link is missing or ambiguous. A historical trace establishes which decision authorized the transfer at the time. Current shipping eligibility requires a new QMS read.

For receiver-scoped receipts, run the collectors and genealogy from the release root with
`PYTHONPATH=.` so the lake can validate the bridge's marker formats. Qualified links
show `publication_receiver_markers` after comparing the PLM acknowledgements and work
pin with the independently collected ERP BOM and MES completion.
The result lists outbound lot quantities and units, the incoming manufacture
bundle, ERP work and stock entry, MES movements, pinned PLM publication, and
the QMS release version cited by that delivery. Check `unresolved` for any
missing segments. Complaint links also require the recorded customer, line
and batch on that specific ERP instance.

Recurring deliveries can also cite a Norvale Dispatch Lease. The QMS reader
archives the native receipt state, delivery and per-lot inspection versions,
units and quantities. A submitted matching receipt proves which version the
QMS checked even when the collector first reads the inspection after revocation.
Run the QMS lease migration for earlier dedicated sites before adding this
DocType to their reader configuration.

The recurring ERP collector reads sales orders as Sales, purchase commitments
and receipts as Buyer, stock ledger entries as Warehouse, and invoices,
payment allocations and GL entries as Finance. The private trace follows each
payment allocation to its cited invoice and each invoice line to its delivery
or receipt and order. Ledger rows retain their voucher and detail references.
Payment totals and allocated amounts are separate fields. An absent payment
record leaves settlement unconfirmed.

For a manufactured shipment lot, the genealogy reader checks the MES work
reference, source-qualified output lot, item and completed quantity against
the ERP manufacture posting and inward batch bundle. It returns the MES and
PLM versions used in the join. Conflicting input observations at the same
MES timestamp leave the genealogy unresolved. The quantity in a partial
shipment can be smaller than the quantity produced.

Shipment genealogy also reconciles every delivery row to exactly one latest
Stock Ledger Entry from the same ERP source. It requires a submitted,
uncancelled ledger row, exact delivery and row IDs, item, stock UOM, warehouse
and bundle, and an outward actual quantity equal to all outward bundle entries.
The existing delivery stock-quantity comparison remains separate. Multiple lots
in one bundle are summed; missing, conflicting or duplicate evidence is
unresolved. The result identifies the ledger row and issued stock quantity.
Older archives without ledger rows or the projected ledger docstatus remain
unresolved: preserve them and rescan into a private archive using the Warehouse
reader. Existing collector settings created before Stock Ledger Entry was added
must be updated to include that entity and its Warehouse reader. This is
observed latest-state analytics, not an as-of or operational authorization.

Shipment publication links require the submitted Work Order's actual ERP BOM and
pinned original MES production BOM to match both PLM receiver receipts, along
with product, approval, effectivity and receipt dates. The PLM source pin and
MES completion source scope the native identities. For commissioned receivers,
the reader compares each marker on the PLM receipt and work pin with the
marker observed on the submitted ERP BOM or MES completion. Legacy receipts
support a content and BOM match. Their configured connections supply the
instance identity. Database clones can share markers. The reader also checks
available ERP company evidence.
Earlier observations without both receipts do not negate a later complete
observation, while conflicting complete observations remain unresolved.
For legacy work orders without `erp_released_on`, the Work Order creation date
is used as a conservative cutoff: it can reject a valid later release and does
not prove the actual submission date or ordering within a day. PLM dates have
only day precision. No inference from a planned start or simulated MES
completion timestamp establishes release time.

For a submitted delivery, the genealogy result includes `invoice_lines`. Each
entry identifies the delivery row and its matching ERP invoice row. Payments
belong to the whole invoice. An invoice spanning multiple deliveries can show
the same allocation on several lines. Count each payment allocation once when
summing cash. A bank statement appears only when the archived native voucher
and a single reconciled statement agree on the allocation, clearance date,
currency and amount. Older delivery snapshots missing currency require a new
scoped scan to verify the invoice row.

Use `genealogy --source ERP_INSTANCE --receipt PURCHASE_RECEIPT` to follow
incoming material from the purchase order line through an inward batch bundle
and its active stock ledger entry. The result includes stock quantity and unit
for each lot, linked supplier invoice lines, and observed invoice allocations.
A receipt can be traced while its invoice remains unpaid. For a multi-line
invoice, the same allocation may appear alongside several receipt rows.

The recurring MES scan archives each newly commissioned Odoo staging receipt under
its MES source and native stock move ID. The receipt identifies the original
manufacturing order, ERP work reference, input lot, quantity, unit and destination.
Registered-family scans use the manufacturing order's output product and company
to select receipts, including those posted before production finishes. On the
MES site, upgrade the Norvale addon to 18.0.1.5.0 before commissioning the staging writer and analytics reader.
Earlier unlinked inventory adjustments have no independently observable order
association and remain outside this projection.

Shipment traces expose the submitted customer order and its exact child row in
`sales_order_lines`. The lookup uses the delivery's ERP source, parent and
child IDs, customer, company, item, stock unit and business dates. Unmatched
delivery rows appear in `unresolved_order_lines`. The ordinary verifier checks
order linkage separately from physical lot genealogy.

The ordinary acceptance report also lists missing, unexpected and changed
outbound deliveries under `shipment_archive_coverage`. It compares the
warehouse account’s submitted deliveries with the latest snapshot from that ERP,
including native versions, customer, posting date, quantities and child-row links.
A projection missing one of those native values needs a new source read.
