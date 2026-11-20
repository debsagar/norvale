# Company logic architecture for Norvale

**Implementation status, 2026-11-19.** The ordinary company generator connects operational ERPNext to InvenTree for product definitions, Odoo for manufacturing, a dedicated ERPNext site for quality, and Delta for analytical history. Isolated native runs exercised the cross-application handoffs. The current integrated revision still needs a full live qualification, including completion of a native backorder after restart. The original 120-day dataset remains frozen. [Recurring deployment and acceptance](../integrations/company_run/README.md)

## Purpose and domain

Norvale is a synthetic consumer-care manufacturer. Its existing catalogue includes shampoo and body lotion (`walkthrough/engine.py`). We need believable work to create records in real applications so an extraction harness can reconstruct relationships, decisions and history. We are extending the existing engine, not building a second company simulator.

The working domain is batch manufacture followed by filling and packing. Configured runs can schedule lab turnaround, pin an approved revision, and link an intermediate bulk lot to the lots used for packing. The bulk path supports one intermediate stage. Recipes, assay limits, staffing and durations are explicit simulation inputs. [Production workflow](../tests/test_company_bulk.py)

### Domain evidence and its design implications

- ISA-95 distinguishes business planning/logistics from manufacturing operations; operations include production, quality, inventory movement and maintenance. Use these responsibility boundaries without assuming each function requires a separate service. [ISA overview](https://www.isa.org/standards-and-publications/isa-standards/isa-95-standard), [OPC Foundation summary](https://reference.opcfoundation.org/specs/OPC-10030/4.2.3).
- FDA's cosmetics inspection checklist describes material controls, batch production records, testing and finished-product control status. For this design, record what was consumed, what was measured and the disposition separately. A production completion cannot substitute for a quality release. The checklist is domain context, not evidence that Norvale meets a jurisdiction's requirements. [FDA checklist](https://www.fda.gov/cosmetics/cosmetics-guidance-documents/good-manufacturing-practice-gmp-guidelinesinspection-checklist-cosmetics).
- GS1's traceability standard describes retaining input/output batch links during transformation. Preserve actual source-lot consumption and output lots, including splits and merges. [GS1 standard](https://www.gs1.org/sites/default/files/docs/traceability/GS1_Global_Traceability_Standard_i2.pdf) (search-index extract reviewed; direct PDF returned 403).
- ERPNext already provides work orders, stock records and quality inspections. Its documented inspection gates are useful primitives, but a completed inspection is not proof of our proposed cross-system release contract. Verify actual installed-version behavior. [Manufacturing guide](https://frappe.io/erpnext/erp-guide/manufacturing), [Quality Inspection](https://docs.frappe.io/erpnext/user/manual/en/quality-inspection) (search-index extract reviewed; direct fetch timed out).
- Delta Lake provides versioned analytical tables. It is a storage component, not the company's operational decision-maker. [Delta Lake documentation](https://docs.delta.io/).

These sources inform the design. The transitions, ownership choices and acceptance checks below are Norvale proposals, not flows prescribed verbatim by those sources. An earlier APQC catalogue approach was retired because categorizing work did not establish executable handoffs or native schemas; see the original project's `attic/2026-10-29-apqc-ontology-skeleton/README.md`.

## Architecture: five responsibilities within the existing loop

1. **Physical world — B/C.** The existing clock advances supplier arrivals, equipment availability, processing and test turnaround. Physical outcomes depend on inputs and modeled conditions. Private causal state stays private; employees receive observations only after measurement or communication.
2. **Company behavior — D/E/F/G/H.** Roles inspect permitted records and their pending work. Known authorized procedures execute deterministically. Unfamiliar tradeoffs go through JEV to role-scoped judgment. Each case has an owner, evidence, next action, due date and escalation condition. A model cannot fabricate measurements or waive an enforced constraint.
3. **Business enforcement — I.** Check authority, quantities, released lots, recipe revision, equipment availability and current record version immediately before acting. Native application permissions/workflows enforce what they can; the engine enforces cross-system prerequisites. Missing authoritative information leaves dependent work pending.
4. **Application actions — J/K/L.** Small product-specific clients perform real API operations, read back their effects and retain failures. The engine persists unfinished handoffs and resumes them. There is no distributed all-or-nothing transaction across vendors.
5. **Analytical evidence — K/D.** Ingest observable application records into the lake. Preserve source identity and history; derive joins with provenance. Operational actions read authoritative application state. Role-visible reports may come from the lake with freshness indicated.

The same scheduler drives all five. Start with existing phase methods and persisted pending work. An event broker, workflow platform, general rules language and always-running agent per employee are unnecessary for the first implementation.

## Record ownership

Ownership is per fact. Two applications may hold references to the same business event without both being permitted to originate that fact.

| System | Authoritative facts | Incoming evidence and outgoing handoff |
| --- | --- | --- |
| ERPNext | Orders, purchase commitments, stock ledger, invoices, payments | Demand creates planned work; confirmed consumption/output creates ledger entries; released stock permits shipment |
| PLM, InvenTree | Approved formulation, packaging specification, revision and effectivity | Approved change publishes a version to ERP/MES; acknowledgement is required before use |
| MES, Odoo Community Manufacturing and Inventory | Dispatch, execution steps, actual labor, consumption, output and genealogy | ERP work request plus approved revision; completion report to ERP and sample request to quality |
| QMS, dedicated ERPNext quality site | Nonconformance, investigation, disposition, release/revocation and corrective action | Test evidence and affected lots; authorized disposition updates ERP eligibility and MES instructions |
| Laboratory function | Samples, methods, measured values and test completion | Initially use the chosen QMS's native capability if suitable; select a separate LIMS only when needed |
| Gitea maintenance today | Issue and repair evidence | Equipment fault opens work; verified restart permits affected equipment to return to service; a closed ticket alone is insufficient |
| CRM, future | Opportunity, customer interaction and complaint intake | Accepted demand goes to ERP; complaint references shipped lots and becomes a QMS case |
| Email | Communication and notification evidence | Carries references and requests; email approval must be implemented in the authoritative workflow |
| Lakehouse, local Delta tables | Ingestion metadata and derived analytical datasets | Copies source records and lineage; does not originate stock, quality disposition or production completion |

No duplicate MES and ERP production writers: MES reports execution; ERP posts the corresponding accounting/stock effect once. If ERP Job Cards remain, identify them as synchronized execution summaries. Product qualification must prove native entities, open-source API availability, authentication/roles, version support and reproducible deployment before an adapter is designed against them.

## Business workflows

### 1. Demand → plan → procure

A customer order creates demand, not automatic production. Planning considers released stock, existing allocations, open production, supplier dates and available equipment. It either allocates stock, releases work or records a shortage with an owner and next review date. Buying raises purchase commitments under delegated authority; exceptions such as substitutions or expedited cost require scoped decisions. Receipt records arrival into quarantine. Testing and authorized disposition make the material usable or rejected.

The ordinary replenishment phase reviews submitted ERPNext purchase lines against the buyer's released stock, observed usage and planner forecast, and dated inbound within supplier lead time. For an overdue line with an open balance and insufficient coverage, SCM assigns a PO-linked ToDo to the buyer with the line, balance and next review date. The line's Expected Delivery Date takes precedence when recorded. Otherwise the review uses Required By, with the header schedule as a fallback. Each daily review rechecks the native line and refreshes or closes the task as coverage, dates, receipt quantities or order status change. A later shortage opens a new dated task. Substitution and expedite spend require separate SCM authorization.

A supplier invoice may exist before quality release; do not universally equate quality acceptance with invoice creation or payment authorization. Those depend on the configured purchasing terms.

### 2. Approved definition → manufacture → inspect

PLM publishes an approved revision and effectivity. Planning pins the revision when releasing a work order. MES starts only when that revision is acknowledged, input lots are usable, the needed equipment is available and capacity is allocated. Production records actual consumption, output, scrap and observations. A completion report creates ERP output into hold stock and requests testing; it does not authorize shipment.

Before submitting a manufacture Stock Entry, and again when recovering its submitted
readback, the controller compares the MES completion ID and version, dispatch,
revision, source and output lot with the intended posting. A mismatch keeps
the handoff pending and prevents lab intake for that output. Planning uses
released batch quantities after subtracting inputs still reserved for open work.

Record units explicitly: bulk mass, packaged units and process losses cannot be equated by a generic quantity sum. A packaging run links the bulk batch and packaging lots to its finished lots. A partially completed run reports only completed quantities; remaining work stays open.

Company acceptance checks every observed MES completion for submitted ERP work, including work that has never shipped. It matches the posted MES version and its native input move lines to ERP manufacture rows by item and lot. Each ERP raw row must have its own submitted outward batch bundle and one active stock ledger issue for the same voucher detail, warehouse, item and stock unit. Split allocations are checked lot by lot and each partial completion accounts only for its own movements. The ordinary handoff supports kilograms (`kg` in Odoo, `Kg` in ERPNext) and individual counts (`Units` and `Nos`) at the same scale. The check retains both unit names, the source-scoped MES unit ID and each system's quantity. Unknown units, inconsistent native unit IDs and archives missing submitted ledger evidence leave the reconciliation unresolved.

### 3. Quality exception → disposition → business response

A failed or missing test keeps the affected lot on hold. Quality receives the measured result, specification revision and genealogy, opens a nonconformance and investigates. Disposition can reject, authorize a specific rework, or release when the applicable criteria and authority permit it. Rework creates additional execution and retest records; it never rewrites the original failure. Corrective action addresses recurrence and requires evidence of effectiveness before closure.

For a newly commissioned primary laboratory, the sample writer creates a draft QMS reference linked to the submitted ERP receipt or manufacture row. The analyst records the measurement and QA checks its lot, sampling date and value before disposition. A separate scheduled reader can follow a failed inspection to that ERP row while the lot is still held. Existing sample references retain their historical owner. [Sample writer commissioning and permissions](../integrations/company_run/README.md#primary-qms-sample-references)

The recurring physical rework path uses a specific QMS authorization that names the failed ERP batch and salvage quantity. It consumes the full held scope through a new MES order and native scrap, then posts a new ERP batch into hold. The controller opens an unmeasured native QMS retest linked to the original failure and the completed MES order. See [recurring authority setup and limits](COMPANY-ERP-CONTRACT.md#physical-rework).

Planning sees the hold and revises allocations or dates. Sales communicates the impact. Quality authority remains separate from shipment urgency. A new revision does not automatically release a batch made under an old revision.

### 4. Released stock → ship → settle → feedback

Warehouse allocates specific eligible lots and rechecks disposition at dispatch. ERP records delivery and billing; finance handles settlement according to terms and observed payment. Partial shipment leaves outstanding demand. A complaint links customer, delivery and lot. Quality can trace upstream inputs and downstream shipments, contain remaining stock and open a withdrawal/recall assessment. For goods already shipped, a QMS approver records a case-scoped withdrawal against the revoked release pinned on the dispatched lot. The recurring feedback command accepts a warehouse observation with the source Delivery Note, line, lot, quantity, arrival date and stable receipt reference. ERPNext posts a linked return Delivery Note into Quarantine and the engine verifies the inward batch bundle and stock ledger row. Partial receipts may be posted separately up to the original quantity. The original delivery and invoice remain posted. Customer credits and refunds require separate authorization. The filtered lake links the return to the shipment and QMS case.

In A–L, warehouse arrival evidence comes into D/E and QMS approval sits in F–H. I/J runs the existing locked feedback operation and posts the native return. K/L verifies and retains the return, quantity, batch, quarantine ledger and source links.

### 5. Equipment fault → repair → restart

The integrated recurring engine pins the native MES work center on each request and budgets capacity per center. Planning and dispatch read the acknowledged MES routing and current availability with the operator credential. The acknowledged BOM defines compatibility. The acceptance fixture accepts an `equipment_by_item` map in its private `company-sources.json` before publishing definitions for distinct centers.

The service phase adopts open MES faults on managed centers and creates a native fault when a center crosses its usage limit. It checkpoints the fault and its Gitea issue.

Ordinary company source provisioning installs separate technician, tester, verifier, and read-only Gitea identities scoped to issues in the private operations repository. For an existing dedicated native run, opt in with `PYTHONPATH=.:native python3 -m integrations.company_run.provision_maintenance /path/to/business`. The command reuses the existing Gitea issue and saved credentials on retry, and preserves any configured verifier and reader for manual cases.

For a recognized usage fault, the technician observes guide vibration and performs service on a later shift. Odoo records the timed intervention and its modeled adjustment while the center stays blocked. The tester reads that service and measures modeled no-load vibration on a separate shift. A reading above 2.50 mm/s RMS leaves the center blocked and queues corrective alignment and another test. On a later shift the verifier reads the original diagnosis, Odoo service and passing test before posting a comment linking all three. Each participant has capacity for one maintenance task per shift. Modeled work occupies 25 minutes for diagnosis, 60 for service, 20 for testing and 15 for verification. The recurring controller rechecks the pinned native evidence before restart.

External, legacy and unexplained faults still require an explicitly authorized verifier to assess their own evidence. The engine reads verification using the private issue-scoped `maintenance_ticket_reader` token. The verifier uses the existing comment format.
```text
Verified restart: <exact MES fault URL from the issue>
Diagnosis: <observed cause>
Repair: <work performed>
Test: <verification procedure and observed result>
Result: PASS
```

Configure a separate MES `maintenance_mes` manufacturing manager credential alongside the `operator` reader in `env/company-sources.json`. Upgrade the `norvale_mes` addon on existing MES databases before enabling the workflow. An older integrated Filler ticket with an open checkpoint is carried into a linked native fault for the configured legacy `center`.

MES records the running operation on the fault. Restart restores equipment state independently of the interrupted operation, which stays blocked from completion. QA can reject the remaining WIP through a submitted QMS `Lot Rejected` inspection tied to a nonconformance. Use the WIP identifier in the maintenance issue as the QMS batch, `<batch>:interrupted` as its unique operation key, and include the MES completion identity in the decision rationale. The inspection must match the item, quantity, UOM and inspection specification pinned on the released work. Its report date must fall between work release and the business date on which QA evidence is accepted for cancellation. The controller saves that date before requesting MES cancellation so a retry uses the same evidence cutoff. The laboratory provides the inspection reference and measurement. The engine validates the current decision using the QMS reader and configured approver, then cancels the matching MES remainder when it has no posted movements. Previously posted output follows its own lot disposition. Rejected WIP cannot produce an ERP lot or count as fulfilled demand. Interrupted work with posted movements, rework, or insufficient evidence remains pending for manual reconciliation.

This path uses the existing company clock and native records in B–E. It applies the routine maintenance rule and preserves separate verifier and QA authority in F–H, checks equipment and disposition in I, and persists application identities and checkpoints in J–L.

## State transitions and guards

These are conceptual states to map onto the selected products' native workflows, not invented replacement database tables.

| Object | Normal progression | Alternative or blocking behavior |
| --- | --- | --- |
| Definition revision | Draft → reviewed → approved → effective → superseded | Rejection; future effectivity; receiving system has not acknowledged revision |
| Material/finished lot | Quarantine → testing → released | Failed/missing evidence → hold; rejected → return/scrap; rework → new execution and testing; released → hold on new evidence |
| Work | Planned → authorized → dispatched → running → completed | Material shortage, stale revision, equipment fault; partial completion; cancel only remaining work |
| Nonconformance | Open → contained → investigated → dispositioned → closed | Rework/retest pending; linked corrective-action effectiveness can remain open separately |
| Order | Accepted → allocated → partially/fully shipped → settled | Credit hold, shortage, changed promise, cancellation, return |

Availability, quality status, allocation and physical location are separate dimensions. Moving stock between warehouses alone cannot express every disposition or revoke an already allocated shipment. Release should identify lot, applicable quantity/scope, decision revision, approver and evidence; revocation invalidates outstanding eligibility.

## How each handoff executes

For each transition persist a small operation record in the existing private checkpoint: triggering source reference/version, role, intended target action, retry identity, target ID when known, progress, last error and next attempt. These are execution bookkeeping, not employee knowledge or exported business truth.

1. Read role-permitted current source records and identify the business reason to act.
2. Route the decision; retain its evidence and authority. Routine approved work need not call a model.
3. Recheck prerequisites and reserve resources using native facilities where available.
4. Submit through the destination API with a stable retry identity or a discoverable external reference.
5. Read back and verify quantity, status, revision and links. Persist the destination ID before advancing.
6. Notify the next owner only after confirmed state, or retain a visible pending handoff.

A timeout is an unknown outcome: search/read back before retrying creation. Duplicate delivery of a MES completion must not consume stock twice. If MES succeeded and ERP failed, retain 'execution complete, posting pending', reconcile and block actions needing the missing posting. Do not undo physical production to make software appear consistent. A release recorded in QMS but not applied in ERP leaves shipment blocked until synchronization succeeds.

For competing shipments/holds, native transactional checks are preferred. Where a cross-system race cannot be excluded, serialize release/dispatch operations in the existing single controller and recheck just before dispatch. This has a clear ceiling: adding independent writers requires version/locking support and a defined hold-versus-dispatch ordering rule; do not claim atomicity across systems.

## People and uncertainty

Roles need bounded work queues and calendars, not random actions. Begin with current roles, explicit due dates and a limited number of tasks per shift. Escalate overdue work through observable tasks/messages. Priority tradeoffs include delivery commitments, material risk, cash and equipment downtime; parameters are company policy assumptions, not universal industry constants.

Use randomness for sampled supplier delays, wear, measurement variation and bounded task delays, conditional on modeled causes. Save the seed and private causes. Role decisions use available evidence, not latent defect labels or future outcomes. A failed test can cause an investigation; a random branch must not directly invent a corrective-action success. Maintain material and cash conservation through disruptions.

During ordinary purchase review, an exhausted routing or model transport retry leaves the Purchase Order in draft. The controller records the outage, creates the buyer's dated follow-up, and continues with other orders. The next review reads current evidence. Model case history excludes transport failures.

## Lakehouse contract

Begin with incremental batch ingestion after confirmed readback; streaming is not necessary for the current daily engine. Preserve source system, native entity/ID, source version or content hash, business-effective time, source-recorded time and ingestion time. Keep deletions/cancellations and late corrections rather than silently overwriting history. Real server timestamps and simulated business dates remain distinguishable.

Retain raw observable snapshots per source, then derive typed datasets and business joins. Publish explicit crosslinks such as order reference, work reference, batch/lot identifier, recipe revision and shipment reference. Keep uncertain inferred joins labeled. Lake ingestion retries deduplicate by source identity/version; an ingestion failure does not stop production, but reports expose their stale watermark. Secrets, controller mappings, latent causes and private reference answers remain outside the extractor's inputs. Apply role scope to analytics as well as application reads.

## Map onto the current code

| A–L blocks | Existing code | Proposed change |
| --- | --- | --- |
| A | `Engine.seed`, `recurring_setup`, `systems_v2.identities` | Seed selected applications' users, references, revisions and opening lots |
| B/C | `recurring_events`, `production`, `service`, `arrivals` | Preserve one clock; separate dispatch, elapsed work and completion; equipment-specific availability |
| D/E/F/G/H | `observe`, `choose`, policy and case state | Include scoped quality/revision/work evidence; route investigations and exceptions |
| I/J | `production`, `inspect_release`, `fulfillment`, `write`, `mapped` | Enforce lot/revision guards; call product APIs; reconcile MES facts into ERP once |
| K/L | Readback, `persist`, logs, `systems_v2.export_native` | Preserve pending cross-system steps, source links and failures; export new systems and ingest observable data |

The frozen generator uses nominal passing readings and ERP manufacturing documents. The configured recurring company sends work to Odoo, records inspections and dispositions in a separate QMS, and posts the resulting stock movements in operational ERPNext. New runs select these paths through the ordinary generator. Configured operating runs scan the lake before management and after timed work closes for the day. An independent collector can also scan the same sources. The deployment guide gives the entry points and role setup for each application. [Recurring company deployment](../integrations/company_run/README.md)

## Implementation sequence and acceptance evidence

The retained 12-day coordinator run recorded eight MES completion postings and five shipments. All 20 checks on that snapshot passed, and one additional allocation remained pending after ERP rejected it. That run used deterministic decisions and scripted interruptions. The current verifier also checks posted input lots against ERP ledger issues and checks a withdrawn draft against its exact QMS inspection, version and quantity. For ordinary withdrawals, the verifier follows the changed allocation to the issuing QMS and the ERP draft, then checks both the prior release and the current revocation in the archive. A missing earlier snapshot leaves the ordinary check unverified. Maintenance restart verification reads the pinned MES and ticket source for each completed case. Read the retained results with the verifier version that produced them. [Current verifier](../integrations/company_run/verify.py)

1. **Qualify products and native contracts.** For each selected application demonstrate create/read/update, role denial, native references, export, restart and retry behavior in isolation. Record real endpoint/schema/version details. The MES, QMS and PLM integration runbooks record the selected products and their qualification scopes.
2. **MES + QMS within recurring business work.** Preserve the company's orders, purchasing, maintenance and finance; add execution and release handoffs. Prove a failed lot stays unshippable, passing lots can ship and backlog persists. Single fixtures are regression checks, not a replacement company run.
3. **PLM revision flow.** Prove future and superseded revisions cannot silently replace a pinned in-progress batch; approved changes require acknowledgement and effective-date checks.
4. **Lake ingestion.** Prove rerunning ingestion does not duplicate versions; corrections remain visible; joins can trace input lot → execution → output lot → quality disposition → shipment without private mappings.
5. **Broaden operational cases.** Add CRM complaints and laboratory or maintenance software when their workflows require capabilities the selected applications lack.

Required adverse-path checks: replay a completion after timeout; interrupt between QMS release and ERP update; hold an allocated lot before dispatch; exhaust one input lot; partially complete a job; rework and retest without erasing failure; fail a model/API call without auto-approval; restart the controller without duplicate writes; prove unauthorized users cannot release stock. Check both positive and negative outcomes through application readbacks and ledger/genealogy reconciliation.

Use separate runs for changed behavior. Keep the existing offline suite and frozen-data hash check. A diagram, successful API response or one happy-path transaction is not evidence that the company logic works.

## Ongoing changes to ordinary product definitions

In an ordinary `--company-operating` run, the production phase commissions
the initial definition using separate native accounts. Later decisions are
explicit operator actions between generator invocations. Use a submitted
ERPNext BOM visible to the run's planner as proposal evidence. It must match
the company, output item and unit, and existing component mapping. This path
supports quantity changes to those components. It records the submitted BOM's
native ID and version and the effective PLM revision the proposal changes.

From the release directory, using the relevant actor's InvenTree API token
in `PLM_DECISION_TOKEN` for each decision command:

```sh
python3 -m integrations.company_run.definition_change RUN propose ITEM B BOM-ID
python3 -m integrations.company_run.definition_change RUN review PART-ID ENGINEERING-EVIDENCE-REF
python3 -m integrations.company_run.definition_change RUN approve PART-ID DECISION-EVIDENCE-REF --effective YYYY-MM-DD
# Alternatively, under the approver's credential:
python3 -m integrations.company_run.definition_change RUN reject PART-ID DECISION-EVIDENCE-REF
```

The first command reads ERPNext with the configured planner token and writes
the proposal through the PLM publisher. Review and decision evidence should
be shareable source references. InvenTree authenticates the respective
Engineering and Approver accounts and enforces distinct groups, binds the
decision to the reviewed digest, and locks approved assemblies. Existing
ordinary deployments need the updated InvenTree Compose service with
`NORVALE_PLM_CHANGE_GUARD=1` and the two scoped groups and accounts provisioned
before using these commands. The isolated acceptance fixture retains its
original commissioning permissions.

In the next production phase the existing bridge reads approved changes from
InvenTree, reconciles the ERPNext and Odoo BOMs, and saves exact receiver
receipts. New work uses the prior revision until the successor's effective
date. On or after that date it waits if either receipt is missing. Existing
work keeps its original revision and receiver IDs. The published-definition
lake view includes only dated approvals with allow-listed proposal references
and receipts. The role-visible native BOM view continues to expose assemblies
and component quantities without decision metadata.

The change uses the planner's native evidence in D/E, explicit Engineering
and Approver actions in F–I, existing native writes and readback in J/K, and
the recurring checkpoint and lake projection in L. Serialize decision
commands and generator writes to the same store. The InvenTree source guard
is tied to the pinned version and uses its normal Part, BOM and metadata
endpoints. Database and Django administrative access remain privileged.
