# Dedicated Norvale QMS

This integration runs a fresh ERPNext v15.124.1 site as the quality authority. The operational ERP and the QMS use different sites, MariaDB volumes, networks and users. The local stack exposes an API on a Docker-assigned loopback port. It runs Bench's development HTTP server for isolated integration and proof.

## Provision and verify

From the release root, with Docker Compose, Python 3, curl, OpenSSL and flock:

```bash
qms=integrations/qms
runtime=$(mktemp -d)
python3 -m unittest discover -s "$qms" -p test_adapter.py
"$qms/provision.sh" up "$runtime"
"$qms/provision.sh" configure "$runtime"
"$qms/provision.sh" restart "$runtime"
"$qms/provision.sh" fixture "$runtime"
python3 "$qms/prove.py" "$runtime" "$runtime/proof"
"$qms/provision.sh" fixture "$runtime"
python3 "$qms/prove_dispatch.py" "$runtime" "$runtime/dispatch-proof"
"$qms/provision.sh" stop "$runtime"
```

`up` follows the Bench install sequence in `norvale/environment/stack.py` and uses cached images only. Set `QMS_IMAGE=nv11-erp:15.124.1` on the first invocation to select the other cached image. Use a fresh runtime directory for each independent instance. Reuse a runtime directory to restart its own instance. It holds local credentials and operational files and should stay outside the release tree. The fixture command creates a new synthetic batch and a draft Stock Entry reference each time. The proof command consumes one fresh fixture and restarts its dedicated DB, Redis and QMS containers. A failed Bench install leaves diagnostics in the private `new-site.log`. A failed fixture/proof is repeatable using a new fixture in the same site.

## Native records and API

- **Batch** is the lot identity. Register the local Item and Batch and provide a local native inspection source, normally a Stock Entry and its item row. Keep the enterprise source's identity in the integration handoff. The local draft fixture Stock Entry gives the inspection a native document reference without posting inventory.
- **Quality Inspection** carries the measurement rows, method parameter, native `batch_no`, reference and computed Accepted/Rejected result. Each test gets its own inspection and submitted reading history. Custom fields record the specification revision, full lot quantity and UOM, rationale and retest link.
- **Non Conformance** links the procedure, Batch and failed Quality Inspection. **Quality Action** links the case and Batch and uses native corrective/preventive type, resolutions and derived status. Closing a corrective action requires separate evidence of effectiveness.
- **Norvale Lot Disposition** is a native Frappe Workflow on Quality Inspection. The analyst records and submits evidence. An approver can reject a failed lot or authorize rework once the failed inspection has a linked case. Release requires the current submitted, accepted, numeric inspection and the declared full scope. After a submitted failure it also requires a retest linked to the latest failed inspection and matching its authorized rework scope. Only the approver can release or revoke. A revoked inspection remains submitted and retains its measurements. A fresh sample starts a new decision.

The client uses native `/api/resource/{doctype}` list/create/read/update and `/api/method/frappe.model.workflow.apply_workflow`. The Analyst, Approver and read-only Reader authenticate separately. The write adapter checks the native response by an independent GET. Creation replays reconcile through a unique native custom operation field, and repeated identical workflow actions reconcile against the target state. Installation uses the Frappe setup wizard and Document APIs for custom fields, permissions, workflow and identities. [Frappe REST API](https://docs.frappe.io/framework/user/en/api/rest), [ERPNext Quality Inspection](https://docs.frappe.io/erpnext/quality-inspection), [ERPNext Non Conformance](https://docs.frappe.io/erpnext/non-conformance), [ERPNext Workflows](https://docs.frappe.io/erpnext/workflows).

## Engine boundary

- **A / D** provision identities and native references. Take measurement inputs and specification limits from the observed sample and applicable revision. The caller supplies those limits. Approval reviews the recorded scope and measured evidence.
- **I** call `QMS.release(batch, revision, quantity, uom)` to read eligibility. It returns the current matching submitted release or `None`. Treat request errors as an unresolved hold. A newer inspection on the same lot, even a draft, supersedes the earlier eligibility check.
- **J / K / L** use `QMS.create`, `QMS.update` and `QMS.transition` with the appropriate actor. Persist the operation identity and confirmed native ID in the engine queue. Resume unknown outcomes with the same operation and evidence. `QMS.export()` reads the native documents and child rows through the Reader account. Run it while writers are idle when an internally consistent snapshot is required.

- **I / J / K / L** use the Warehouse controller's dedicated Dispatcher credential for the native dispatch lease. Inspection writers use their existing credentials and workflows. The lease survives DB and server restarts. See the [recurring dispatch contract](../../docs/COMPANY-ERP-CONTRACT.md#warehouse-dispatch-ordering) for the native scope mapping and operator recovery.

On an existing dedicated QMS instance, pause controller and QMS writers, reconcile old submits, then run `provision.sh lease-only <runtime>`. This installs the dispatch DocType, fields and server scripts and writes the private Dispatcher credential to `dispatcher.json`. Restart QMS and wire that credential into the recurring authority before enabling new deliveries. Fresh `configure` also includes the Dispatcher in `users.json`.

The dispatch proof exercises native workflow and API writers, lot isolation, all-or-nothing acquisition, and lease persistence across restart using synthetic Delivery Note identities. The recurring controller's ERP submit and reconciliation are covered by deterministic fault injection.

## Live acceptance evidence

A separate historical qualification captured the isolated site running ERPNext 15.124.1 with Frappe 15.124.0. Lot `NV-QMS-b53566ffb563` had a pH of 7.2 against 5.0–6.0 in `MAT-QA-2026-00003`. The approver's release attempt failed. The investigation and corrective action were linked to the lot, and the approver authorized rework. Inspection `MAT-QA-2026-00004` measured 5.5. The analyst's workflow and direct record update both failed to release it. The approver released 100 Nos against SHAMPOO-R1 and then revoked the release. Frappe denied the analyst's revocation attempt. The post-restart native export matched the saved snapshot and the lot remained held. The export includes earlier fixtures from setup and permission troubleshooting on the same dedicated site.

During first-time setup, the installer checks an authenticated TCP query from a separate container on the run network before starting Bench. If socket health succeeds but this query fails, inspect the private bootstrap-login log and the database account grants. An interrupted database initialization can leave a volume without the required grant. The installer preserves the volume and leaves grant repair to its operator.
