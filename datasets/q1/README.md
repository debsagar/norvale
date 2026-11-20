# Norvale consumer care · January–March 2027

This retrospective slice contains 7,740 records across 90 business-calendar dates from a frozen 120-day synthetic company run. It includes native ERPNext documents and ledger movements, delivered email, and Gitea maintenance reports. The source commit is 0e2d52c. The separate 132-record reference snapshot describes the released catalog, BOMs, synthetic employees and users.

Open `norvale.sqlite` or read the two UTF-8 JSONL files. In SQLite, try:

```sql
SELECT substr(business_date,1,7) AS month, kind, count(*) AS records
FROM observations GROUP BY month,kind ORDER BY month,kind;

SELECT id, json_extract(data,'$.items[0].against_sales_order') AS sales_order
FROM observations WHERE kind='Delivery Note' ORDER BY business_date,id LIMIT 10;
```

The observations table contains `business_date, source, kind, id, data`. Row identity includes source and kind. `data` preserves relevant child-row IDs, references, quantities, units, amounts, role names and text. Use `questions.jsonl` to exercise a delivery → invoice → payment trail. The builder places the answer key outside the archive. Posted payment allocations establish ledger activity; bank clearance needs additional evidence.

## Contents

| Record type | Count |
|---|---:|
| Delivery Note | 236 |
| GL Entry | 1,848 |
| Job Card | 236 |
| Journal Entry | 13 |
| Maintenance Issue | 11 |
| Message | 90 |
| Note | 15 |
| Payment Entry | 292 |
| Planning Forecast | 90 |
| Purchase Invoice | 70 |
| Purchase Order | 74 |
| Purchase Receipt | 70 |
| Quality Inspection | 324 |
| Sales Invoice | 236 |
| Sales Order | 192 |
| Stock Entry | 561 |
| Stock Ledger Entry | 3,072 |
| Supplier Quotation | 74 |
| Work Order | 236 |

## Dates, initial state and coverage

* Dates select posted documents, order and quote transaction dates, inspection report dates, planned work starts, message dates, explicitly dated Notes and forecast payloads, and the reported date inside Gitea issues. The manifest lists ERP selectors. Future due dates represent commitments known to the business. A business date is distinct from the date a user could first see a record.
* Initial evidence is the January 1 slice, including opening stock and journal movements and the operating-mandates Note. Reference rows are a separate final-export snapshot, not a reconstruction of historical permissions. The same snapshot underlies document projections; immutable version history and precise within-day order cannot be established.
* Header/line allowlists suppress later delivery and billing rollups, status changes, payment outstanding amounts and modified/creation timestamps. Inspections retain their reported results. The final stock-balances export and ticket closure state are excluded. Reports and message bodies may discuss future plans. Use as retrospective evidence; strict point-in-time evaluation requires independently versioned observations.
* This release predates the separate Odoo MES, InvenTree PLM, QMS site and Delta collectors. It contains no fabricated native records for those systems. All inspections in the frozen run passed. Private prompts, hidden states, expert-only context and authentication material are absent. Ledger and operational documents can describe the same transaction; only sum amounts on the selected accounting basis.
* Rebuild from the repository root with `python3 datasets/q1/build.py /path/to/norvale/data /path/to/empty-output`. Requires Python 3.10+ and SQLite. The builder validates source hashes, record identity, message dates, 90 daily dates and SQLite integrity. See manifest.json for per-file SHA-256. Only the public package should be supplied to an extractor.

## Benchmark design and maintenance

The unit of evaluation is an answer backed by native record identities. A grader may score evidence sufficiency independently of answer correctness. Keep the owner key outside the extractor's inputs. Publish dataset changes as a new version with source hashes, and retain the earlier tag or commit so an experiment can be reproduced.

IBM's AssetOpsBench provides seven scenario groups (CAR, FCC, FMEA, FMSR, health, TSFM and WOSR) and four suite sizes (all, lite, mini and open). Its server code separates IoT, failure-mode retrieval, time-series modeling, vibration and work orders. The overlap here is evidence-based questions across specialized records and tools. This dataset exercises cross-record order fulfillment; the IBM environments require separate assets, sensor streams and evaluation targets. [Pinned scenario suite](https://github.com/IBM/AssetOpsBench/tree/0d044061ab3ffd4b213ddf6a0868c360d7e9667c/benchmarks/scenario_suite) · [Native tool servers](https://github.com/IBM/AssetOpsBench/tree/0d044061ab3ffd4b213ddf6a0868c360d7e9667c/src/servers)
