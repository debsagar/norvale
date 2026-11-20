"""Build Norvale Q1 from the published v2 data/ directory. Python 3.10+."""
import argparse
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

START, END = "2027-01-01", "2027-03-31"
DATES = {
    "Sales Order":"transaction_date", "Supplier Quotation":"transaction_date",
    "Purchase Order":"transaction_date", "Work Order":"planned_start_date",
    "Quality Inspection":"report_date", **{t:"posting_date" for t in (
        "Job Card","Stock Entry","Delivery Note","Sales Invoice","Payment Entry",
        "Purchase Receipt","Purchase Invoice","Journal Entry")},
}
HEADER = """owner company customer supplier item_code stock_uom production_item qty bom_no sales_order work_order
workstation operation for_quantity inspection_type inspected_by reference_type reference_name child_row_reference
sample_size currency total grand_total delivery_date schedule_date due_date valid_till stock_entry_type purpose
from_warehouse to_warehouse source_warehouse fg_warehouse payment_type party_type party paid_amount
received_amount paid_from paid_to voucher_type is_return""".split()
LINE = """name idx item_code qty required_qty stock_qty rate amount uom stock_uom conversion_factor warehouse
s_warehouse t_warehouse source_warehouse target_warehouse bom_no delivery_note dn_detail
sales_order sales_order_item against_sales_order so_detail purchase_order purchase_order_item
prevdoc_docname purchase_receipt pr_detail po_detail supplier_quotation supplier_quotation_item
operation workstation planned_start_time planned_end_time time_in_mins reference_doctype reference_name allocated_amount
account debit_in_account_currency credit_in_account_currency specification min_value max_value reading_1 status""".split()
REFERENCE = {"Employee":"employee_name user_id reports_to date_of_joining", "Customer":"customer_name customer_group territory",
             "Supplier":"supplier_name supplier_group", "Item":"item_code item_name item_group stock_uom",
             "BOM":"item quantity uom", "User":"full_name", "Workstation":"workstation_name", "Operation":"operation"}
def json_text(v): return json.dumps(v,ensure_ascii=False,sort_keys=True)
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def project(doc, fields): return {k:doc[k] for k in fields if k in doc and doc[k] not in (None,"")}
def dump_lines(path, rows): path.write_text("".join(json_text(x)+"\n" for x in rows))
def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument("source",type=Path,help="Frozen Norvale release data/")
    parser.add_argument("output",type=Path,help="New or empty directory")
    args=parser.parse_args(); out=args.output; out.mkdir(parents=True,exist_ok=True)
    if any(out.iterdir()): raise SystemExit("Choose an empty output directory")
    root=args.source; src=root/"observable"; manifest=json.loads((root/"manifest.json").read_text())
    source_hashes={}
    def load(name):
        p=src/(name+".json"); key="observable/"+p.name
        source_hashes[key]=digest(p)
        if source_hashes[key]!=manifest["sha256"][key]: raise ValueError("source hash mismatch: "+key)
        return json.loads(p.read_text())
    records=load("native-records"); observed=[]; reference=[]
    def put(day,source,kind,record_id,fields):
        day=date.fromisoformat(day[:10]).isoformat()
        if START<=day<=END: observed.append(dict(business_date=day,source=source,kind=kind,id=record_id,data=fields))
    for kind,docs in records.items():
        for key,d in docs.items():
            if kind in DATES:
                fields=project(d,HEADER)
                if kind=="Quality Inspection": fields.update(project(d,["status"]))
                for table in ("items","references","accounts","readings","required_items","operations"):
                    if d.get(table):
                        keep=LINE if table=="readings" else [f for f in LINE if f!="status"]
                        fields[table]=[project(row,keep) for row in d[table]]
                put(d[DATES[kind]],"erpnext",kind,key,fields)
            elif kind=="Note":
                day=re.search(r'(2027-\d{2}-\d{2})$',d['title'])
                if day:put(day[1],"erpnext",kind,key,project(d,["title","content","owner"]))
            elif kind=="ToDo":
                try:ctx=json.loads(d.get("description",""))
                except ValueError:continue
                if isinstance(ctx,dict) and ctx.get("as_of"):
                    put(ctx["as_of"],"erpnext","Planning Forecast",key,dict(context=ctx,**project(d,["allocated_to","owner"])))
            elif kind in REFERENCE:
                fields=project(d,REFERENCE[kind].split())
                if kind=="BOM": fields["items"]=[project(r,["item_code","qty","uom","stock_qty","stock_uom"]) for r in d["items"]]
                if kind=="User": fields["roles"]=[r["role"] for r in d.get("roles",[])]
                reference.append(dict(kind=kind,id=key,data=fields))
    for filename,kind in (("stock-ledger","Stock Ledger Entry"),("general-ledger","GL Entry")):
        for row in load(filename):
            put(row["posting_date"],"erpnext",kind,row["name"],{k:v for k,v in row.items() if k not in ("name","posting_date")})
    for row in load("mail"):
        m=row["message"]; day=(date.fromisoformat(START)+timedelta(days=row["day"])).isoformat()
        assert parsedate_to_datetime(m["date"]).date().isoformat()==day
        put(day,"email","Message",m["message_id"],dict(sender_role=row["sender"],recipient_role=row["recipient"],**project(m,["from","to","subject","body"])))
    for row in load("tickets"):
        match=re.search(r'reported (2027-\d{2}-\d{2})',row['body'])
        if match:put(match[1],"gitea","Maintenance Issue",str(row["number"]),dict(title=row["title"],body=re.sub(r'<!--.*?-->\s*','',row['body'])))
    observed.sort(key=lambda x:(x['business_date'],x['source'],x['kind'],x['id']))
    reference.sort(key=lambda x:(x['kind'],x['id']))
    identities={(x['source'],x['kind'],x['id']) for x in observed};assert len(identities)==len(observed)
    days=Counter(x['business_date'] for x in observed);assert len(days)==90
    dump_lines(out/"observations.jsonl",observed); dump_lines(out/"reference.jsonl",reference)
    with sqlite3.connect(out/"norvale.sqlite") as db:
        db.executescript("CREATE TABLE observations(business_date TEXT,source TEXT,kind TEXT,id TEXT,data TEXT,PRIMARY KEY(source,kind,id)); CREATE INDEX by_kind_date ON observations(kind,business_date);CREATE TABLE reference(kind TEXT,id TEXT,data TEXT,PRIMARY KEY(kind,id));")
        db.executemany("INSERT INTO observations VALUES(?,?,?,?,?)",[(r['business_date'],r['source'],r['kind'],r['id'],json_text(r['data'])) for r in observed])
        db.executemany("INSERT INTO reference VALUES(?,?,?)",[(r['kind'],r['id'],json_text(r['data'])) for r in reference])
        assert db.execute("PRAGMA integrity_check").fetchone()==("ok",)
    shipments=[r for r in observed if r['kind']=='Delivery Note']
    order=shipments[0]['data']['items'][0]['against_sales_order']
    deliveries={r['id']:r for r in shipments if any(x.get('against_sales_order')==order for x in r['data']['items'])}
    invoices={r['id']:r for r in observed if r['kind']=='Sales Invoice' and any(x.get('delivery_note') in deliveries for x in r['data']['items'])}
    payments={r['id']:r for r in observed if r['kind']=='Payment Entry' and any(x.get('reference_doctype')=='Sales Invoice' and x.get('reference_name') in invoices for x in r['data']['references'])}
    assert deliveries and invoices and payments
    def alloc(r):
        return sum(Decimal(str(x['allocated_amount'])) for x in r['data']['references'] if x.get('reference_doctype')=='Sales Invoice' and x.get('reference_name') in invoices)
    case=[("shipments",f"Which Delivery Notes contain a line against {order}?",sorted(deliveries)),
          ("invoices",f"Which Sales Invoices contain a line linked to one of those deliveries for {order}?",sorted(invoices)),
          ("payments",f"Which Payment Entries allocate money to those invoices for {order}?",sorted(payments)),
          ("posted-allocation",f"What amount in invoice currency was allocated to those invoices for {order} in this quarter? Return a two-decimal numeric string.",format(sum(map(alloc,payments.values())),'.2f'))]
    case.append(("release-authority",f"Can this dataset establish a separate native QMS release version authorizing the first delivery against {order}? Return 'unresolved' or the source-qualified version.","unresolved"))
    evidence={"shipments":["erpnext/Sales Order/"+order]+["erpnext/Delivery Note/"+key for key in sorted(deliveries)],
              "invoices":["erpnext/Delivery Note/"+key for key in sorted(deliveries)]+["erpnext/Sales Invoice/"+key for key in sorted(invoices)],
              "payments":["erpnext/Sales Invoice/"+key for key in sorted(invoices)]+["erpnext/Payment Entry/"+key for key in sorted(payments)],
              "posted-allocation":["erpnext/Payment Entry/"+key for key in sorted(payments)],
              "release-authority":["manifest.json#by_source","erpnext/Delivery Note/"+min(deliveries)]}
    dump_lines(out/'questions.jsonl',[dict(id=k,question=q,cutoff=END) for k,q,a in case])
    private=out.parent/(out.name+"-grading-key.json"); private.write_text(json_text({k:dict(answer=v,evidence=evidence[k]) for k,q,v in case})+'\n');private.chmod(0o600)
    counts=Counter(row['kind'] for row in observed); files=['observations.jsonl','reference.jsonl','questions.jsonl','norvale.sqlite']
    m=dict(version="1.0.0",period=[START,END],source_commit=manifest["source_commit"],source_release_manifest_sha256=digest(root/'manifest.json'),source_hashes=source_hashes,
           observations=len(observed),by_kind=dict(sorted(counts.items())),by_source=dict(Counter(r['source'] for r in observed)),by_month=dict(Counter(r['business_date'][:7] for r in observed)),
           reference_records=len(reference),business_date_fields=DATES,sha256={n:digest(out/n) for n in files})
    (out/'manifest.json').write_text(json.dumps(m,indent=2)+'\n')
    (out/'README.md').write_text(f"""# Norvale consumer care · January–March 2027

This retrospective slice contains {len(observed):,} records across 90 business-calendar dates from a frozen 120-day synthetic company run. It includes native ERPNext documents and ledger movements, delivered email, and Gitea maintenance reports. The source commit is {manifest['source_commit']}. The separate {len(reference)}-record reference snapshot describes the released catalog, BOMs, synthetic employees and users.

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
"""+''.join(f"| {k} | {v:,} |\n" for k,v in sorted(counts.items()))+"""
## Dates, initial state and coverage

* Dates select posted documents, order and quote transaction dates, inspection report dates, planned work starts, message dates, explicitly dated Notes and forecast payloads, and the reported date inside Gitea issues. The manifest lists ERP selectors. Future due dates represent commitments known to the business. A business date is distinct from the date a user could first see a record.
* Initial evidence is the January 1 slice, including opening stock and journal movements and the operating-mandates Note. Reference rows are a separate final-export snapshot, not a reconstruction of historical permissions. The same snapshot underlies document projections; immutable version history and precise within-day order cannot be established.
* Header/line allowlists suppress later delivery and billing rollups, status changes, payment outstanding amounts and modified/creation timestamps. Inspections retain their reported results. The final stock-balances export and ticket closure state are excluded. Reports and message bodies may discuss future plans. Use as retrospective evidence; strict point-in-time evaluation requires independently versioned observations.
* This release predates the separate Odoo MES, InvenTree PLM, QMS site and Delta collectors. It contains no fabricated native records for those systems. All inspections in the frozen run passed. Private prompts, hidden states, expert-only context and authentication material are absent. Ledger and operational documents can describe the same transaction; only sum amounts on the selected accounting basis.
* Rebuild from the repository root with `python3 datasets/q1/build.py /path/to/norvale/data /path/to/empty-output`. Requires Python 3.10+ and SQLite. The builder validates source hashes, record identity, message dates, 90 daily dates and SQLite integrity. See manifest.json for per-file SHA-256. Only the public package should be supplied to an extractor.
""")
    with ZipFile(out.parent/(out.name+'.zip'),'w') as z:
        for path in sorted(out.iterdir()):
            info=ZipInfo(out.name+'/'+path.name,(2027,3,31,0,0,0));info.compress_type=ZIP_DEFLATED
            z.writestr(info,path.read_bytes())
    print(json_text(m))
if __name__=='__main__':main()
