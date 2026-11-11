"""Data-quality rules: one canonical rule model, rules recommended from what the catalog already knows (keys,
nullability, foreign keys, check constraints, known values, profiles), and translators to SQL checks, dbt tests,
Great Expectations suites and SodaCL.

A rule's SQL check counts the rows that break it, so 0 means it holds. Rules recommended from declared constraints
have ``origin="constraint"`` and confidence 1.0; rules from values or profiles are ``origin="profile"`` with a lower
confidence, because the data seen so far may not be the whole story.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = ["Rule", "TableShape", "recommend", "to_sql", "to_dbt", "to_great_expectations", "to_soda", "dump_yaml",
           "CATEGORIES", "KINDS"]

CATEGORIES = ("completeness", "uniqueness", "validity", "referential", "contract", "business")
KINDS = {"not_null": "completeness", "unique": "uniqueness", "accepted_values": "validity", "range": "validity",
         "regex": "validity", "relationship": "referential", "row_count": "contract", "freshness": "contract",
         "expression": "business", "unique_combination": "uniqueness"}


@dataclass
class Rule:
    kind: str
    table: str                       # schema.table
    columns: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    severity: str = "error"          # error, warn
    origin: str = "constraint"       # constraint, profile, inferred, user
    confidence: float = 1.0
    description: str = ""

    @property
    def category(self) -> str:
        return KINDS.get(self.kind, "business")

    @property
    def id(self) -> str:
        cols = "_".join(self.columns) if self.columns else "table"
        return (self.table.replace(".", "_") + "__" + cols + "__" + self.kind).lower()

    def to_json(self) -> dict:
        return {"id": self.id, "kind": self.kind, "category": self.category, "table": self.table,
                "columns": self.columns, "params": self.params, "severity": self.severity, "origin": self.origin,
                "confidence": self.confidence, "description": self.description or describe(self)}


def describe(r: Rule) -> str:
    c = ", ".join(r.columns)
    return {"not_null": c + " is never null",
            "unique": c + " is unique",
            "unique_combination": "(" + c + ") is unique",
            "accepted_values": c + " is one of " + ", ".join(map(str, r.params.get("values", []))),
            "range": c + " is between " + str(r.params.get("min")) + " and " + str(r.params.get("max")),
            "regex": c + " matches " + str(r.params.get("pattern")),
            "relationship": c + " exists in " + str(r.params.get("to")) + "." + str(r.params.get("field")),
            "row_count": "the table has at least " + str(r.params.get("min", 1)) + " rows",
            "freshness": c + " is at most " + str(r.params.get("max_age_hours")) + " hours old",
            "expression": str(r.params.get("expression"))}.get(r.kind, r.kind)


@dataclass
class TableShape:
    """What the recommender needs to know about a table."""
    name: str                                        # schema.table
    columns: list[tuple[str, str, bool]]             # (name, data type, nullable)
    primary_key: list[str] = field(default_factory=list)
    unique: list[list[str]] = field(default_factory=list)
    references: list[tuple[list[str], str, list[str]]] = field(default_factory=list)  # (cols, parent, parent cols)
    checks: list[str] = field(default_factory=list)  # check constraint expressions
    values: dict[str, list[Any]] = field(default_factory=dict)    # low-cardinality values seen, per column
    profile: dict[str, dict] = field(default_factory=dict)        # per column: min, max, nulls, rows


_IN = re.compile(r"\(?\s*\"?(\w+)\"?\s+in\s*\(([^)]*)\)", re.I)
_ANY = re.compile(r"\(?\s*\"?(\w+)\"?\s*(?:::\w+)?\s*=\s*any\s*\(\s*\(?\s*array\s*\[([^\]]*)\]", re.I)
_CMP = re.compile(r"\(?\s*\"?(\w+)\"?\s*(>=|>|<=|<)\s*(-?\d+(?:\.\d+)?)\s*\)?$")
_DATE = re.compile(r"date|time", re.I)


def _literals(s: str) -> list[str]:
    return [re.sub(r"::\w+.*$", "", v.strip()).strip("'\" ") for v in s.split(",") if v.strip()]


def recommend(t: TableShape, include_profile: bool = True) -> list[Rule]:
    """Rules a table should satisfy, from its constraints first, then from values and profiles."""
    out: list[Rule] = []
    names = {c for c, _, _ in t.columns}
    if t.primary_key:
        if len(t.primary_key) == 1:
            out.append(Rule("unique", t.name, list(t.primary_key)))
        else:
            out.append(Rule("unique_combination", t.name, list(t.primary_key)))
    for cols in t.unique:
        if cols != t.primary_key:
            out.append(Rule("unique" if len(cols) == 1 else "unique_combination", t.name, list(cols)))
    for c, _, nullable in t.columns:
        if not nullable or c in t.primary_key:
            out.append(Rule("not_null", t.name, [c]))
    for cols, parent, refs in t.references:
        if len(cols) == 1 and len(refs) == 1:
            out.append(Rule("relationship", t.name, list(cols), {"to": parent, "field": refs[0]}))
    covered: set[str] = set()
    for chk in t.checks:
        m = _IN.search(chk) or _ANY.search(chk)
        if m and m.group(1) in names:
            out.append(Rule("accepted_values", t.name, [m.group(1)], {"values": _literals(m.group(2))}))
            covered.add(m.group(1))
            continue
        m = _CMP.search(chk.strip())
        if m and m.group(1) in names:
            v = float(m.group(3))
            v = int(v) if v.is_integer() else v
            bound = {">=": {"min": v}, ">": {"min": v, "strict": True}, "<=": {"max": v},
                     "<": {"max": v, "strict": True}}[m.group(2)]
            out.append(Rule("range", t.name, [m.group(1)], bound))
    if include_profile:
        for c, vals in t.values.items():
            if c in names and c not in covered and 0 < len(vals) <= 25:
                out.append(Rule("accepted_values", t.name, [c], {"values": list(vals)}, "warn", "profile", 0.8))
        for c, p in t.profile.items():
            if c not in names:
                continue
            if p.get("min") is not None and p.get("max") is not None and not _DATE.search(dict(
                    (n, ty) for n, ty, _ in t.columns).get(c, "")):
                out.append(Rule("range", t.name, [c], {"min": p["min"], "max": p["max"]}, "warn", "profile", 0.6))
    out.append(Rule("row_count", t.name, [], {"min": 1}, "warn", "inferred", 0.9))
    return out


def _q(ident: str) -> str:
    return ident if re.fullmatch(r"[a-z_][a-z0-9_]*", ident) else '"' + ident.replace('"', '""') + '"'


def _qt(table: str) -> str:
    return ".".join(_q(p) for p in table.split("."))


def _lit(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def to_sql(r: Rule) -> str:
    """A query returning ``failures``: the number of rows that break the rule (0 means it holds)."""
    t = _qt(r.table)
    c = _q(r.columns[0]) if r.columns else ""
    if r.kind == "not_null":
        return "select count(*) as failures from " + t + " where " + c + " is null"
    if r.kind in ("unique", "unique_combination"):
        cols = ", ".join(_q(x) for x in r.columns)
        nn = " and ".join(_q(x) + " is not null" for x in r.columns)
        return ("select count(*) as failures from (select " + cols + " from " + t + " where " + nn + " group by "
                + cols + " having count(*) > 1) d")
    if r.kind == "accepted_values":
        return ("select count(*) as failures from " + t + " where " + c + " is not null and " + c + " not in ("
                + ", ".join(_lit(v) for v in r.params.get("values", [])) + ")")
    if r.kind == "range":
        conds = []
        if r.params.get("min") is not None:
            conds.append(c + (" <= " if r.params.get("strict") else " < ") + _lit(r.params["min"]))
        if r.params.get("max") is not None:
            conds.append(c + (" >= " if r.params.get("strict") else " > ") + _lit(r.params["max"]))
        return "select count(*) as failures from " + t + " where " + " or ".join(conds or ["1 = 0"])
    if r.kind == "relationship":
        p, f = _qt(r.params["to"]), _q(r.params["field"])
        return ("select count(*) as failures from " + t + " c where c." + c + " is not null and not exists "
                "(select 1 from " + p + " p where p." + f + " = c." + c + ")")
    if r.kind == "row_count":
        return "select case when count(*) >= " + str(int(r.params.get("min", 1))) + " then 0 else 1 end as failures " \
               "from " + t
    if r.kind == "expression":
        return "select count(*) as failures from " + t + " where not (" + str(r.params["expression"]) + ")"
    if r.kind == "regex":
        return ("select count(*) as failures from " + t + " where " + c + " is not null and not regexp_like(" + c
                + ", " + _lit(r.params["pattern"]) + ")")
    raise ValueError("no SQL check for rule kind " + r.kind)


def _dbt_test(r: Rule, model_of: dict[str, str]) -> Any:
    if r.kind in ("not_null", "unique"):
        return r.kind
    if r.kind == "accepted_values":
        return {"accepted_values": {"values": list(r.params.get("values", []))}}
    if r.kind == "relationship":
        to = model_of.get(r.params["to"])
        ref = "ref('" + to + "')" if to else "source('" + r.params["to"].split(".")[0] + "', '" + \
            r.params["to"].split(".")[-1] + "')"
        return {"relationships": {"to": ref, "field": r.params["field"]}}
    if r.kind == "range":
        arg = {k: r.params[k] for k in ("min", "max") if r.params.get(k) is not None}
        return {"dbt_utils.accepted_range": {**({"min_value": arg["min"]} if "min" in arg else {}),
                                             **({"max_value": arg["max"]} if "max" in arg else {})}}
    return None


def to_dbt(rules: Iterable[Rule], model_of: dict[str, str] | None = None, sources: bool = False) -> dict:
    """A dbt ``schema.yml`` document (as a dict; :func:`dump_yaml` writes it). ``model_of`` maps ``schema.table``
    to the dbt model that builds it; tables without a model become ``sources:`` when ``sources`` is true."""
    model_of = model_of or {}
    models: dict[str, dict] = {}
    srcs: dict[str, dict] = {}
    for r in rules:
        test = _dbt_test(r, model_of)
        if r.kind == "unique_combination":
            test = {"dbt_utils.unique_combination_of_columns": {"combination_of_columns": list(r.columns)}}
        if test is None:
            continue
        name = model_of.get(r.table)
        if name is None and sources:
            schema, tbl = (r.table.split(".", 1) + [""])[:2] if "." in r.table else ("public", r.table)
            holder = srcs.setdefault(schema, {"name": schema, "tables": {}})["tables"].setdefault(
                tbl, {"name": tbl, "columns": {}, "tests": []})
        else:
            holder = models.setdefault(name or r.table.split(".")[-1], {"name": name or r.table.split(".")[-1],
                                                                       "columns": {}, "tests": []})
        if len(r.columns) == 1:
            col = holder["columns"].setdefault(r.columns[0], {"name": r.columns[0], "tests": []})
            col["tests"].append(test)
        elif r.kind == "unique_combination":
            holder["tests"].append(test)
    doc: dict = {"version": 2}

    def finish(h: dict) -> dict:
        out = {"name": h["name"]}
        if h["tests"]:
            out["tests"] = h["tests"]
        out["columns"] = list(h["columns"].values())
        return out
    if models:
        doc["models"] = [finish(m) for m in models.values()]
    if srcs:
        doc["sources"] = [{"name": s["name"], "tables": [finish(t) for t in s["tables"].values()]} for s in srcs.values()]
    return doc


def to_great_expectations(rules: Iterable[Rule], suite: str = "agentfrontier") -> dict:
    exps = []
    for r in rules:
        c = r.columns[0] if r.columns else None
        e: dict | None = None
        if r.kind == "not_null":
            e = {"expectation_type": "expect_column_values_to_not_be_null", "kwargs": {"column": c}}
        elif r.kind == "unique":
            e = {"expectation_type": "expect_column_values_to_be_unique", "kwargs": {"column": c}}
        elif r.kind == "unique_combination":
            e = {"expectation_type": "expect_compound_columns_to_be_unique", "kwargs": {"column_list": r.columns}}
        elif r.kind == "accepted_values":
            e = {"expectation_type": "expect_column_values_to_be_in_set",
                 "kwargs": {"column": c, "value_set": r.params.get("values", [])}}
        elif r.kind == "range":
            e = {"expectation_type": "expect_column_values_to_be_between",
                 "kwargs": {"column": c, "min_value": r.params.get("min"), "max_value": r.params.get("max"),
                            "strict_min": bool(r.params.get("strict")), "strict_max": bool(r.params.get("strict"))}}
        elif r.kind == "row_count":
            e = {"expectation_type": "expect_table_row_count_to_be_between",
                 "kwargs": {"min_value": r.params.get("min", 1)}}
        elif r.kind == "regex":
            e = {"expectation_type": "expect_column_values_to_match_regex",
                 "kwargs": {"column": c, "regex": r.params.get("pattern")}}
        if e is not None:
            e["meta"] = {"table": r.table, "agentfrontier_rule": r.id, "origin": r.origin}
            exps.append(e)
    return {"expectation_suite_name": suite, "expectations": exps, "meta": {"generated_by": "agentfrontier"}}


def to_soda(rules: Iterable[Rule]) -> str:
    """SodaCL checks, one ``checks for <table>`` block per table."""
    by: dict[str, list[str]] = {}
    for r in rules:
        c = r.columns[0] if r.columns else ""
        line = None
        if r.kind == "not_null":
            line = "missing_count(" + c + ") = 0"
        elif r.kind == "unique":
            line = "duplicate_count(" + c + ") = 0"
        elif r.kind == "unique_combination":
            line = "duplicate_count(" + ", ".join(r.columns) + ") = 0"
        elif r.kind == "accepted_values":
            line = "invalid_count(" + c + ") = 0:\n      valid values: " + json.dumps(
                [str(v) for v in r.params.get("values", [])])
        elif r.kind == "range":
            parts = []
            if r.params.get("min") is not None:
                parts.append("min(" + c + ") >= " + str(r.params["min"]))
            if r.params.get("max") is not None:
                parts.append("max(" + c + ") <= " + str(r.params["max"]))
            for p in parts:
                by.setdefault(r.table, []).append(p)
            continue
        elif r.kind == "row_count":
            line = "row_count >= " + str(r.params.get("min", 1))
        elif r.kind == "relationship":
            line = ("values in (" + c + ") must exist in " + r.params["to"].split(".")[-1] + " ("
                    + r.params["field"] + ")")
        if line:
            by.setdefault(r.table, []).append(line)
    return "".join("checks for " + t.split(".")[-1] + ":\n" + "".join("  - " + x + "\n" for x in lines)
                   for t, lines in by.items())


# ------------------------------------------------------------------------------------------------ YAML

_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_./ -]*$")
_RESERVED = {"true", "false", "yes", "no", "null", "on", "off", "~", ""}


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if _PLAIN.match(s) and s.lower() not in _RESERVED and not s.endswith(" ") and ": " not in s:
        return s
    return json.dumps(s, ensure_ascii=False)


def dump_yaml(o: Any, indent: int = 0) -> str:
    """Block YAML for plain dicts, lists and scalars (enough for dbt and Soda files); no dependency."""
    pad = " " * indent
    if isinstance(o, dict):
        if not o:
            return pad + "{}\n"
        out = []
        for k, v in o.items():
            if isinstance(v, (dict, list)) and v:
                out.append(pad + _scalar(k) + ":\n" + dump_yaml(v, indent + 2))
            else:
                out.append(pad + _scalar(k) + ": " + (_scalar(v) if not isinstance(v, (dict, list)) else
                                                      ("{}" if isinstance(v, dict) else "[]")) + "\n")
        return "".join(out)
    if isinstance(o, list):
        out = []
        for v in o:
            if isinstance(v, dict) and v:
                body = dump_yaml(v, indent + 2)
                out.append(pad + "- " + body[indent + 2:])
            elif isinstance(v, list) and v:
                out.append(pad + "-\n" + dump_yaml(v, indent + 2))
            else:
                out.append(pad + "- " + _scalar(v) + "\n")
        return "".join(out)
    return pad + _scalar(o) + "\n"
