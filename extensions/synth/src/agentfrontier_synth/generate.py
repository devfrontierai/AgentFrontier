"""Deterministic generation from a :class:`~agentfrontier_synth.spec.FixtureSpec`: the same spec and seed give the
same rows. Tables are generated parents first, so every foreign key points at a generated parent row; keys and
unique columns stay unique; accepted values, ranges and nullability are respected; scenarios add the rows a test
needs (refunds, duplicate submissions, failed payments); rules keep dates in order and totals consistent.

Values are invented, never copied: names come from small built-in lists, emails use the reserved ``example.test``
domain and phone numbers the fictional 555-01xx range.
"""

from __future__ import annotations

import datetime as dt
import random
import re
import string
from decimal import Decimal
from typing import Any

from .spec import ColumnSpec, FixtureSpec, TableSpec

__all__ = ["generate", "GenerationError", "topological_order"]

FIRST = ["Ada", "Alan", "Grace", "Linus", "Margaret", "Dennis", "Barbara", "Ken", "Frances", "Edsger", "Radia",
         "Tim", "Hedy", "John", "Katherine", "Niklaus", "Shafi", "Donald", "Sophie", "Ravi", "Mei", "Omar", "Lena",
         "Tariq", "Ines", "Kofi", "Priya", "Mateo", "Yuki", "Aisha"]
LAST = ["Lovelace", "Turing", "Hopper", "Torvalds", "Hamilton", "Ritchie", "Liskov", "Thompson", "Allen", "Dijkstra",
        "Perlman", "Berners", "Lamarr", "McCarthy", "Johnson", "Wirth", "Goldwasser", "Knuth", "Wilson", "Kumar",
        "Chen", "Haddad", "Novak", "Rahman", "Silva", "Mensah", "Patel", "Garcia", "Tanaka", "Okafor"]
CITIES = ["Lisbon", "Austin", "Pune", "Osaka", "Lagos", "Toronto", "Berlin", "Leeds", "Lyon", "Perth"]
COUNTRIES = ["PT", "US", "IN", "JP", "NG", "CA", "DE", "GB", "FR", "AU"]
WORDS = ["alpha", "bravo", "cedar", "delta", "ember", "fjord", "garnet", "harbor", "iris", "juniper", "kelp",
         "lumen", "meadow", "nova", "onyx", "pine", "quartz", "river", "sable", "tundra"]


class GenerationError(ValueError):
    pass


def topological_order(spec: FixtureSpec) -> list[TableSpec]:
    """Parents before children (self references are allowed and filled from earlier rows)."""
    names = {t.name for t in spec.tables}
    deps = {t.name: {p for _, p, _ in t.foreign_keys if p in names and p != t.name} for t in spec.tables}
    out: list[str] = []
    while deps:
        ready = sorted(n for n, d in deps.items() if not d - set(out))
        if not ready:
            raise GenerationError("foreign keys form a cycle between " + ", ".join(sorted(deps)))
        out += ready
        for n in ready:
            deps.pop(n)
    return [spec.table(n) for n in out]  # type: ignore[misc]


def _kind(c: ColumnSpec) -> str:
    t = (c.type or "").lower()
    if re.search(r"bool", t):
        return "bool"
    if re.search(r"timestamp|datetime", t):
        return "timestamp"
    if re.search(r"\bdate\b|^date", t):
        return "date"
    if re.search(r"int|serial", t):
        return "int"
    if re.search(r"num|dec|float|double|real|money", t):
        return "decimal"
    return "text"


def _scale(t: str) -> int:
    m = re.search(r"\(\s*\d+\s*,\s*(\d+)\s*\)", t or "")
    return int(m.group(1)) if m else 2


def _as_date(v: Any) -> dt.date | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


class _Gen:
    def __init__(self, spec: FixtureSpec):
        self.spec = spec
        self.rnd = random.Random(spec.seed)
        self.data: dict[str, list[dict]] = {}
        self.seen: dict[tuple[str, str], set] = {}

    def value(self, t: TableSpec, c: ColumnSpec, i: int) -> Any:
        r = self.rnd
        n = c.name.lower()
        if c.values:
            return r.choices(c.values, weights=c.weights)[0] if c.weights else r.choice(c.values)
        kind = _kind(c)
        gen = c.generator or ""
        if gen == "email" or (kind == "text" and "email" in n):
            return "user" + str(i + 1) + "." + str(r.randint(100, 999)) + "@example.test"
        if gen == "first_name" or n == "first_name":
            return r.choice(FIRST)
        if gen == "last_name" or n == "last_name":
            return r.choice(LAST)
        if gen == "name" or n in ("name", "full_name", "display_name", "customer_name"):
            return r.choice(FIRST) + " " + r.choice(LAST)
        if gen == "phone" or "phone" in n:
            return "+1-555-01" + format(r.randint(0, 99), "02d")
        if gen == "city" or n.endswith("city"):
            return r.choice(CITIES)
        if gen == "country" or n.endswith("country") or n == "country_code":
            return r.choice(COUNTRIES)
        if kind == "bool":
            return r.random() < 0.5
        if kind in ("date", "timestamp"):
            lo = _as_date(c.min) or dt.date(2024, 1, 1)
            hi = _as_date(c.max) or dt.date(2025, 12, 31)
            if hi < lo:
                hi = lo
            d = lo + dt.timedelta(days=r.randint(0, (hi - lo).days))
            if kind == "date":
                return d
            return dt.datetime(d.year, d.month, d.day, r.randint(0, 23), r.randint(0, 59), r.randint(0, 59))
        if kind == "int":
            lo = int(c.min) if c.min is not None else (1 if re.search(r"qty|quantity|count|number", n) else 0)
            hi = int(c.max) if c.max is not None else max(lo + 1, 10 if re.search(r"qty|quantity", n) else 1000)
            return r.randint(lo, hi)
        if kind == "decimal":
            lo = float(c.min) if c.min is not None else (0.0 if not re.search(r"pct|percent|rate", n) else 0.0)
            hi = float(c.max) if c.max is not None else (50.0 if re.search(r"pct|percent", n) else 500.0)
            return Decimal(str(round(r.uniform(lo, hi), _scale(c.type))))
        if gen == "code" or re.search(r"(^|_)(code|sku)$", n):
            return "".join(r.choices(string.ascii_uppercase, k=3)) + "-" + str(r.randint(1000, 9999))
        return r.choice(WORDS) + "-" + str(r.randint(1, 9999))

    def key(self, t: TableSpec, c: ColumnSpec, i: int) -> Any:
        k = _kind(c)
        if k == "int":
            return i + 1
        if k == "text":
            return t.name.split(".")[-1][:3].upper() + "-" + format(i + 1, "06d")
        return self.value(t, c, i)

    def unique_value(self, t: TableSpec, c: ColumnSpec, i: int) -> Any:
        seen = self.seen.setdefault((t.name, c.name), set())
        for attempt in range(50):
            v = self.value(t, c, i + attempt * 7919)
            if v not in seen:
                seen.add(v)
                return v
        raise GenerationError("could not keep " + t.name + "." + c.name + " unique; widen its range or values")

    def table(self, t: TableSpec) -> list[dict]:
        rows: list[dict] = []
        fk_cols = {cols[0]: (parent, refs[0]) for cols, parent, refs in t.foreign_keys if len(cols) == 1}
        composite = [(cols, parent, refs) for cols, parent, refs in t.foreign_keys if len(cols) > 1]
        single_pk = t.primary_key[0] if len(t.primary_key) == 1 else None
        for i in range(t.rows):
            row: dict = {}
            for cols, parent, refs in composite:  # a composite key picks one parent row for all its columns
                prow = self.rnd.choice(self.parent_rows(parent, t.name))
                for a, b in zip(cols, refs):
                    row[a] = prow[b]
            for c in t.columns:
                if c.name in row:
                    continue
                if c.name == single_pk:
                    row[c.name] = self.key(t, c, i)
                elif c.name in fk_cols:
                    parent, ref = fk_cols[c.name]
                    if parent == t.name:  # self reference: an earlier row, or null for the first ones
                        row[c.name] = self.rnd.choice(rows)[ref] if rows and (not c.nullable or
                                                                              self.rnd.random() > 0.3) else None
                    elif c.nullable and c.null_share and self.rnd.random() < c.null_share:
                        row[c.name] = None
                    else:
                        row[c.name] = self.rnd.choice(self.parent_rows(parent, t.name))[ref]
                elif c.nullable and c.null_share and self.rnd.random() < c.null_share:
                    row[c.name] = None
                elif c.unique:
                    row[c.name] = self.unique_value(t, c, i)
                else:
                    row[c.name] = self.value(t, c, i)
            rows.append(row)
        if len(t.primary_key) > 1:  # composite key: drop repeats so the key holds
            seen, kept = set(), []
            for row in rows:
                k = tuple(row.get(c) for c in t.primary_key)
                if k not in seen:
                    seen.add(k)
                    kept.append(row)
            rows = kept
        for cols in t.unique:
            if len(cols) > 1:
                seen, kept = set(), []
                for row in rows:
                    k = tuple(row.get(c) for c in cols)
                    if k not in seen:
                        seen.add(k)
                        kept.append(row)
                rows = kept
        return rows

    def parent_rows(self, parent: str, child: str) -> list[dict]:
        rows = self.data.get(parent)
        if not rows:
            raise GenerationError(child + " references " + parent + ", which has no generated rows; add it to the "
                                  "spec with rows > 0")
        return rows

    def scenarios(self) -> list[dict]:
        applied = []
        for s in self.spec.scenarios:
            t = self.spec.table(s.table)
            rows = self.data.get(s.table)
            if t is None or not rows:
                raise GenerationError("scenario " + (s.name or s.table) + ": no table " + s.table)
            n = s.count if s.count is not None else max(1, round(len(rows) * float(s.share or 0)))
            if s.duplicate:
                pk = t.primary_key
                uniq = {c.name for c in t.columns if c.unique}
                nxt = max((r[pk[0]] for r in rows if isinstance(r.get(pk[0]) if pk else None, int)), default=len(rows))
                for k in range(n):
                    src = dict(self.rnd.choice(rows))
                    if len(pk) == 1 and isinstance(src.get(pk[0]), int):
                        nxt += 1
                        src[pk[0]] = nxt
                    for u in uniq - set(pk):
                        src[u] = self.unique_value(t, t.column(u), len(rows) + k)  # type: ignore[arg-type]
                    src.update(s.set)
                    rows.append(src)
            else:
                for row in self.rnd.sample(rows, min(n, len(rows))):
                    row.update(s.set)
            applied.append({"name": s.name or s.table, "table": s.table, "rows": n, "duplicate": s.duplicate})
        return applied

    def rules(self) -> None:
        for rule in self.spec.rules:
            rows = self.data.get(rule.get("table", ""), [])
            if rule.get("type") == "after":
                col, after = rule["column"], rule["after"]
                for r in rows:
                    a, b = r.get(after), r.get(col)
                    if a is not None and b is not None and b < a:
                        gap = dt.timedelta(days=self.rnd.randint(0, int(rule.get("maxDays", 10))))
                        r[col] = a + gap
            elif rule.get("type") == "sum":
                child_t, child_c = rule["from"].rsplit(".", 1)
                key = rule["key"]
                pk = (self.spec.table(rule["table"]).primary_key or [key])[0]  # type: ignore[union-attr]
                totals: dict = {}
                for cr in self.data.get(child_t, []):
                    if cr.get(child_c) is not None:
                        totals[cr.get(key)] = totals.get(cr.get(key), Decimal(0)) + Decimal(str(cr[child_c]))
                for r in rows:
                    r[rule["column"]] = totals.get(r.get(pk), Decimal("0.00"))


def generate(spec: FixtureSpec) -> tuple[dict[str, list[dict]], dict]:
    """The rows per table and a generation report (order, counts, scenarios applied)."""
    g = _Gen(spec)
    order = topological_order(spec)
    for t in order:
        g.data[t.name] = g.table(t)
    applied = g.scenarios()
    g.rules()
    return g.data, {"order": [t.name for t in order], "rows": {k: len(v) for k, v in g.data.items()},
                    "scenarios": applied, "seed": spec.seed, "spec": spec.digest()}
