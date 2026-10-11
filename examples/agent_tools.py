"""Use case 3 (part 1): safe, read-only data source tools for AI agents.

``DataSourceTools`` wraps one AgentFrontier connection as four tools an LLM can call:

    list_datasets      tables, views and files, with their type, description and row count
    describe_dataset   columns, keys, indexes and foreign keys of one dataset
    sample_rows        the first rows of one dataset
    run_query          one read-only SQL statement (writes, DDL and dangerous functions are rejected by the SDK)

The class is framework-agnostic: every method takes and returns plain JSON-friendly values. ``mcp_server.py``
exposes it over the Model Context Protocol (Claude Desktop, Claude Code, Cursor, ...) and ``claude_agent.py`` hands
it to Claude through the Anthropic SDK. Wrapping it for LangChain, LlamaIndex or your own agent loop is a few lines.

Try it without an LLM:

    python examples/agent_tools.py                     # the bundled sample database
    python examples/agent_tools.py my-postgres.json    # your own source
"""

from __future__ import annotations

import json
import sys
from typing import Any

from agentfrontier import AgentFrontier, AgentFrontierError, QueryRequest, Scope

from common import levels, load_connection, qualified, ref_target

MAX_ROWS = 200        # hard cap on rows returned to the model, whatever it asks for
MAX_CELL_CHARS = 500  # long text values are shortened so one wide row cannot flood the context


class DataSourceTools:
    def __init__(self, connection, af: AgentFrontier | None = None, allow_queries: bool = True):
        self.af = af or AgentFrontier()
        self.connection = connection
        self.levels = levels(self.af, connection)
        self.allow_queries = allow_queries

    # --- tools ----------------------------------------------------------------------------------------------

    def list_datasets(self, filter: str = "") -> dict:
        """Every dataset (table, view, file, index, ...) with type, description and row count when known."""
        out = []
        with self.af.open(self.connection) as s:
            for scope in self._leaf_scopes(s, Scope.root()):
                cursor = None
                while True:
                    page = s.datasets(scope, cursor, 500)
                    for d in page.records:
                        name = qualified(d, self.levels, "name")
                        if filter.lower() in name.lower():
                            out.append({k: v for k, v in {
                                "name": name, "type": d.string("type"), "description": d.string("comment"),
                                "rows": d.long_value("rowCount")}.items() if v is not None})
                    cursor = page.next_cursor
                    if cursor is None or len(out) >= 1000:
                        break
        return {"source": self.connection.id or self.connection.type, "datasets": out[:1000],
                "nameFormat": ".".join(self.levels + ["dataset"])}

    def describe_dataset(self, name: str) -> dict:
        """Columns (type, nullability, default, description), primary/unique/foreign keys and indexes."""
        scope, dataset = self._split(name)
        with self.af.open(self.connection) as s:
            fields = s.records("field", scope, [dataset])
            if not fields:
                return {"error": "NOT_FOUND", "message": f"no dataset {name!r}; call list_datasets for valid names"}
            keys = s.records("constraint", scope, [dataset]) if s.supports("constraint") else []
            indexes = s.records("index", scope, [dataset]) if s.supports("index") else []
        return {
            "name": name,
            "columns": [{k: v for k, v in {
                "name": f.string("name"), "type": f.string("dataType"), "nullable": f.bool("nullable"),
                "default": f.string("defaultValue"), "description": f.string("comment")}.items() if v is not None}
                for f in sorted(fields, key=lambda f: f.int_value("ordinal") or 0)],
            "keys": [{k: v for k, v in {
                "type": c.string("type"), "columns": c.strings("columns"),
                "references": ref_target(c, self.levels) if c.string("refDataset") else None,
                "referencedColumns": c.strings("refColumns") or None}.items() if v} for c in keys],
            "indexes": [{"columns": i.strings("columns"), "unique": i.bool("isUnique", False)} for i in indexes],
        }

    def sample_rows(self, name: str, limit: int = 5) -> dict:
        """The first rows of a dataset."""
        scope, dataset = self._split(name)
        with self.af.open(self.connection) as s:
            rows = s.sample(scope, dataset, max(1, min(limit, MAX_ROWS)))
        return {"name": name, "rows": [{k: _cell(v) for k, v in r.values.items()} for r in rows]}

    def run_query(self, sql: str, max_rows: int = 50, scope: str = "") -> dict:
        """Run one read-only SQL statement and return the rows as objects."""
        if not self.allow_queries:
            return {"error": "DISABLED", "message": "ad hoc queries are disabled for this source"}
        request = QueryRequest(sql, scope=self._scope(scope.split(".") if scope else []),
                               max_rows=max(1, min(max_rows, MAX_ROWS)), timeout_seconds=30)
        with self.af.open(self.connection) as s:
            result = s.query(request)
        names = [c.name for c in result.columns]
        return {"columns": [{"name": c.name, "type": c.type_name} for c in result.columns],
                "rows": [dict(zip(names, map(_cell, row))) for row in result.rows],
                "truncated": result.truncated, "elapsedMs": result.elapsed_ms}

    def call(self, tool: str, arguments: dict) -> dict:
        """Dispatch a tool call; SDK errors become a result the model can read and recover from."""
        try:
            return getattr(self, tool)(**arguments)
        except AgentFrontierError as e:
            return {"error": e.code, "message": str(e)}

    # --- helpers --------------------------------------------------------------------------------------------

    def _leaf_scopes(self, s, scope: Scope):
        if scope.depth >= len(s.hierarchy):
            yield scope
            return
        for c in s.containers(scope):
            yield from self._leaf_scopes(s, scope.with_(c.string("level"), c.string("name")))

    def _scope(self, values: list[str]) -> Scope:
        scope = Scope.root()
        for level, value in zip(self.levels, values):
            scope = scope.with_(level, value)
        return scope

    def _split(self, name: str) -> tuple[Scope, str]:
        """``database.schema.table`` -> (scope, table); the dataset part may itself contain dots."""
        parts = name.split(".", len(self.levels))
        if len(parts) <= len(self.levels):
            raise AgentFrontierError.invalid_config(
                f"dataset names look like {'.'.join(self.levels + ['dataset'])}; call list_datasets for valid names")
        return self._scope(parts[:-1]), parts[-1]


def _cell(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + f"... ({len(value)} chars)"
    return value


# Tool descriptions shared by the MCP server and the Claude agent. Good descriptions matter more than anything else
# for how well a model uses tools.
DESCRIPTIONS = {
    "list_datasets": "List the datasets (tables, views, files) in the connected data source, with type, description "
                     "and row count where known. Call this first to learn the exact dataset names. Optional `filter` "
                     "is a case-insensitive substring of the name.",
    "describe_dataset": "Describe one dataset: its columns (type, nullability, default, description), primary, unique "
                        "and foreign keys, and indexes. `name` is a full name exactly as returned by list_datasets.",
    "sample_rows": "Return the first `limit` rows (default 5, at most 200) of one dataset to see what the data looks "
                   "like. `name` is a full name exactly as returned by list_datasets.",
    "run_query": "Run ONE read-only SQL statement (SELECT / WITH) against the data source and return the rows. Writes, "
                 "DDL and multiple statements are rejected. Refer to tables by the full names list_datasets returns. "
                 "Use the SQL dialect of the source. Aggregate in SQL rather than fetching many rows; `max_rows` "
                 "defaults to 50, at most 200.",
}


if __name__ == "__main__":
    tools = DataSourceTools(load_connection(sys.argv[1] if len(sys.argv) > 1 else None))
    catalog = tools.call("list_datasets", {})
    print(json.dumps(catalog, indent=2))
    first = next((d["name"] for d in catalog.get("datasets", []) if d.get("type") != "view"), None)
    if first:
        print(json.dumps(tools.call("describe_dataset", {"name": first}), indent=2))
        print(json.dumps(tools.call("sample_rows", {"name": first, "limit": 3}), indent=2))
    print(json.dumps(tools.call("run_query", {"sql": "delete from customers"}), indent=2))
