"""Fixture files: CSV per table, JSON Lines per table, or one SQL file of INSERT statements in dependency order,
plus a manifest (spec digest, seed, row counts, checks, limitations) built on :class:`agentfrontier.provenance.Manifest`.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from decimal import Decimal
from typing import Any

from agentfrontier.provenance import Manifest

from .spec import FixtureSpec

__all__ = ["to_csv", "to_jsonl", "to_sql", "export", "fixture_manifest"]


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return str(v)


def _json(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def to_csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([_text(r.get(c)) for c in columns])
    return buf.getvalue()


def to_jsonl(rows: list[dict], columns: list[str]) -> str:
    return "".join(json.dumps({c: _json(r.get(c)) for c in columns}, ensure_ascii=False) + "\n" for r in rows)


def _lit(v: Any, dialect: str) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return ("1" if v else "0") if dialect in ("sqlite", "tsql", "oracle") else ("true" if v else "false")
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, dt.datetime):
        return ("timestamp '" if dialect in ("postgres", "oracle", "mysql") else "'") + v.isoformat(sep=" ") + "'"
    if isinstance(v, dt.date):
        return ("date '" if dialect in ("postgres", "oracle", "mysql") else "'") + v.isoformat() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def to_sql(spec: FixtureSpec, data: dict[str, list[dict]], order: list[str], dialect: str = "postgres",
           batch: int = 500) -> str:
    lines = ["-- synthetic fixture, seed " + str(spec.seed) + ", spec " + spec.digest()]
    for name in order:
        t = spec.table(name)
        rows = data.get(name, [])
        if t is None or not rows:
            continue
        cols = [c.name for c in t.columns]
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            lines.append("insert into " + name + " (" + ", ".join(cols) + ") values\n"
                         + ",\n".join("  (" + ", ".join(_lit(r.get(c), dialect) for c in cols) + ")" for r in chunk)
                         + ";")
    return "\n".join(lines) + "\n"


def export(spec: FixtureSpec, data: dict[str, list[dict]], order: list[str], fmt: str = "csv",
           dialect: str = "postgres") -> dict[str, str]:
    """File name -> content."""
    if fmt == "sql":
        return {"fixture.sql": to_sql(spec, data, order, dialect)}
    out = {}
    for name in order:
        t = spec.table(name)
        if t is None:
            continue
        cols = [c.name for c in t.columns]
        rows = data.get(name, [])
        if fmt == "csv":
            out[name + ".csv"] = to_csv(rows, cols)
        elif fmt == "jsonl":
            out[name + ".jsonl"] = to_jsonl(rows, cols)
        else:
            raise ValueError("format must be csv, jsonl or sql")
    return out


def fixture_manifest(spec: FixtureSpec, report: dict, validation: dict, files: dict[str, str],
                     title: str = "Synthetic fixture", privacy: dict | None = None,
                     limitations: list[str] | None = None) -> Manifest:
    m = Manifest("fixture", title, created_by="agentfrontier-synth")
    m.add_input("spec", "fixture-spec", json.dumps(spec.to_json(), sort_keys=True, default=str), seed=spec.seed)
    for name, content in files.items():
        m.add_output(name, content)
    m.add_tool("agentfrontier-synth", "0.1.0", rows=report.get("rows"), scenarios=report.get("scenarios"))
    m.set_validation("passed" if validation.get("ok") else "failed", validation.get("checks", []))
    m.limitations = list(limitations or []) + [
        "Values are generated from the spec, not sampled from real rows; distributions follow the spec's ranges "
        "and accepted values, not the source's real shape unless a profile was given.",
        "Check constraints other than accepted values and ranges are not enforced."]
    if privacy:
        m.inputs.append({"kind": "privacy", "ref": "privacy-report", **privacy})
    return m
