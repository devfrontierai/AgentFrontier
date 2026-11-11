"""NL-to-SQL: assemble grounded context for a question (relevant tables, their columns, join conditions, business
terms, with policy applied while assembling), ask a model for one read-only query, validate it statically against
the catalog, repair it once from the validation errors, and run it only through a guarded callback the caller owns.
Every result carries a provenance manifest. :func:`evaluate` scores a generator on labelled questions by comparing
result sets with reference queries.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from agentfrontier.provenance import Manifest

from .llm import Budget, Client, Usage, structured_output
from .sqlcheck import SchemaCatalog, ValidationReport, validate_sql

__all__ = ["SqlContext", "Relationship", "assemble_context", "generate_sql", "Nl2SqlResult", "Case", "evaluate",
           "compare_results"]

Relationship = tuple[str, list[str], str, list[str]]  # (child table, columns, parent table, parent columns)
_STOP = {"the", "and", "for", "with", "from", "that", "this", "what", "which", "how", "many", "much", "are", "was",
         "were", "per", "by", "of", "in", "on", "to", "a", "an", "is", "me", "show", "give", "list", "all", "each",
         "top", "last", "year", "month", "total", "number"}


def _words(s: str) -> list[str]:
    out = []
    for w in re.split(r"[^a-z0-9]+", s.lower()):
        if len(w) < 3 or w in _STOP or w.isdigit():
            continue
        out.append(w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 3 else w)
    return out


@dataclass
class SqlContext:
    question: str
    tables: list[str]
    joins: list[str]
    text: str
    excluded: list[str] = field(default_factory=list)


def assemble_context(question: str, catalog: SchemaCatalog, relationships: Iterable[Relationship] = (),
                     glossary: Iterable[dict] = (), max_tables: int = 8, hint_tables: Iterable[str] = (),
                     allowed: Callable[[str], bool] | None = None,
                     column_allowed: Callable[[str, str], bool] | None = None,
                     comments: dict[str, str] | None = None) -> SqlContext:
    """The tables that matter for a question (table-name hits weigh three times column hits, hinted tables first,
    then foreign-key neighbours of the top three), their columns and the join conditions between them. Tables the
    policy does not allow are left out and listed in ``excluded``."""
    rels = list(relationships)
    q = set(_words(question))
    score: dict[str, float] = {}
    for key, cols in catalog.tables.items():
        tw = set(_words(key.split(".", 1)[-1]))
        s = 3.0 * len(q & tw)
        s += sum(1.0 for c in cols if set(_words(c)) & q)
        if s:
            score[key] = s
    chosen = [h.lower() for h in hint_tables if h.lower() in catalog.tables]
    for k, _ in sorted(score.items(), key=lambda x: (-x[1], x[0])):
        if len(chosen) >= max_tables:
            break
        if k not in chosen:
            chosen.append(k)
    for child, _, parent, _ in rels:
        for a, b in ((child, parent), (parent, child)):
            if a in chosen[:3] and b not in chosen and b in catalog.tables and len(chosen) < max_tables + 3:
                chosen.append(b)
    excluded = [t for t in chosen if allowed is not None and not allowed(t)]
    chosen = [t for t in chosen if t not in excluded]
    lines = ["Tables (" + str(len(chosen)) + "):"]
    for t in chosen:
        lines.append("")
        lines.append(t + (("  -- " + comments[t]) if comments and comments.get(t) else ""))
        for c, ty in catalog.tables[t].items():
            if column_allowed is not None and not column_allowed(t, c):
                continue
            lines.append("  " + c + " " + ty)
    joins = []
    for child, cols, parent, refs in rels:
        if child in chosen and parent in chosen:
            joins.append(" AND ".join(child + "." + c + " = " + parent + "." + r for c, r in zip(cols, refs)))
    if joins:
        lines += ["", "Join conditions:"] + ["  " + j for j in joins]
    terms = [g for g in glossary if set(_words(str(g.get("name", "")))) & q]
    if terms:
        lines += ["", "Business terms:"]
        for g in terms:
            lines.append("  " + str(g.get("name")) + ": " + str(g.get("definition") or "")
                         + ((" (expression: " + str(g["expression"]) + ")") if g.get("expression") else ""))
    if excluded:
        lines += ["", "Not available to you by policy: " + ", ".join(excluded)]
    return SqlContext(question, chosen, joins, "\n".join(lines) + "\n", excluded)


@dataclass
class Nl2SqlResult:
    question: str
    sql: str | None
    validation: ValidationReport | None
    attempts: list[dict]
    assumptions: list[str]
    usage: Usage
    manifest: Manifest
    columns: list[str] | None = None
    rows: list[list] | None = None
    executed: bool = False
    error: str | None = None

    def to_json(self) -> dict:
        return {"question": self.question, "sql": self.sql, "ok": bool(self.validation and self.validation.ok),
                "validation": self.validation.to_json() if self.validation else None, "attempts": self.attempts,
                "assumptions": self.assumptions, "executed": self.executed, "columns": self.columns,
                "rows": self.rows, "error": self.error, "manifest": self.manifest.to_json(),
                "usage": {"inputTokens": self.usage.input_tokens, "outputTokens": self.usage.output_tokens}}


_SCHEMA = {"type": "object", "required": ["sql"],
           "properties": {"sql": {"type": "string", "description": "one read-only SELECT"},
                          "assumptions": {"type": "array", "items": {"type": "string"}}}}


def _prompt(question: str, dialect: str, context: SqlContext, today: dt.date, extra: str) -> str:
    return (f"Write one read-only SQL query in the {dialect} dialect that answers the question.\n"
            f"Today is {today}. Use only the tables and columns listed; qualify tables with their schema. "
            "Aggregate in SQL and return a small result. Exclude cancelled or invalid records where the data "
            "suggests it, and state that as an assumption.\n" + (extra + "\n" if extra else "")
            + "\nQuestion: " + question + "\n\n" + context.text)


def generate_sql(question: str, client: Client, catalog: SchemaCatalog, dialect: str = "postgres",
                 context: SqlContext | None = None, relationships: Iterable[Relationship] = (),
                 glossary: Iterable[dict] = (), budget: Budget | None = None,
                 policy: Callable[[str, list[str], list[str]], tuple[bool, str, list[dict]]] | None = None,
                 execute: Callable[[str], tuple[list[str], list[list]]] | None = None, max_repairs: int = 1,
                 today: dt.date | None = None, extra_instructions: str = "") -> Nl2SqlResult:
    """Generate, validate, repair once, and run through ``execute`` only when validation passed."""
    ctx = context or assemble_context(question, catalog, relationships, glossary)
    m = Manifest("sql", question[:120], created_by="agentfrontier-capabilities/nl2sql")
    m.add_input("question", "question", question)
    for t in ctx.tables:
        m.add_input("table", t)
    m.add_tool("nl2sql", "1").set_model(client.provider, client.model)
    prompt = _prompt(question, dialect, ctx, today or dt.date.today(), extra_instructions)
    usage = Usage()
    attempts: list[dict] = []
    sql: str | None = None
    rep: ValidationReport | None = None
    assumptions: list[str] = []
    for i in range(max_repairs + 1):
        ask = prompt if i == 0 else (prompt + "\n\nYour previous query failed validation:\n" + (sql or "") + "\n"
                                     + "\n".join("- " + e for e in (rep.errors() if rep else [])) + "\nFix it.")
        try:
            out, u = structured_output(client, ask, _SCHEMA, budget=budget)
        except Exception as e:  # noqa: BLE001 - the model or the budget gave out: report, never guess
            m.set_validation("not_run", [{"name": "generate", "status": "fail", "message": str(e)}])
            return Nl2SqlResult(question, sql, rep, attempts, assumptions, usage, m, error=str(e))
        usage = Usage(usage.input_tokens + u.input_tokens, usage.output_tokens + u.output_tokens,
                      usage.cached_input_tokens + u.cached_input_tokens, usage.reasoning_tokens + u.reasoning_tokens)
        sql = str(out.get("sql") or "").strip()
        assumptions = [str(a) for a in out.get("assumptions") or []]
        rep = validate_sql(sql, dialect, catalog, policy=policy)
        attempts.append({"sql": sql, "status": rep.status, "errors": rep.errors()})
        if rep.ok:
            break
    assert rep is not None
    m.set_model(client.provider, client.model, inputTokens=usage.input_tokens, outputTokens=usage.output_tokens)
    m.add_output("query.sql", sql or "")
    m.limitations = assumptions
    res = Nl2SqlResult(question, sql, rep, attempts, assumptions, usage, m)
    if rep.ok and execute is not None and sql:
        try:
            res.columns, res.rows = execute(sql)
            res.executed = True
            rep.ran = True
            m.set_validation("passed", [c.to_json() for c in rep.checks] + [
                {"name": "execute", "status": "pass", "message": str(len(res.rows)) + " rows"}])
        except Exception as e:  # noqa: BLE001
            res.error = str(e)
            m.set_validation("failed", [c.to_json() for c in rep.checks] + [
                {"name": "execute", "status": "fail", "message": str(e)}])
    else:
        m.set_validation("static" if rep.ok else "failed", [c.to_json() for c in rep.checks])
    return res


# ------------------------------------------------------------------------------------------------ evaluation


@dataclass
class Case:
    question: str
    reference_sql: str
    notes: str = ""


def _norm(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return str(v).lower()
    try:
        f = float(v)
        return format(round(f, 2), ".2f")
    except (TypeError, ValueError):
        return str(v).strip().lower()


def _contains(row: Counter, want: Counter) -> bool:
    return all(row[k] >= n for k, n in want.items())


def compare_results(expected: list, got: list, extra_columns: bool = True) -> tuple[bool, str]:
    """Same rows regardless of row and column order, numbers to two decimals. With ``extra_columns`` (the
    default), a generated row may carry more columns than the reference (an id, a count next to a sum) as long as
    every expected value is in it; the number of rows must match either way."""
    a = [Counter(_norm(v) for v in r) for r in expected]
    b = [Counter(_norm(v) for v in r) for r in got]
    if len(a) != len(b):
        return False, "expected " + str(len(a)) + " rows, got " + str(len(b))
    if not extra_columns:
        same = sorted(sorted(x.elements()) for x in a) == sorted(sorted(x.elements()) for x in b)
        return (True, "same result") if same else (False, "values differ")
    free = list(range(len(b)))
    for want in sorted(a, key=lambda c: -sum(c.values())):
        hit = next((i for i in free if _contains(b[i], want)), None)
        if hit is None:
            return False, "values differ (no generated row has " + ", ".join(sorted(want.elements())[:4]) + ")"
        free.remove(hit)
    wider = bool(got) and len(got[0]) > len(expected[0])
    return True, "same result" + (" (with extra columns)" if wider else "")


def evaluate(cases: Iterable[Case], generate: Callable[[str], str | None],
             run: Callable[[str], list]) -> dict:
    """Accuracy of a generator: each case's generated SQL must return the same result as its reference query."""
    out = []
    for c in cases:
        try:
            sql = generate(c.question)
        except Exception as e:  # noqa: BLE001
            out.append({"question": c.question, "ok": False, "reason": "generation failed: " + str(e)})
            continue
        if not sql:
            out.append({"question": c.question, "ok": False, "reason": "no SQL"})
            continue
        try:
            ok, why = compare_results(run(c.reference_sql), run(sql))
        except Exception as e:  # noqa: BLE001
            ok, why = False, "query failed: " + str(e).split("\n")[0]
        out.append({"question": c.question, "ok": ok, "reason": why, "sql": sql})
    n = len(out)
    return {"cases": out, "passed": sum(1 for x in out if x["ok"]), "total": n,
            "accuracy": round(sum(1 for x in out if x["ok"]) / n, 3) if n else None}
