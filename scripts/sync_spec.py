"""Re-copies the shared AgentFrontier spec into this repository.

    python scripts/sync_spec.py <path-to-spec>          # for example ../agentfrontier-java/spec

The spec (definitions, entity model, auth catalog, sink mappings and conformance cases) is owned jointly by every
implementation. Change it in one place, then run this script so the Python copies stay byte-for-byte identical:

* ``<spec>/definitions``, ``sinks``, ``entities.json``, ``auth-methods.json``, ``index.json``
  -> ``src/agentfrontier/spec/``
* ``<spec>/conformance`` -> ``tests/conformance/``

Files that no longer exist in the source are removed from the copies. ``--check`` only reports differences and exits
with 1 when there are any.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE_SPEC = REPO / "src" / "agentfrontier" / "spec"
CONFORMANCE = REPO / "tests" / "conformance"
PACKAGE_ITEMS = ["definitions", "sinks", "entities.json", "auth-methods.json", "index.json"]


def _files(root: Path) -> set[str]:
    if root.is_file():
        return {""}
    if not root.exists():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def _sync(src: Path, dst: Path, check: bool, changes: list[str]) -> None:
    if src.is_file():
        if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
            changes.append("update " + str(dst.relative_to(REPO)))
            if not check:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
        return
    want = _files(src)
    have = _files(dst)
    for rel in sorted(have - want):
        changes.append("remove " + str((dst / rel).relative_to(REPO)))
        if not check:
            (dst / rel).unlink()
    for rel in sorted(want):
        s, d = src / rel, dst / rel
        if not d.exists() or not filecmp.cmp(s, d, shallow=False):
            changes.append(("add " if not d.exists() else "update ") + str(d.relative_to(REPO)))
            if not check:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(s, d)


def _check_index(spec: Path) -> list[str]:
    index = json.loads((spec / "index.json").read_text(encoding="utf-8"))
    listed = set(index.get("definitions") or [])
    on_disk = _files(spec / "definitions")
    problems = []
    for f in sorted(on_disk - listed):
        problems.append("index.json does not list definitions/" + f)
    for f in sorted(listed - on_disk):
        problems.append("index.json lists missing definitions/" + f)
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("spec", help="path to the shared spec folder (the one holding index.json)")
    ap.add_argument("--check", action="store_true", help="report differences only")
    a = ap.parse_args(argv)
    spec = Path(a.spec).resolve()
    if not (spec / "index.json").is_file():
        print("not a spec folder (no index.json): " + str(spec), file=sys.stderr)
        return 2
    problems = _check_index(spec)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    changes: list[str] = []
    for item in PACKAGE_ITEMS:
        _sync(spec / item, PACKAGE_SPEC / item, a.check, changes)
    _sync(spec / "conformance", CONFORMANCE, a.check, changes)
    print("\n".join(changes) if changes else "spec copies are up to date")
    return 1 if (a.check and changes) else 0


if __name__ == "__main__":
    sys.exit(main())
