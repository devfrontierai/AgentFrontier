"""What to generate: tables, their columns and constraints, row counts, scenarios and cross-column rules.

A :class:`FixtureSpec` is plain data (:meth:`FixtureSpec.from_json` / :meth:`to_json`), so a product can build it from
its catalog, store it with the fixture and generate the same data again from the same seed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ColumnSpec", "TableSpec", "Scenario", "FixtureSpec"]


@dataclass
class ColumnSpec:
    name: str
    type: str = "text"
    nullable: bool = True
    unique: bool = False
    values: list[Any] | None = None           # accepted values (weights optional)
    weights: list[float] | None = None
    min: Any = None
    max: Any = None
    null_share: float = 0.0                    # share of nulls when nullable
    generator: str | None = None               # name of a built-in generator, overriding the guess
    labels: list[str] = field(default_factory=list)  # classification labels (pii.email, ...)

    def to_json(self) -> dict:
        out: dict = {"name": self.name, "type": self.type, "nullable": self.nullable}
        for k in ("unique", "values", "weights", "min", "max", "null_share", "generator", "labels"):
            v = getattr(self, k)
            if v not in (None, [], False) and not (k == "null_share" and v == 0.0):
                out[k] = v
        return out


@dataclass
class TableSpec:
    name: str                                  # schema.table
    columns: list[ColumnSpec]
    rows: int = 100
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[tuple[list[str], str, list[str]]] = field(default_factory=list)
    unique: list[list[str]] = field(default_factory=list)

    def column(self, name: str) -> ColumnSpec | None:
        return next((c for c in self.columns if c.name == name), None)

    def to_json(self) -> dict:
        return {"name": self.name, "rows": self.rows, "primaryKey": self.primary_key,
                "foreignKeys": [{"columns": c, "references": p, "refColumns": r} for c, p, r in self.foreign_keys],
                "unique": self.unique, "columns": [c.to_json() for c in self.columns]}


@dataclass
class Scenario:
    """Rows that must exist: ``share`` (0-1) or ``count`` rows of ``table`` get ``set`` values, or are duplicates
    of other rows (every column but the key) when ``duplicate`` is true."""
    table: str
    name: str = ""
    share: float | None = None
    count: int | None = None
    set: dict = field(default_factory=dict)
    duplicate: bool = False

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, {}, False, "")}


@dataclass
class FixtureSpec:
    tables: list[TableSpec]
    seed: int = 42
    scenarios: list[Scenario] = field(default_factory=list)
    # cross-column rules: {"type": "after", "table", "column", "after"} keeps a date after another;
    # {"type": "sum", "table", "column", "from": "child.table.column", "key": "fk column"} totals children
    rules: list[dict] = field(default_factory=list)

    def table(self, name: str) -> TableSpec | None:
        return next((t for t in self.tables if t.name == name), None)

    def to_json(self) -> dict:
        return {"seed": self.seed, "tables": [t.to_json() for t in self.tables],
                "scenarios": [s.to_json() for s in self.scenarios], "rules": self.rules}

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(self.to_json(), sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def from_json(d: dict) -> "FixtureSpec":
        tables = []
        for t in d.get("tables") or []:
            cols = [ColumnSpec(**{k: v for k, v in c.items() if k in ColumnSpec.__dataclass_fields__})
                    for c in t.get("columns") or []]
            fks = [(list(f["columns"]), f["references"], list(f["refColumns"])) for f in t.get("foreignKeys") or []]
            tables.append(TableSpec(t["name"], cols, int(t.get("rows", 100)), list(t.get("primaryKey") or []), fks,
                                    [list(u) for u in t.get("unique") or []]))
        scen = [Scenario(**{k: v for k, v in s.items() if k in Scenario.__dataclass_fields__})
                for s in d.get("scenarios") or []]
        return FixtureSpec(tables, int(d.get("seed", 42)), scen, list(d.get("rules") or []))
