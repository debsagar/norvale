"""Archive approved InvenTree definitions and their native component links."""
if __package__:
    from .boundary import ingest
else:
    from boundary import ingest
from integrations.revisions import valid_receiver_marker


def definition_document(entity, publication):
    definition = publication["definition"]
    if str(definition["pk"]) != str(publication["native_id"]):
        raise ValueError("PLM definition differs from published record")
    approval = publication["approval"]
    return {"name": str(publication["native_id"]), "product": definition["name"],
            "revision": definition["revision"], "version": publication["source_version"],
            "approved_on": approval["on"], "effective_on": approval["effective"],
            "components": [{field: line[field] for field in
                            ("native_id", "component_id", "source_id", "unit", "kind", "quantity")}
                           for line in definition["lines"]]}


def scan(root, source, client, part_ids):
    return ingest(root, "InvenTree Part", lambda: (client.publication(pk) for pk in part_ids),
                  source=source, projection=definition_document)


def scan_publications(root, source, client, family_ids, on, *, registered_family=None):
    """Archive the as-of PLM export, including review evidence and receiver receipts."""
    def fetch():
        for family in family_ids:
            yield from client.export(family, on=on)

    def project(entity, record):
        result = definition_document(entity, record)
        for key in ("approval", "review"):
            result[key] = {k: record[key][k] for k in ("actor", "on", "evidence", "sha256")}
        if proposal := record["definition"].get("proposal"):
            result["proposal"] = {key: proposal[key] for key in
                                  ("doctype", "id", "version", "on", "base_id", "base_version")}
        result["revision_of"] = record["definition"]["revision_of"]
        result["acknowledgments"] = {}
        for system, receipt in record["acknowledgments"].items():
            if (receipt["plm_native_id"] != record["native_id"]
                    or receipt["plm_source_version"] != record["source_version"]):
                raise ValueError("Receiver receipt refers to another PLM revision")
            result["acknowledgments"][system] = {k: receipt[k] for k in
                ("native_id", "plm_native_id", "plm_source_version", "on")}
            if "receiver_marker" in receipt:
                marker = receipt["receiver_marker"]
                if not valid_receiver_marker(system, marker):
                    raise ValueError("Invalid PLM receiver marker")
                result["acknowledgments"][system]["receiver_marker"] = marker
        return result

    family_ids = tuple(family_ids)
    scope = {'family': registered_family} if registered_family is not None else None
    def changes():
        for family in family_ids:
            yield from client.change_events(family, on=on)

    count = 0
    errors = []
    for entity, read, projection in (("InvenTree Publication", fetch, project),
                                     ("InvenTree Change", changes, lambda _, row: row)):
        try:
            count += ingest(root, entity, read, source=source, projection=projection, scope=scope)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise ExceptionGroup("PLM archive scans failed", errors)
    return count


def scan_bom(root, source, client, part_ids, *, registered_family=None):
    """Read the role-visible assembly and BOM independently of approval metadata."""
    def fetch():
        for pk in part_ids:
            part = client._part(pk)
            yield {"name": str(pk), "product": part["name"], "revision": part["revision"],
                   "locked": part["locked"], "components": client._bom(pk)}
    def project(entity, row):
        return {**row, "components": [{k: line[k] for k in
                ("native_id", "component_id", "source_id", "unit", "kind", "quantity")}
                for line in row["components"]]}
    scope = {"family": registered_family} if registered_family is not None else None
    return ingest(root, "InvenTree BOM", fetch, source=source, projection=project, scope=scope)


if __name__ == "__main__":
    import argparse
    import os
    from integrations.plm import PLM

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--part", action="append", type=int, required=True)
    parser.add_argument("--view", choices=("bom", "approved", "publication"), required=True)
    parser.add_argument("--on", help="Business date for the publication view")
    args = parser.parse_args()
    client = PLM(args.url, os.environ["PLM_TOKEN"], publisher=args.publisher)
    collect = scan_bom if args.view == "bom" else scan
    if args.view == "publication":
        if not args.on:
            parser.error("--on is required for the publication view")
        print(scan_publications(args.root, args.source, client, args.part, args.on))
    else:
        print(collect(args.root, args.source, client, args.part))
