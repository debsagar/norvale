"""Check the executing engine against the release recorded by this company."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def verify(release_root, lock):
    directory = release_root / "engine"
    files = {p.relative_to(directory).as_posix(): sha256(p.read_bytes()).hexdigest()
             for p in directory.rglob("*") if p.is_file()
             and "__pycache__" not in p.parts and p.suffix not in {".md", ".pyc"}}
    if files != lock["files"]:
        raise ValueError("Engine differs from the pinned release")
    return lock["version"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        pin = json.loads((ROOT / "engine.lock.json").read_text())
        print(f"EnterpriseEngine {verify(args.release, pin)} at {pin['commit']}")
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
