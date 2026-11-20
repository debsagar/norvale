"""Offline commands for existing engine checkpoints."""

import argparse
import json
import os
import sqlite3
from pathlib import Path

from . import DAY, Engine, SECTIONS, _journal


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate object key: {key!r}")
        result[key] = value
    return result


def main(argv=None, *, adapters=None):
    parser = argparse.ArgumentParser(prog="python -m engine")
    parser.add_argument("--run", type=Path, required=True)
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument("--extend", type=Path)
    command.add_argument("--viewer", type=Path, help="Create a private offline HTML file")
    command.add_argument("--metrics", action="store_true", help="Print journal metrics")
    args = parser.parse_args(argv)
    try:
        database = args.run / "engine.sqlite3"
        if args.viewer or args.metrics:
            evidence = _journal.read(database)
            if args.viewer:
                html = _journal.viewer(evidence)
                fd = os.open(args.viewer, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    output.write(html)
            else:
                print(json.dumps(_journal.metrics(evidence, DAY), indent=2))
            return
        command = json.loads(args.extend.read_text(), object_pairs_hook=_unique_object)
        if (not isinstance(command, dict) or "id" not in command
                or set(command) - {"id", *SECTIONS, "events"}
                or any(section in command and not isinstance(command[section], dict)
                       for section in SECTIONS)
                or ("events" in command and not isinstance(command["events"], list))):
            raise ValueError("Extension command requires id, catalog objects and an events array")
        # Open the queue without loading application code or constructing a transport.
        engine = Engine.__new__(Engine)
        engine._directory = args.run
        engine._database = database
        if not engine._database.is_file():
            raise ValueError("Extension requires an existing engine checkpoint")
        _journal.secure_database(database)
        engine._active = None
        engine._adapters = dict(adapters or {})
        engine.state = {}
        engine._extend(command["id"], *(command.get(section) for section in SECTIONS),
                       command.get("events"), from_command=True)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    main()
