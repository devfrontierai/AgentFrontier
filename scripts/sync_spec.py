"""Re-copies the shared AgentFrontier spec into this repository.

    python scripts/sync_spec.py ../agentfrontier-spec                      # a local checkout, as it is
    python scripts/sync_spec.py ../agentfrontier-spec --ref spec-v1.1.0    # a tag of a local checkout
    python scripts/sync_spec.py https://github.com/devfrontierai/agentfrontier-spec.git --ref spec-v1.1.0

The spec (definitions, entity model, raw-stream catalog, schemas, auth catalog, sink mappings and conformance
cases) lives in the agentfrontier-spec repository and is owned jointly by every implementation. Change it there,
tag it, then run this script so the Python copies stay byte-for-byte identical:

* ``definitions``, ``sinks``, ``schemas``, ``entities.json``, ``streams.json``, ``auth-methods.json``,
  ``index.json`` -> ``src/agentfrontier/spec/``
* ``conformance`` -> ``tests/conformance/``

With ``--ref`` the given tag or branch is checked out (``git clone --depth 1``) and recorded with its commit in
``scripts/spec.lock``. Files that no longer exist in the source are removed from the copies. ``--check`` only
reports differences and exits with 1 when there are any.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE_SPEC = REPO / "src" / "agentfrontier" / "spec"
CONFORMANCE = REPO / "tests" / "conformance"
LOCK = REPO / "scripts" / "spec.lock"
PACKAGE_ITEMS = ["definitions", "sinks", "schemas", "classification", "entities.json", "streams.json",
                 "auth-methods.json", "index.json"]


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


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("spec", help="the spec: a folder holding index.json, or a git repository (path or URL) with --ref")
    ap.add_argument("--ref", help="tag or branch to check out from the spec repository")
    ap.add_argument("--check", action="store_true", help="report differences only")
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="agentfrontier-spec-") as tmp:
        commit = None
        if a.ref:
            source = a.spec if "://" in a.spec else str(Path(a.spec).resolve())
            try:
                _git("-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1", "--branch", a.ref,
                     source, tmp)
                commit = _git("-C", tmp, "rev-parse", "HEAD")
            except (subprocess.CalledProcessError, OSError) as e:
                print("cannot check out " + a.ref + " from " + a.spec + ": "
                      + (getattr(e, "stderr", None) or str(e)), file=sys.stderr)
                return 2
            spec = Path(tmp)
        else:
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
    if commit is not None and not a.check:
        LOCK.write_text("ref=" + a.ref + "\ncommit=" + commit + "\n", encoding="utf-8", newline="\n")
    print("\n".join(changes) if changes else "spec copies are up to date")
    return 1 if (a.check and changes) else 0


if __name__ == "__main__":
    sys.exit(main())
