"""Use case 2: catch breaking schema changes before they reach downstream consumers.

    python examples/schema_drift.py snapshot -o baseline.json          # record the schema you depend on
    python examples/schema_drift.py check --baseline baseline.json     # compare the live schema with it
    python examples/schema_drift.py diff old.json new.json             # compare two snapshots

``check`` and ``diff`` print a report (``--markdown`` also writes one for a PR comment) and exit with status 1 when a
change at or above ``--fail-on`` (default ``breaking``) is found, so the script can gate a CI job or a deployment.
Pass a connection file as the first argument of ``snapshot`` / ``check`` to point it at your own database.

A snapshot is a small, sorted JSON document (datasets, fields, keys, indexes) that is meant to be committed next to
the code that depends on the schema. Constraints and indexes are compared by content (type, columns, target), not by
name, so engine-generated names do not cause noise.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agentfrontier import AgentFrontier, MemorySink, PipelineConfig

from common import levels, load_connection, qualified

SEVERITIES = ("info", "warning", "breaking")


# --- snapshot ---------------------------------------------------------------------------------------------------

def take_snapshot(af: AgentFrontier, connection) -> dict:
    report = af.run(PipelineConfig.of(connection, ["metadata"], [{"type": "memory", "name": "drift"}]))
    if report.status == "FAILED":
        raise SystemExit(f"Extraction failed: {report.errors}")
    sink = MemorySink.get("drift")
    lv = levels(af, connection)
    datasets: dict[str, dict] = {}
    for d in sink.records("dataset"):
        datasets[qualified(d, lv, "name")] = {"type": d.string("type"), "fields": {}, "keys": [], "indexes": []}
    for f in sink.records("field"):
        datasets.setdefault(qualified(f, lv), {"fields": {}, "keys": [], "indexes": []})["fields"][f.string("name")] = {
            "type": f.string("dataType"), "nullable": f.bool("nullable", True), "default": f.string("defaultValue"),
        }
    for c in sink.records("constraint"):
        key = f"{c.string('type')}({', '.join(c.strings('columns'))})"
        if c.string("type") == "foreign_key":
            key += f" -> {c.string('refDataset')}({', '.join(c.strings('refColumns'))})"
        elif c.string("type") == "check":
            key += f" {c.string('definition') or ''}".rstrip()
        datasets[qualified(c, lv)]["keys"].append(key)
    for i in sink.records("index"):
        kind = "primary" if i.bool("isPrimary", False) else "unique" if i.bool("isUnique", False) else "index"
        datasets[qualified(i, lv)]["indexes"].append(f"{kind}({', '.join(i.strings('columns'))})")
    for d in datasets.values():
        d["keys"].sort()
        d["indexes"].sort()
    MemorySink.forget("drift")
    return {"source": connection.id or connection.type, "takenAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "datasets": dict(sorted(datasets.items()))}


# --- diff -------------------------------------------------------------------------------------------------------

def diff(old: dict, new: dict) -> list[tuple[str, str, str]]:
    """(severity, dataset, message) for every difference between two snapshots."""
    changes: list[tuple[str, str, str]] = []
    add = lambda sev, ds, msg: changes.append((sev, ds, msg))
    a, b = old["datasets"], new["datasets"]
    for name in sorted(a.keys() - b.keys()):
        add("breaking", name, f"{a[name].get('type') or 'dataset'} removed")
    for name in sorted(b.keys() - a.keys()):
        add("info", name, f"{b[name].get('type') or 'dataset'} added")
    for name in sorted(a.keys() & b.keys()):
        before, after = a[name], b[name]
        fa, fb = before["fields"], after["fields"]
        for col in sorted(fa.keys() - fb.keys()):
            add("breaking", name, f"column `{col}` removed")
        for col in sorted(fb.keys() - fa.keys()):
            f = fb[col]
            if not f["nullable"] and f["default"] is None:
                add("breaking", name, f"column `{col}` added as NOT NULL without a default (inserts will fail)")
            else:
                add("info", name, f"column `{col}` added ({f['type']})")
        for col in sorted(fa.keys() & fb.keys()):
            x, y = fa[col], fb[col]
            if x["type"] != y["type"]:
                add("breaking", name, f"column `{col}` type changed: {x['type']} -> {y['type']}")
            if x["nullable"] and not y["nullable"]:
                add("breaking", name, f"column `{col}` became NOT NULL")
            elif not x["nullable"] and y["nullable"]:
                add("warning", name, f"column `{col}` became nullable")
            if x["default"] != y["default"]:
                add("warning", name, f"column `{col}` default changed: {x['default']} -> {y['default']}")
        for key in sorted(set(before["keys"]) - set(after["keys"])):
            add("warning", name, f"constraint removed: {key}")
        for key in sorted(set(after["keys"]) - set(before["keys"])):
            add("warning", name, f"constraint added: {key}")
        for idx in sorted(set(before["indexes"]) - set(after["indexes"])):
            add("warning", name, f"index removed: {idx} (queries may slow down)")
        for idx in sorted(set(after["indexes"]) - set(before["indexes"])):
            add("info", name, f"index added: {idx}")
    return changes


def text_report(changes) -> str:
    if not changes:
        return "No schema changes."
    icons = {"breaking": "BREAKING", "warning": "warning ", "info": "info    "}
    order = sorted(changes, key=lambda c: (-SEVERITIES.index(c[0]), c[1]))
    return "\n".join(f"{icons[sev]}  {ds}: {msg}" for sev, ds, msg in order)


def markdown_report(changes, old: dict, new: dict) -> str:
    counts = {s: sum(1 for c in changes if c[0] == s) for s in SEVERITIES}
    lines = ["## Schema drift report", "",
             f"`{old['source']}` baseline from {old['takenAt']} vs. {new['takenAt']}: "
             f"**{counts['breaking']} breaking**, {counts['warning']} warning{'' if counts['warning'] == 1 else 's'}, "
             f"{counts['info']} additive.", ""]
    if changes:
        lines += ["| Severity | Dataset | Change |", "|---|---|---|"]
        icons = {"breaking": "🔴 breaking", "warning": "🟡 warning", "info": "🟢 info"}
        for sev, ds, msg in sorted(changes, key=lambda c: (-SEVERITIES.index(c[0]), c[1])):
            lines.append(f"| {icons[sev]} | `{ds}` | {msg} |")
    return "\n".join(lines) + "\n"


def finish(changes, old, new, args) -> int:
    print(text_report(changes))
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(markdown_report(changes, old, new), encoding="utf-8")
    if args.fail_on == "never":
        return 0
    threshold = SEVERITIES.index("info" if args.fail_on == "any" else args.fail_on)
    return 1 if any(SEVERITIES.index(sev) >= threshold for sev, _, _ in changes) else 0


# --- CLI --------------------------------------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("snapshot", help="write a snapshot of the live schema")
    s.add_argument("connection", nargs="?", help="connection JSON file (default: the bundled sample database)")
    s.add_argument("-o", "--output", default="schema-baseline.json")
    for name, helptext in (("check", "compare the live schema with a baseline"), ("diff", "compare two snapshots")):
        c = sub.add_parser(name, help=helptext)
        if name == "check":
            c.add_argument("connection", nargs="?", help="connection JSON file (default: the bundled sample)")
            c.add_argument("--baseline", default="schema-baseline.json")
        else:
            c.add_argument("old")
            c.add_argument("new")
        c.add_argument("--fail-on", choices=["breaking", "warning", "any", "never"], default="breaking")
        c.add_argument("--markdown", help="also write a Markdown report (for example for a PR comment)")
    args = p.parse_args()

    af = AgentFrontier()
    if args.command == "snapshot":
        snap = take_snapshot(af, load_connection(args.connection))
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {args.output} ({len(snap['datasets'])} datasets)")
        return 0
    if args.command == "check":
        old = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        new = take_snapshot(af, load_connection(args.connection))
    else:
        old, new = (json.loads(Path(f).read_text(encoding="utf-8")) for f in (args.old, args.new))
    return finish(diff(old, new), old, new, args)


if __name__ == "__main__":
    sys.exit(main())
