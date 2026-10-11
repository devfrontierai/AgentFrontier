# Integrating AgentFrontier into your project

This guide takes you from "never used it" to "running in production": try it in five minutes, install it into
your project, connect your source, pick one of four integration patterns, then work through the production checklist.

## 1. Try it in five minutes (no database needed)

```bash
git clone https://github.com/devfrontierai/AgentFrontier.git
cd AgentFrontier
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -e .

python examples/data_dictionary.py        # Markdown data dictionary of a sample database -> out/
python examples/schema_drift.py snapshot -o out/baseline.json
python examples/agent_tools.py            # the read-only tools an AI agent would get
agentfrontier definitions                 # every source you can connect to
```

The [examples](../examples/README.md) explain each use case and how to point it at your own data.

## 2. Install it in your project

Requirements: Python 3.10+. The core has **no dependencies**; database drivers are optional extras.

AgentFrontier is not on PyPI yet, so install it from GitHub and pin a tag or commit for reproducible builds:

```bash
pip install "agentfrontier @ git+https://github.com/devfrontierai/AgentFrontier.git@main"
pip install "agentfrontier[postgres] @ git+https://github.com/devfrontierai/AgentFrontier.git@main"
```

In `requirements.txt` or `pyproject.toml`:

```text
agentfrontier[postgres,mysql] @ git+https://github.com/devfrontierai/AgentFrontier.git@<tag-or-commit>
```

Once the package is published, this becomes `pip install "agentfrontier[postgres]"`.

| Extra | Adds | For |
|---|---|---|
| (none) | nothing | SQLite, Elasticsearch, OpenSearch, Unity Catalog, local files |
| `postgres` | psycopg 3 | PostgreSQL, Redshift |
| `mysql` | PyMySQL | MySQL |
| `oracle` | python-oracledb (thin mode, no Oracle client) | Oracle |
| `snowflake` | snowflake-connector-python | Snowflake |
| `sqlserver` | pymssql | SQL Server |
| `neo4j` | neo4j driver | the Neo4j graph sink |

When a driver is missing, opening a connection fails with error code `UNSUPPORTED` and names the package to install.

## 3. Connect your source

Keep one JSON file per source, next to your code or in your configuration repository:

```json
{
  "id": "warehouse",
  "name": "Analytics warehouse",
  "type": "postgresql",
  "properties": {"host": "db.internal", "port": 5432, "database": "analytics"},
  "auth": {"method": "username_password", "username": "af_reader", "password": "${env:WAREHOUSE_PASSWORD}"},
  "options": {"includeSchemas": ["sales", "finance"], "excludeDatasets": ["tmp_*"]}
}
```

`agentfrontier describe postgresql` lists a definition's properties, auth methods and options. Then check the file
in three steps, from cheapest to most complete:

```bash
agentfrontier validate warehouse.json      # offline: structure, required properties, auth method
agentfrontier test warehouse.json          # connects; prints product, version, current user, latency
agentfrontier extract warehouse.json --sink jsonl:out/warehouse   # full metadata extract to files
```

You can also build the connection in code without a file:

```python
from agentfrontier import AuthConfig, ConnectionConfig

conn = (ConnectionConfig.builder("postgresql").id("warehouse")
        .property("host", "db.internal").property("database", "analytics")
        .option("includeSchemas", ["sales"])
        .auth(AuthConfig.username_password("af_reader", "${env:WAREHOUSE_PASSWORD}"))
        .build())
```

**Use a least-privilege account.** AgentFrontier only reads, but the database user is your real security boundary:
grant catalog access plus `SELECT` only where you want sample rows or queries.

## 4. Pick an integration pattern

### A. Embed the library (pull API): your code asks for what it needs

Best for applications and services that browse metadata on demand: catalog UIs, query builders, agents.

```python
from agentfrontier import AgentFrontier, ConnectionConfig, QueryRequest, Scope

af = AgentFrontier()                                  # thread-safe: create one per process and share it
conn = ConnectionConfig.from_file("warehouse.json")

with af.open(conn) as s:                              # a session: not thread-safe, open one per thread
    for db in s.containers(Scope.root()):
        db_scope = Scope.of("database", db.string("name"))
        for schema in s.containers(db_scope):
            scope = db_scope.with_("schema", schema.string("name"))
            tables = s.datasets(scope, None, 500).records
            columns = s.records("field", scope, [t.string("name") for t in tables])
    result = s.query(QueryRequest("select count(*) from sales.orders", max_rows=10, timeout_seconds=30))
```

Walk `s.hierarchy` instead of hard-coding `database` / `schema` when one piece of code should serve several source
types (SQLite has no levels, Oracle has `schema`, Unity Catalog has `catalog` / `schema`). See
[`examples/agent_tools.py`](../examples/agent_tools.py) for a generic walk.

### B. Run pipelines (push API): send everything to a sink

Best for batch jobs: populate a catalog, feed a search index, snapshot schemas, build a knowledge graph.

```python
from agentfrontier import AgentFrontier, ConnectionConfig, PipelineConfig

af = AgentFrontier()
report = af.run(PipelineConfig(
    ConnectionConfig.from_file("warehouse.json"),
    aspects=["metadata", "lineage", "governance"],
    sinks=[{"type": "jsonl", "path": "out/warehouse"}],
    parallelism=4,
))
print(report.status, report.counts)                   # SUCCEEDED / PARTIAL / FAILED
```

Built-in sinks: `jsonl`, `json`, `console`, `memory` (read back in-process), `http` (POSTs batches to your
service), `neo4j`. To write to your own store, subclass `Sink`:

```python
from agentfrontier import AgentFrontier, Sink, SinkProvider

class CatalogSink(Sink):
    def __init__(self, db):
        self.db = db
    def write(self, records):                         # may be called from several threads
        for r in records:
            self.db.upsert(r.entity, r.to_json())     # every record is a flat JSON-friendly dict

class CatalogSinkProvider(SinkProvider):
    type = "catalog"
    def __init__(self, db):
        self.db = db
    def create(self, config, sdk):
        return CatalogSink(self.db)

af = AgentFrontier(sinks=[CatalogSinkProvider(my_db)])
af.run(PipelineConfig.of(conn, ["metadata"], [{"type": "catalog"}]))
```

Packages can also register sink providers through the `agentfrontier.sinks` entry point group, so they are
available without code changes.

### C. Use the CLI: no Python code

Best for cron jobs, CI steps, shell scripts and quick exploration. Results go to standard output as JSON. Exit code
is 0 on success, 1 on failure and 2 on bad usage.

```bash
agentfrontier run pipelines/nightly.json                     # pipeline file: source, aspects, scope, sinks
agentfrontier extract warehouse.json --aspects metadata --sink jsonl:/data/catalog/warehouse
agentfrontier query warehouse.json "select count(*) from sales.orders" --max-rows 10
```

`python -m agentfrontier` is the same command, and it helps where the generated `agentfrontier.exe` launcher is
blocked by application control.

### D. Give AI agents access

Expose a source to an agent as read-only tools: list, describe, sample, query. The SQL guard rejects writes no matter
what the model generates. [`examples/mcp_server.py`](../examples/mcp_server.py) serves any connection to MCP clients
(Claude Code, Claude Desktop, Cursor), and [`examples/claude_agent.py`](../examples/claude_agent.py) uses the same
tools with the Claude API.

## 5. Production checklist

* **One `AgentFrontier`, many sessions.** Share one `AgentFrontier` object (it caches definitions and version
  detection). Sessions hold a connection: open them with `with`, one per thread. Pipelines with `parallelism > 1`
  manage their own sessions.
* **Scope the extract.** Use `includeSchemas` / `excludeSchemas` / `includeDatasets` / `excludeDatasets` (wildcards
  `*` and `?`) so large warehouses only read what you need. Tune `page_size`, `batch_size` and `parallelism`.
* **Choose aspects deliberately.** `metadata` is the default. `data` reads sample rows (real values, possibly
  personal data), so enable it only where that is acceptable.
* **Secrets.** Write `${env:NAME}` or `${file:/run/secrets/name}` in connection files, never literal passwords.
  Values are resolved when a session opens and never appear in `str(connection)`, logs or error messages. Custom
  resolvers (a vault, a cloud secret manager) plug in via `AgentFrontier(secret_resolvers=[...])`.
* **Handle errors by code.** Every failure is an `AgentFrontierError` with a stable `code`: `INVALID_CONFIG`,
  `CONNECTION_FAILED`, `AUTH_FAILED`, `QUERY_FAILED`, `QUERY_REJECTED`, `UNSUPPORTED`, `SINK_FAILED`, ... Retry
  `CONNECTION_FAILED` with backoff (but note that a wrong password can also surface as `CONNECTION_FAILED`, so cap the
  retries); fix the configuration for the others. `af.test(conn)` never raises, so use it for health checks.
* **Check the run report.** `PARTIAL` means some scopes failed and the rest were written; `report.errors` and
  `report.warnings` say which. Alert on it instead of treating it as success.
* **Ad hoc queries** have a statement timeout and a row cap (`QueryRequest(timeout_seconds=..., max_rows=...)`), and
  their `?` parameters are bound, never concatenated.

## 6. Extend it

A source is a JSON definition, not code. Put definition files in a folder and load it with `--definitions <dir>`,
`AgentFrontier(definitions=["dir"])` or `AGENTFRONTIER_DEFINITIONS=dir1:dir2` with `AgentFrontier.from_environment()`:

* a file with `"extends": "postgresql"` creates a variant (for example a PostgreSQL-compatible product);
* a file with `"overlay": "postgresql"` and a `when` patches a definition for some versions or variants;
* a file with the id of a built-in definition replaces it;
* a new file adds a source, for an HTTP API, a database with a DB-API driver, or a file layout.

`agentfrontier validate my-definitions/*.json` checks them. See [getting-started.md](getting-started.md#adding-your-own-definitions)
and the definitions under `src/agentfrontier/spec/definitions/` for working models.

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `UNSUPPORTED ... (pip install "agentfrontier[postgres]")` | The driver extra is missing; install it as in section 2 |
| `INVALID_CONFIG` on `validate` | A required property or auth field is missing; `agentfrontier describe <type>` lists them |
| `AUTH_FAILED` / `CONNECTION_FAILED` | Wrong credentials, host or firewall; `agentfrontier test` shows the driver's message (without secrets) |
| `QUERY_REJECTED` | The statement is not a single read; this is by design |
| Fewer datasets than expected | `include*` / `exclude*` options, or the user lacks catalog privileges on those schemas |
| SQLite path not found | Relative `path` values are resolved against the working directory; use an absolute path |
| `agentfrontier.exe` blocked on Windows | Use `python -m agentfrontier` |

Questions, bugs and new definitions are welcome as GitHub issues and pull requests.
