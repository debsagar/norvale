"""Single-writer Delta archive of records already read through source permissions."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
from deltalake import DeltaTable, QueryBuilder, write_deltalake


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class Lake:
    def __init__(self, path, allowed):
        self.path = str(Path(path).resolve())
        self.allowed = {role: set(entities) for role, entities in allowed.items()}

    def latest_hashes(self, source, entity):
        latest = {}
        if Path(self.path, '_delta_log').exists():
            # The payload history can be large. Deduplication needs only the most
            # recent digest per native record in the source/entity being scanned.
            source_literal = source.replace("'", "''")
            entity_literal = entity.replace("'", "''")
            query = (
                "SELECT native_id, first_value(content_hash ORDER BY ingested_at DESC) AS digest "
                f"FROM snapshots WHERE source = '{source_literal}' AND entity = '{entity_literal}' "
                "GROUP BY native_id"
            )
            reader = QueryBuilder().register('snapshots', DeltaTable(self.path)).execute(query)
            latest = {row['native_id']: row['digest'] for row in pa.table(reader.read_all()).to_pylist()}
        return latest

    def ingest(self, source, entity, documents, effective_field=None, *, latest=None):
        """Append confirmed source readbacks. Caller must serialize writers and filter secrets."""
        if latest is None:
            latest = self.latest_hashes(source, entity)
        pending = {}
        new = []
        for doc in documents:
            payload = canonical(doc)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            key = str(doc['name'])
            if pending.get(key, latest.get(key)) == digest:
                continue
            pending[key] = digest
            new.append(dict(source=source, entity=entity, native_id=key, content_hash=digest,
                            source_recorded_at=str(doc.get('modified') or ''),
                            effective_at=str(doc.get(effective_field) or '') if effective_field else '',
                            ingested_at=now(), payload=payload))
        if new:
            write_deltalake(self.path, pa.Table.from_pylist(new), mode='append')
            latest.update(pending)
        return len(new)

    def _rows(self, version=None, *, entity=None):
        if not Path(self.path, '_delta_log').exists():
            return []
        table = DeltaTable(self.path, version=version)
        query = 'SELECT * FROM snapshots'
        if entity is not None:
            literal = entity.replace("'", "''")
            query += f" WHERE entity = '{literal}'"
        batches = QueryBuilder().register('snapshots', table).execute(query)
        return pa.table(batches.read_all()).to_pylist()

    def read(self, role, entity, version=None):
        if entity not in self.allowed.get(role, set()):
            raise PermissionError(f'{role} cannot read {entity}')
        return self._rows(version, entity=entity)

    def history(self):
        return DeltaTable(self.path).history() if Path(self.path, '_delta_log').exists() else []
