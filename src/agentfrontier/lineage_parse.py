"""Column lineage parsed from view definitions (spec docs/lineage.md). Python only, with the ``lineage`` extra
(``sqlglot``)."""

from __future__ import annotations

from typing import Any

from . import json_util as J
from .model import Record

# AgentFrontier definition dialects to sqlglot dialects
_DIALECTS = {"postgresql": "postgres", "postgres": "postgres", "sqlserver": "tsql", "tsql": "tsql",
             "mysql": "mysql", "oracle": "oracle", "sqlite": "sqlite", "snowflake": "snowflake",
             "bigquery": "bigquery", "redshift": "redshift", "databricks": "databricks", "spark": "spark",
             "trino": "trino", "duckdb": "duckdb"}


class ParseUnavailable(Exception):
    """sqlglot is not installed."""


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


class LineageParser:
    """Parses one view at a time. ``schema`` maps a table's level values plus its name (a tuple) to its columns and
    types; it lets ``select *`` and unqualified columns resolve."""

    def __init__(self, dialect: str | None, levels: list[str], schema: dict[tuple, dict[str, str]] | None = None):
        try:
            import sqlglot  # noqa: F401
        except ImportError:
            raise ParseUnavailable("lineage.parse is on but sqlglot is not installed "
                                   "(pip install \"agentfrontier[lineage]\")") from None
        self.dialect = _DIALECTS.get((dialect or "").lower(), dialect or None)
        self.levels = list(levels)
        self.schema = schema or {}

    def _mapping(self) -> dict | None:
        """sqlglot's nested schema: {catalog: {db: {table: {column: type}}}} with up to the last two levels."""
        depth = min(len(self.levels), 2)
        if not self.schema:
            return None
        out: dict = {}
        for key, cols in self.schema.items():
            parts = [J.jstr(p) for p in key[len(key) - 1 - depth:]] if depth else [J.jstr(key[-1])]
            node = out
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = {c: (t or "text") for c, t in cols.items()}
        return out

    def view_lineage(self, view: Record) -> list[tuple[Record, float]]:
        """Lineage records of one view (with ``definition``), each with its confidence."""
        import sqlglot
        from sqlglot import exp
        from sqlglot.lineage import lineage

        sql = J.jstr(view.get("definition"))
        tree = sqlglot.parse_one(sql, read=self.dialect)
        if isinstance(tree, exp.Create):
            tree = tree.expression
        if not isinstance(tree, (exp.Query,)):
            return []
        values = view.values
        own = {lv: values.get(lv) for lv in self.levels}
        mapping = self._mapping()
        tables_in_query = {t.name for t in tree.find_all(exp.Table)}
        projections = {p.alias_or_name: p for p in getattr(tree, "selects", [])}
        out: list[tuple[Record, float]] = []
        seen: set[tuple] = set()
        for name in tree.named_selects:
            if not name or name == "*":
                continue
            try:
                node = lineage(name, tree, schema=mapping, dialect=self.dialect)
            except Exception:  # noqa: BLE001 - the schema may not know every table yet: try without it
                try:
                    node = lineage(name, tree, dialect=self.dialect)
                except Exception:  # noqa: BLE001 - a column that cannot be traced is left out
                    continue
            proj = projections.get(name)
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            identity = isinstance(inner, exp.Column)
            unqualified = proj is not None and any(not c.table for c in proj.find_all(exp.Column))
            for leaf in node.walk():
                if leaf.downstream:
                    continue
                table = leaf.source if isinstance(leaf.source, exp.Table) else None
                if table is None or not table.name:
                    continue
                column = leaf.name.rsplit(".", 1)[-1]
                if not column or column == "*":
                    continue
                up = self._levels_of(table, own)
                key = (name, tuple(up.items()), table.name, column)
                if key in seen:
                    continue
                seen.add(key)
                confidence = 0.9 if identity else 0.7
                if unqualified and len(tables_in_query) > 1 and mapping is None:
                    confidence = 0.5
                v: dict[str, Any] = {"source": values.get("source")}
                v.update({k: x for k, x in own.items() if x is not None})
                v["dataset"] = values.get("name")
                v["field"] = name
                for lv, x in up.items():
                    v["upstream" + _cap(lv)] = x
                v["upstreamDataset"] = table.name
                v["upstreamField"] = column
                v["kind"] = "view"
                v["transformation"] = "identity" if identity else "expression"
                out.append((Record("lineage", v), confidence))
        return out

    def _levels_of(self, table: Any, own: dict) -> dict:
        """Upstream level values: the parts the SQL names (catalog, db) on the last two levels, else the view's."""
        up = dict(own)
        named = [p for p in (table.catalog, table.db) if p]
        tail = self.levels[-len(named):] if named else []
        for lv, value in zip(tail, named):
            up[lv] = value
        return {k: v for k, v in up.items() if v is not None}


__all__ = ["LineageParser", "ParseUnavailable"]
