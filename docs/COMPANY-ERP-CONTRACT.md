# Recurring ERP company authority contract

Enable on a fresh run with `python generate.py --run PATH --provision --company-authority package.module:factory`.
The saved `config.json` selects the integration on every restart. `factory(Path(run))` returns the authority object. Programmatic callers can pass the same interface as `Engine(run, company_authority=adapter)` or `initialize(run, config, company_authority=adapter)` with the configuration key present. A separate run/site is required because serial-and-batch tracking starts at Item creation.

The authority composes authenticated PLM, MES and QMS adapters. Use role-scoped credentials and read the authoritative system each time. The role argument is the requested controller identity. The adapter is responsible for checking native identity, approval authority, effectivity, the accessible evidence and the latest version. Return only JSON-native values. Return `None` for an authoritative absence on singular reads, and raise `native.company_erp.Pending` (or a transport error) when authority is unavailable or ambiguous. Use ISO business dates and native, nonempty string IDs and versions. A repeated successful read of a version must return the same payload.

| Method | Contract |
| --- | --- |
| `definition(item, *, as_of, role)` | PLM read as Planner. Return `{item, erp_bom, id, version, approved: true, acknowledged: true, approval_id, erp_ack_id, mes_ack_id, effective_from, effective_until}`. End date is exclusive and can be null. Both MES and ERP must have acknowledged that exact approved revision. Use the ERP receiver BOM ID as `erp_bom`. An adapter can map InvenTree publication `native_id` and `source_version`, its approval evidence, and the two bound receiver IDs to these fields. ERP verifies the returned BOM directly and can use a different approved BOM on new work. The same record is pinned into the Work Order and MES request. |
| `disposition(lot, *, as_of, role)` | Current QMS read for QA, Planner or Warehouse. Lot includes `batch, item, qty, uom, kind, source_type, source_id, source_row`. Incoming lots include the native purchase order and row IDs. MES output also includes `completion_id, completion_version, mes_lot_id, revision, work_order`. Return `{batch, item, uom, id, version, approver, evidence, authorized: true, status, qty}`. `evidence` identifies the native decision evidence. Status is `released`, `hold`, `rejected` or `rework`. Quantity is the cumulative authorized cap in ERP stock UOM on the original lot, inclusive of units already consumed or shipped. Other statuses have zero cap. Opening stock and purchase receipts also require QMS release. For finished lots, verify the QMS specification scope against the pinned PLM revision. The current QMS adapter can authorize full-lot releases via `release(batch, revision, quantity, uom)`. Sub-lot release caps require a native QMS read that authorizes the smaller scope. Put observations and actual approval in the source workflow. |
| `dispatch_lease(request, *, outcome, role)` | Order a warehouse Delivery Note against all its QMS lots in one native transaction. The request has immutable `id, delivery, scopes`. Each scope has the original `lot`, pinned `disposition`, and `dispatch_qty`. Return `{id, delivery, state, submit_permitted}`. Only the first committed arm response can set `submit_permitted` to true. Bind the Warehouse operation to a dedicated QMS dispatcher credential. See the native mapping and recovery below. |
| `find_dispatch(key, *, role)` | MES read as Production by immutable external key. Return the same acknowledgement as `dispatch`, or null only when absence is authoritative. Transport uncertainty must raise. |
| `dispatch(request, *, role)` | Idempotent MES creation under `request.key`. Verify current execution prerequisites and reject repeats with changed payload. Read back and return `{id, version, request}` with the persisted request. Request includes `key, work_order, sales_order, sales_order_item, item, uom, qty, revision, equipment, as_of, inputs`. Each input has `batch, item, uom, qty, disposition`. |
| `completions(dispatch, *, as_of, role)` | MES read as Production given acknowledged `{id, version}`. Return an iterable of immutable incremental completions, including previously returned reports when useful. Each is `{id, version, dispatch_id, work_order, revision, authorized: true, inputs, output}`. Each input is actual `{batch, item, qty, uom}`. Output is `{lot_id, item, qty, uom}`. Use a globally unique completion ID and unique output lot per report. Product quantities are whole units. Reports are actual deltas and never change under the original ID. |

QMS lot records can be discovered or linked using the lot provenance presented to `disposition`. The controller opens a draft inspection request for physical rework. The lab supplies measurements and Quality makes the disposition. MES execution and PLM approval belong to their adapters. Division of a completion into multiple reports requires real incremental consumption and a distinct lot per report. Reject and rework keep stock quarantined. Inputs already issued to a dispatched or unconfirmed MES request stay available for ERP to post actual consumption, while current QMS controls eligibility for new work. Bulk-to-pack orchestration, manual resolution of stale pinned work and held/rejected work commitments and equipment routing beyond the current single filler require additional workflows.

The existing recurring controller serializes its phases with `execution.lock`. Stock Entry, Work Order and Delivery Note use unique operation markers for recovery. Batch IDs derive from receipt row or MES output identity. ERPNext 15.124.1 builds a serial-and-batch bundle for each ledger row from explicit batch fields. Readback verifies the native stock ledger and bundle, voucher, child ID, lot, sign, quantity and warehouse before advancing. Transfers verify both sides. Work Orders carry native sales and BOM links, and native fields carry pinned source versions on stock postings and deliveries. Finance uses confirmed deliveries and purchase receipts. Sales Order delivered quantity preserves backlog.

Provision quantity precision on the dedicated ERPNext site before opening stock. The engine verifies the effective Frappe float default at eight places. Frappe synchronizes changed System Settings fields on document save, so a direct singleton write can leave calculation precision at three places even when the settings form displays eight. Odoo requires both its Product Unit of Measure decimal precision and the mass UOM rounding to be set. A manufacture draft must match the MES stock-unit quantities before submission, and submitted quantities are checked again against ledger rows and batch bundles.

When routing work across MES instances, include `mes_source` in the pinned
revision and `source` in both the dispatch acknowledgement and its completion
reports. The controller validates this identity, scopes completion replay and
output-lot registration to the instance, and retains the source in the native
ERP posting. Existing single-source records with globally qualified completion
IDs retain their previous operation keys.

The company authority pins a finished-lot inspection specification in the
Work Order definition. It binds the assay method version, product, source
PLM, part and publication fingerprint. The lab writes this value into its
inspection. Warehouse checks that same value on the submitted release and
rejects inspections reported after its business date. A finished lot whose
older Work Order has no inspection specification needs quality reconciliation
before shipment. Incoming lots use their material test specification.

## Warehouse dispatch ordering

Blocks I–L use a native QMS lease for outbound Delivery Notes. The controller checks the draft, allocation and ERP stock, checkpoints the exact lease request, and asks QMS to acquire every lot in that request. QMS takes Batch row locks in sorted order and compares each latest inspection to the pinned release version, approver, item, specification, quantity and UOM. A newer draft or an earlier revocation prevents acquisition. The same transaction records the lease and places a pointer on each Batch. Native Quality Inspection mutations, including release revocation and creation of a newer inspection, check the same Batch row locks. An active or armed lease blocks those mutations on its lots. An inspection writer can retry after the lease finishes.

The native receipt grants a single submit permission when it moves from `active` to `armed`. The controller then checkpoints `submit_started` and sends the ERP submit. A pre-arm withdrawal or denial leaves a terminal receipt so a delayed retry cannot acquire the same operation later. QMS rejects dispatcher withdrawal after arming. A verified submitted Delivery Note, including matching lot bundles and stock ledger entries, permits the `submitted` transition. This transaction retains the QMS receipt and clears the lot pointers. The Delivery Note quality release field links the lease ID to the original allocations. The controller checkpoints the terminal receipt before recognizing physical departure and allowing invoicing.

The integrated authority uses the finished lot's pinned `inspection_spec` and original QMS source. Configure `qms_dispatcher` with dedicated credentials for the same site used by `qms_reader`. A production profile for a separate QMS site supplies its own `qms_dispatcher` connection with credential environment references. A profile on the same site can use the company's dispatcher. Every lot in a Delivery Note must use the same dispatcher and site.

Use the native Quality Inspection name, `modified` and `modified_by` for disposition `id`, `version` and `approver`. Other authorities must translate the pinned scope consistently and sort by native QMS Batch ID before calling `QMS.dispatch_lease`. The native endpoint takes the same top-level ID and Delivery Note name with scopes containing `batch, item, inspection, version, approver, revision, qty, uom, dispatch_qty`. The dedicated adapter supports the full inspected lot quantity as the release cap. The controller checks the remaining ERP quantity and its cumulative allocation separately.

## Recovery and limits

- **Lost QMS response before ERP submit.** Read the native receipt using the same request. A lost acquisition reply can be retried with a fresh draft check. A lost arm reply leaves an armed receipt that cannot grant another submit permission. Reconcile it as an unknown attempt. If eligibility changes before arming, the controller can record a pre-submit withdrawal. A withdrawn or denied receipt fences delayed acquisitions for that ID.
- **Lost ERP response or interrupted controller.** Read the exact Delivery Note recovered by the native operation marker. When it is submitted and matches the pinned draft and ledger, retry finalization idempotently. A draft, missing document, cancellation or incomplete ledger keeps the operation pending. Resolve unknown legacy submits before migration. The controller sends no second submit after recording `submit_started`, and time passing never releases a lease.
- **Operator recovery.** For a terminally unsubmitted delivery, stop the controller, reconcile ERP logs and the native operation, and establish that any queued or running ERP requests have drained and the draft cannot be submitted later. Use that evidence to authorize an Administrator-only POST to `/api/method/norvale_dispatch_lease` with `{"outcome": "aborted", "lease": <exact pinned native request>}`. The request is in the QMS receipt's `request_json`. If acquisition itself was uncertain, reconstruct the request from the checkpoint using the same scope mapping. Record the ERP evidence with the incident. Restart the controller so it reads the terminal receipt. A submitted delivery belongs on the normal verification and finalization path. If ERP outcome or quiescence remains unknown, retain the lease.
- **Deployment boundary.** Keep the dedicated QMS dispatcher credential with the controller that verifies ERP outcomes. This design assumes a single recurring controller per operational ERP inventory namespace and exclusive control of its ERP drafts and allocations. QMS enforces ordering for ordinary native document and workflow users. Privileged code, migrations, direct database writes and changes to QMS configuration require administrative coordination. QMS revocation may wait behind an unresolved dispatch. The controller reconciles deliveries independently. While a delivery is unresolved, its quantities are reserved against the native Sales Order line and its batches are excluded from new allocations because QMS grants one lease per batch. Eligible demand can still ship from other batches after current stock and release checks. This guard covers outbound Delivery Notes. MES dispatch keeps its own reconciliation path.

## Ordinary quarantine release ordering

Blocks I–L use the same native Batch lock for ordinary operating-run releases from quarantine to eligible ERP stock. The ERP draft cites the QMS transfer lease in its release remarks. QMS binds the exact current inspection version and lot scope to the ERP source, Stock Entry, stock row, company, warehouses and transferred quantity. The controller checkpoints the request before acquiring and arming it. Provision the updated QMS lease schema and API on the owning site and use the registered ERP source from the ordinary company configuration.

The controller reopens the submitted Stock Entry and reads its active ledger and batch bundles for both legs through the operating QA identity. Only matching evidence is sent to QMS to finalize. The QMS transaction stores that evidence and clears the lot pointer. An armed transfer lease stays locked if its ERP outcome is unknown, including a lost arm reply. A recovered commit permits idempotent finalization and an ambiguous attempt remains pending. An Administrator cannot abort an armed transfer lease through the lease endpoint. This path assumes the controller exclusively submits its ERP drafts and that its dedicated dispatcher credential is trusted to report the independently fetched ERP evidence.

Scheduled Delta scans collect both native records and the stock legs. Archived resume requires the finalized release-transfer receipt from the owning QMS site and its exactly matching ERP evidence. Legacy transfers and outbound dispatch leases provide no such authorization evidence. The confirmation follows the QMS state transition and ERP readback, without comparing native timestamps across servers. Return-to-quarantine moves and the existing physical rework route keep their separate reconciliation rules.

## Physical rework

Provision the patched QMS schema and Odoo addon for a fresh integration. Give a dedicated MES bridge account the **Norvale QMS Rework Bridge** group. For the normal `integrations.company_run.authority:Authority` factory, provision the bridge credentials as `rework_mes` in the private `env/company-sources.json`, alongside `qms_reader`, `qms_approver`, `qms_source`, `lab.analyst`, `lab.analyst_user` and `uoms`. The isolated company provisioner configures these fields. The rework credential must point to the configured company MES deployment. Registered production families can commission a dedicated bridge on their own MES and QMS sites for the supported rework procedure. See the [registered lab configuration](REGISTERED-QMS-LAB.md). ERP, MES and QMS use the same SKU code. The source-qualified MES completion and lot IDs identify the original execution. Before consuming held stock, the bridge rereads that completion and checks its native finish timestamp against the version pinned at ERP lot registration. A changed or missing pin leaves the operation pending. The pinned revision's `inspection_spec` must match the QMS spec revision.

1. The lab records the failed inspection, with `Rework Input Batch` set to the exact ERP lot ID and `Rework Salvage Quantity` set to the intended output quantity in the inspection's stock UOM. The workflow requires a linked nonconformance and a QMS approver's **Authorize Rework** action. The approved failed inspection preserves the original measurements.
2. Add an entry to the recurring run's private `rework-requests.json` array and publish the file atomically. It routes the operation to the normal inspection phase under the run's execution lock. A request can wait for approval. Additional entries can be appended, and accepted requests remain in the checkpoint for reconciliation.

   ```json
   [{"batch":"ERP-LOT-ID","mes_order":123,"qms_batch":"QMS-BATCH-ID","inspection":"MAT-QA-2026-00001"}]
   ```

3. The controller checks both native hold balances, absence of unresolved shipments, current QMS scope and MES source identity. Odoo runs a separate manufacturing order and work order at the original center, consumes the salvage share from hold, records any remainder in native Scrap linked to the order, and puts the new lot into hold. ERP posts a native Repack from the full failed batch to the salvaged batch in hold and verifies signed batch-ledger entries and quantities.
4. A native QMS Quality Inspection draft records the failure ID, MES completion reference and ERP salvage batch. The native QMS reference document stays the original sample's local Stock Entry anchor. ERPNext can update that anchor's inspection pointer while the original inspection remains intact. The lab enters fresh measurements and the approver uses the existing release workflow. ERP eligibility requires the exact linked current released retest. A missing, draft, failed or revoked retest keeps the salvage on hold. The warehouse dispatch lease uses the original batch on the QMS site and the salvage batch on the ERP Delivery Note.

This route supports one rework per original completed lot, a positive salvage quantity, one SKU and stock UOM, one held quantity consumed in full, and the original work center. Loss can be zero. It uses the original pinned specification and has no additional materials, repeat rework, scrap-only disposition, or automatic replacement planning. The MES bridge account and run request file are privileged integration inputs. QMS authority is re-read just before the atomic Odoo call. As in the release path, cross-system changes during that call require externally serialized authority changes for a strict ordering guarantee. A committed MES run is reconciled even if ERP or QMS has yet to record its follow-up.

Run the native regression against isolated projects using the existing provisioners. The QMS runtime supplies the dedicated test ERP company and QMS workflow in one Frappe site.

```sh
runtime=$(mktemp -d)
integrations/qms/provision.sh up "$runtime"
integrations/qms/provision.sh configure "$runtime"
integrations/qms/provision.sh fixture "$runtime"
MES_REWORK_QMS_RUNTIME="$runtime" MES_TEST_SOURCE=nvmes-rework-check MES_PORT=53645 integrations/mes/verify.sh
integrations/qms/provision.sh stop "$runtime"
```

## Invoice settlement

In ordinary operating mode, the recurring bank statement phase runs before
Finance settlement on the company clock. A modeled payer uses the issued EUR
invoice, stable terms and the run seed to schedule transfers. Some customers
pay late, some pay in installments, and some have no payment in the run.
Supplier disbursements are statement withdrawals. The feed imports only
events whose business dates have arrived and submits them under the separate
Bank Feed user to a native company Bank Account linked to its Bank GL account.

Finance looks up submitted statements by bank, company, EUR, counterparty,
native invoice reference, feed owner and observed dates. It maps one Payment
Entry per transfer, checks the current balance before submit, then reconciles
the voucher with ERPNext's bank reconciliation tool and reads back the
allocation, clearance and invoice balance. Partial receipts leave the
invoice open. A missing or excessive receipt leaves the balance unchanged
and the overdue work pending. The unique native operation keys recover
committed writes across retries.

For supplier invoices with multiple installments, each native schedule row
needs its own payment-term name. The bank feed records a withdrawal on the
first eligible business date for that row. Finance checks the hold, due date
and open installment balance before posting. The lake links the observed
bank transfer to the Payment Entry and its named invoice installment.

The fixed fixture and recurring configurations without ordinary operating
mode retain their existing payment behavior and cash-account configuration.

## Revision changes during a release

The submitted ERP Work Order records the release date and the approved PLM
and receiver identities in its definition. On recovery, the controller reads
that order as Planner. The bridge resolves the exact publication and checks
both installed BOMs before dispatch, even when another revision has since
become effective. Earlier native orders use their recorded planned start
date if the release-date field is absent. An unsubmitted proposal whose
recipe is superseded releases its input reservations after the planner
confirms that no native Work Order exists. A conflicting native draft
remains pending for reconciliation.

## Sales handoff after an allocation is withdrawn

When Quality changes a lot's eligibility before shipment, the recurring
controller retains the abandoned draft Delivery Note. Planning reads the
draft and Sales Order, verifies the affected batch and line, and creates a
Sales ToDo recording the quantity at risk, remaining line quantity and
promised date. The sales user reads the task before Sales Operations sends
the internal notice. The checkpoint preserves the original notice across
later deliveries and retries. An unconfirmed mailbox readback leaves the
handoff pending while other fulfillment work can proceed.

### Sales holds and outstanding quantities

Planning rereads the submitted Sales Order line before releasing a Work
Order. A held or closed line, or an insufficient open quantity, retires a
proposal after Planning confirms that no native Work Order exists. An
existing draft stays pending. A submitted Work Order retains its production
authorization. Warehouse rereads the order after acquiring an active QMS
dispatch lease and before arming it. A changed line withdraws the lease
and the Delivery Note stays in draft. ERPNext performs its own checks at
submission if another writer changes the order after that read.

## Receipt-based supplier billing

Finance matches the authorized Purchase Order, submitted Purchase Receipt
and supplier bill by native row ID, supplier, company, currency, item,
unit, quantity and unit price. A mismatch leaves that invoice pending.
The buyer shares read access to the individual order with Finance when the
linked receipt is confirmed and the Finance account lacks access. The
supplier bill records no second stock movement. It may be posted while
the receipt is still in quarantine.

Where a purchase order specifies a payment terms template, the receiver
mapper can omit it. Before creating an invoice, the controller passes
the purchase template to ERPNext and lets ERPNext calculate the dated
schedule. Finance checks the resulting installment portions and records
the submitted invoice's native due date in the recurring checkpoint.
