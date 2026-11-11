"""Static validation of SQL before anyone runs it: parse it in the source's dialect, make sure it is one read-only
statement, check every table and column it references against the catalog, transpile it to another dialect when
asked, apply policy to the tables and columns it reads, and estimate its cost with ``EXPLAIN`` through a callback
the caller controls. Nothing here executes the statement; :attr:`ValidationReport.ran` is always False.

Needs ``sqlglot`` (the ``lineage`` extra of agentfrontier, a dependency of this distribution).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, UnsupportedError

__all__ = ["SchemaCatalog", "Check", "ValidationReport", "validate_sql", "sqlglot_dialect", "referenced_tables"]

# AgentFrontier source types -> sqlglot dialects
_DIALECTS = {"postgresql": "postgres", "postgres": "postgres", "cockroachdb": "postgres", "aurora-postgresql": "postgres",
             "alloydb": "postgres", "greenplum": "postgres", "redshift": "redshift", "mysql": "mysql",
             "mariadb": "mysql", "aurora-mysql": "mysql", "sqlite": "sqlite", "oracle": "oracle",
             "oracle-autonomous": "oracle", "sqlserver": "tsql", "azure-sql": "tsql", "snowflake": "snowflake",
             "bigquery": "bigquery", "databricks": "databricks", "duckdb": "duckdb", "trino": "trino"}

_WRITES = tuple(getattr(exp, n) for n in ("Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command",
                                           "TruncateTable", "Grant", "Copy", "Set") if hasattr(exp, n))
_READS = tuple(getattr(exp, n) for n in ("Select", "Union", "Intersect", "Except", "SetOperation", "Subquery")
               if hasattr(exp, n))


def sqlglot_dialect(source_type: str | None) -> str:
    return _DIALECTS.get((source_type or "").lower(), (source_type or "postgres").lower())


class SchemaCatalog:
    """Tables and their columns, keyed ``schema.table`` (lower case), with optional labels per column (for policy)
    and a default schema for unqualified names."""

    def __init__(self, tables: dict[str, dict[str, str]], labels: dict[str, dict[str, list[str]]] | None = None,
                 default_schema: str | None = None):
        self.tables = {k.lower(): {c.lower(): (t or "TEXT") for c, t in cols.items()} for k, cols in tables.items()}
        self.labels = {k.lower(): {c.lower(): v for c, v in cols.items()} for k, cols in (labels or {}).items()}
        self.default_schema = default_schema.lower() if default_schema else None

    def resolve(self, name: str, schema: str | None) -> tuple[str | None, list[str]]:
        """The ``schema.table`` key a reference means, or None with candidates (close or same-named tables)."""
        n = name.lower()
        if schema:
            key = schema.lower() + "." + n
            return (key, []) if key in self.tables else (None, self._close(key))
        same = [k for k in self.tables if k.split(".", 1)[-1] == n]
        if len(same) == 1:
            return same[0], []
        if self.default_schema and self.default_schema + "." + n in self.tables:
            return self.default_schema + "." + n, []
        return None, same or self._close(n)

    def _close(self, key: str) -> list[str]:
        names = list(self.tables) + [k.split(".", 1)[-1] for k in self.tables]
        out = []
        for c in difflib.get_close_matches(key, names, 3, 0.7):
            out += [k for k in self.tables if k == c or k.endswith("." + c)]
        return list(dict.fromkeys(out))[:3]


@dataclass
class Check:
    name: str
    status: str          # pass, fail, warn, skip
    message: str
    detail: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"name": self.name, "status": self.status, "message": self.message, **({"detail": self.detail}
                                                                                      if self.detail else {})}


@dataclass
class ValidationReport:
    sql: str
    dialect: str
    checks: list[Check] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    transpiled: str | None = None
    target_dialect: str | None = None
    cost: dict | None = None
    ran: bool = False

    @property
    def status(self) -> str:
        if any(c.status == "fail" for c in self.checks):
            return "failed"
        if any(c.status == "warn" for c in self.checks):
            return "warnings"
        return "passed"

    @property
    def ok(self) -> bool:
        return self.status != "failed"

    def errors(self) -> list[str]:
        return [c.message for c in self.checks if c.status == "fail"]

    def to_json(self) -> dict:
        return {"status": self.status, "ok": self.ok, "ran": self.ran, "dialect": self.dialect, "sql": self.sql,
                "tables": self.tables, "columns": self.columns, "checks": [c.to_json() for c in self.checks],
                "transpiled": self.transpiled, "targetDialect": self.target_dialect, "cost": self.cost}


def referenced_tables(sql: str, dialect: str = "postgres") -> list[tuple[str | None, str]]:
    """(schema, table) of every table a statement reads, CTE names excluded. Empty when it does not parse."""
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except ParseError:
        return []
    out: list[tuple[str | None, str]] = []
    for tree in trees:
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            if t.name and not (not t.db and t.name.lower() in ctes):
                out.append((t.db or None, t.name))
    return list(dict.fromkeys(out))


_COST = re.compile(r"cost=([\d.]+)\.\.([\d.]+) rows=(\d+)")


def validate_sql(sql: str, dialect: str = "postgres", catalog: SchemaCatalog | None = None,
                 target_dialect: str | None = None,
                 policy: Callable[[str, list[str], list[str]], tuple[bool, str, list[dict]]] | None = None,
                 explain: Callable[[str], Any] | None = None, max_cost: float | None = None) -> ValidationReport:
    """Validate one statement statically.

    :param catalog: tables and columns to check references against; without it, references are not checked
    :param policy: ``(table, columns, labels) -> (allowed, reason, obligations)`` per table read
    :param explain: runs ``EXPLAIN <sql>`` (read-only) and returns its rows or text; the cost is parsed from it
    """
    rep = ValidationReport(sql.strip(), dialect)
    text = sql.strip().rstrip(";").strip()
    if not text:
        rep.checks.append(Check("parse", "fail", "the statement is empty"))
        return rep
    try:
        trees = [t for t in sqlglot.parse(text, read=dialect) if t is not None]
    except ParseError as e:
        first = (e.errors or [{}])[0]
        where = (" at line " + str(first.get("line")) + ", column " + str(first.get("col"))) if first.get("line") else ""
        rep.checks.append(Check("parse", "fail", "the SQL does not parse in " + dialect + where + ": "
                                + str(first.get("description") or e).split("\n")[0],
                                {"line": first.get("line"), "column": first.get("col")}))
        return rep
    rep.checks.append(Check("parse", "pass", "parses as " + dialect))
    if len(trees) != 1:
        rep.checks.append(Check("single_statement", "fail", "one statement expected, got " + str(len(trees))))
        return rep
    tree = trees[0]
    writes = [n for n in tree.walk() if isinstance(n, _WRITES)]
    into = tree.find(exp.Into)
    if writes or into is not None or not isinstance(tree, _READS):
        kind = type(writes[0] if writes else tree).__name__.upper()
        rep.checks.append(Check("read_only", "fail", "only read-only SELECT statements are allowed (found "
                                + ("SELECT INTO" if into is not None else kind) + ")"))
        return rep
    rep.checks.append(Check("read_only", "pass", "a read-only query"))

    refs = referenced_tables(text, dialect)
    resolved: dict[tuple[str | None, str], str] = {}
    if catalog is not None:
        missing = []
        for schema, name in refs:
            key, cands = catalog.resolve(name, schema)
            if key is None:
                missing.append(((schema + ".") if schema else "") + name
                               + (" (did you mean " + ", ".join(cands) + "?)" if cands else ""))
            else:
                resolved[(schema, name)] = key
        if missing:
            rep.checks.append(Check("tables", "fail", "unknown table" + ("s" if len(missing) > 1 else "") + ": "
                                    + "; ".join(missing)))
        else:
            rep.checks.append(Check("tables", "pass", str(len(refs)) + " table(s) found in the catalog"))
        rep.tables = list(dict.fromkeys(resolved.values()))
        if not missing:
            _check_columns(rep, tree, dialect, catalog, resolved)
    else:
        rep.tables = [((s + ".") if s else "") + n for s, n in refs]
        rep.checks.append(Check("tables", "skip", "no catalog given; references not checked"))

    for c in _lint(tree):
        rep.checks.append(c)

    if target_dialect and sqlglot_dialect(target_dialect) != dialect:
        tgt = sqlglot_dialect(target_dialect)
        try:
            rep.transpiled = sqlglot.transpile(text, read=dialect, write=tgt, unsupported_level="raise")[0]
            rep.target_dialect = tgt
            rep.checks.append(Check("transpile", "pass", "transpiled to " + tgt))
        except (UnsupportedError, ParseError) as e:
            rep.checks.append(Check("transpile", "warn", "could not transpile to " + tgt + ": "
                                    + str(e).split("\n")[0]))

    if policy is not None and catalog is not None:
        _check_policy(rep, catalog, policy)

    if explain is not None:
        _estimate(rep, text, explain, max_cost)
    return rep


def _check_columns(rep: ValidationReport, tree: exp.Expression, dialect: str, catalog: SchemaCatalog,
                   resolved: dict[tuple[str | None, str], str]) -> None:
    """Column references, through sqlglot's qualifier (aliases, CTEs and subqueries handled)."""
    from sqlglot.optimizer.qualify import qualify
    t = tree.copy()
    for node in t.find_all(exp.Table):
        key = resolved.get((node.db or None, node.name))
        if key and "." in key:
            node.set("db", exp.to_identifier(key.split(".", 1)[0]))
            node.set("this", exp.to_identifier(key.split(".", 1)[1]))
    mapping: dict[str, dict[str, dict[str, str]]] = {}
    for key in set(resolved.values()):
        s, n = key.split(".", 1) if "." in key else ("", key)
        mapping.setdefault(s, {})[n] = {c: "TEXT" for c in catalog.tables[key]}
    try:
        q = qualify(t, schema=mapping, dialect=dialect, validate_qualify_columns=True, quote_identifiers=False,
                    identify=False)
    except OptimizeError as e:
        rep.checks.append(Check("columns", "fail", _column_message(str(e), catalog, resolved)))
        return
    except Exception as e:  # noqa: BLE001 - a construct the qualifier does not model: say so, do not fail
        rep.checks.append(Check("columns", "warn", "columns not fully checked: " + str(e).split("\n")[0]))
        return
    cols = []
    alias_to_key = {}
    for node in q.find_all(exp.Table):
        key = (node.db.lower() + "." if node.db else "") + node.name.lower()
        alias_to_key[(node.alias_or_name or node.name).lower()] = key
    for c in q.find_all(exp.Column):
        key = alias_to_key.get((c.table or "").lower())
        if key in catalog.tables and c.name.lower() in catalog.tables[key]:
            cols.append(key + "." + c.name.lower())
    rep.columns = sorted(set(cols))
    rep.checks.append(Check("columns", "pass", str(len(rep.columns)) + " column(s) found in the catalog"))


def _column_message(err: str, catalog: SchemaCatalog, resolved: dict) -> str:
    m = re.search(r"[Cc]olumn '([^']+)'", err)
    if not m:
        return "column check failed: " + err.split("\n")[0]
    name = m.group(1).lower()
    known = sorted({c for k in resolved.values() for c in catalog.tables.get(k, {})})
    close = difflib.get_close_matches(name, known, 3, 0.6)
    return ("unknown column " + name + " in " + ", ".join(sorted(set(resolved.values())))
            + ("; did you mean " + ", ".join(close) + "?" if close else ""))


def _lint(tree: exp.Expression) -> Iterable[Check]:
    if any(isinstance(s.parent, (exp.Select, exp.Column)) for s in tree.find_all(exp.Star)):  # not count(*)
        yield Check("select_star", "warn", "SELECT * reads every column; name the columns you need")
    for j in tree.find_all(exp.Join):
        if not j.args.get("on") and not j.args.get("using") and (j.args.get("kind") or "").upper() != "CROSS" \
                and not isinstance(j.this, (exp.Unnest, exp.Lateral)):
            yield Check("join_condition", "warn", "a join without ON or USING is a cross join")
            break


def _check_policy(rep: ValidationReport, catalog: SchemaCatalog, policy: Callable) -> None:
    denied, masked = [], []
    for key in rep.tables:
        cols = [c.split(".")[-1] for c in rep.columns if c.startswith(key + ".")]
        labels = sorted({lab for c in cols for lab in catalog.labels.get(key, {}).get(c, [])})
        allowed, reason, obligations = policy(key, cols, labels)
        if not allowed:
            denied.append(key + ": " + reason)
        for o in obligations or []:
            if o.get("type") == "mask":
                masked.append(key)
    if denied:
        rep.checks.append(Check("policy", "fail", "policy denies reading " + "; ".join(denied)))
    elif masked:
        rep.checks.append(Check("policy", "warn", "allowed; values of labelled columns will be masked in "
                                + ", ".join(dict.fromkeys(masked))))
    else:
        rep.checks.append(Check("policy", "pass", "policy allows reading every table"))


def _estimate(rep: ValidationReport, text: str, explain: Callable[[str], Any], max_cost: float | None) -> None:
    try:
        plan = explain(text)
    except Exception as e:  # noqa: BLE001 - the database says why; a validation is not an execution
        rep.checks.append(Check("explain", "fail", "EXPLAIN failed: " + str(e).split("\n")[0]))
        return
    flat = plan if isinstance(plan, str) else "\n".join(" ".join(str(v) for v in (r if isinstance(r, (list, tuple))
                                                                                 else [r])) for r in plan or [])
    m = _COST.search(flat)
    if m:
        rep.cost = {"startup": float(m.group(1)), "total": float(m.group(2)), "rows": int(m.group(3))}
        if max_cost is not None and rep.cost["total"] > max_cost:
            rep.checks.append(Check("cost", "warn", "estimated cost " + format(rep.cost["total"], ",.0f")
                                    + " is above the limit of " + format(max_cost, ",.0f")))
        else:
            rep.checks.append(Check("cost", "pass", "estimated cost " + format(rep.cost["total"], ",.0f") + ", about "
                                    + format(rep.cost["rows"], ",") + " rows"))
    else:
        rep.cost = {"plan": flat[:2000]}
        rep.checks.append(Check("explain", "pass", "the database accepted the plan"))
