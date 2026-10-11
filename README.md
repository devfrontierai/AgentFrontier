# AgentFrontier for Python

AgentFrontier is an open-source, configuration-driven data source SDK. It reads the **metadata** of a data source
(databases, schemas, tables, columns, keys, indexes, lineage, grants, policies, statistics) and, on request, sample
rows and read-only queries, and returns everything as one canonical record model.

There is no per-source code. A source is a JSON **definition** (SQL statements, HTTP requests or file operations plus
mappings); a small engine per **protocol** (`sql`, `http`, `files`) runs it. Adding a source means adding a JSON
file.

This is the **Python edition**. It is a faithful port of the Java reference implementation and shares the same
language-neutral spec, so both produce the same records for the same source: the four conformance cases pass
unchanged, and on the test databases the Python and Java CLIs write byte-identical JSON Lines files.

* Python 3.10 or newer, **no required runtime dependencies** (standard library only).
* Database drivers are optional extras (DB-API 2.0 drivers: psycopg, PyMySQL, python-oracledb, ...).

## Try it in five minutes

No database needed: the examples bring a sample one.

```bash
git clone https://github.com/devfrontierai/AgentFrontier.git && cd AgentFrontier
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
python examples/data_dictionary.py                     # -> out/data-dictionary.md with an ER diagram
python examples/agent_tools.py                         # the read-only tools an AI agent would get
```

## What you can build

| Use case | What it does | Example |
|---|---|---|
| **Data dictionary** | Markdown docs with an ER diagram for any database, regenerated on every run | [`data_dictionary.py`](examples/data_dictionary.py) |
| **Schema drift check** | Fails CI when a column is dropped or retyped, before downstream jobs break | [`schema_drift.py`](examples/schema_drift.py) |
| **AI agent data access** | Read-only list / describe / sample / query tools, served over MCP or used with Claude | [`mcp_server.py`](examples/mcp_server.py), [`claude_agent.py`](examples/claude_agent.py) |

Each one runs unchanged against PostgreSQL, MySQL, Oracle, Snowflake, SQL Server, Unity Catalog, Elasticsearch or a
folder of files. Pass it a connection file. See [examples/README.md](examples/README.md).

## Install

AgentFrontier is not published on PyPI yet. Install it from GitHub, with the extras for the drivers you need:

```bash
pip install "agentfrontier @ git+https://github.com/devfrontierai/AgentFrontier.git"            # core: SQLite, HTTP sources, local files
pip install "agentfrontier[postgres] @ git+https://github.com/devfrontierai/AgentFrontier.git"  # + psycopg 3 (PostgreSQL, Redshift)
```

| Extra | Driver |
|---|---|
| `postgres` | psycopg 3 (PostgreSQL, Redshift) |
| `mysql` | PyMySQL |
| `oracle` | python-oracledb (thin mode, no Oracle client needed) |
| `snowflake` | snowflake-connector-python |
| `sqlserver` | pymssql |
| `neo4j` | the neo4j sink |

From a checkout: `pip install -e ".[dev,postgres,mysql,oracle,neo4j]"`. Once the package is on PyPI this becomes
`pip install "agentfrontier[postgres]"`. [docs/integration.md](docs/integration.md) covers pinning, connection
files, integration patterns and a production checklist.

## Quick start: library

```python
from agentfrontier import AgentFrontier, AuthConfig, ConnectionConfig, PipelineConfig, Scope

af = AgentFrontier()
pg = ConnectionConfig.from_json({
    "id": "shop", "type": "postgresql",
    "properties": {"host": "localhost", "port": 5432, "database": "shopdb"},
    "auth": {"method": "username_password", "username": "reader", "password": "${env:PG_PASSWORD}"},
})

print(af.test(pg))                                     # connect, detect the version, run the test operation

with af.open(pg) as s:                                 # the pull API
    for db in s.containers(Scope.root()):
        db_scope = Scope.of("database", db.string("name"))
        for schema in s.containers(db_scope):
            scope = db_scope.with_("schema", schema.string("name"))
            page = s.datasets(scope, None, 500)
            names = [r.string("name") for r in page.records]
            fields = s.records("field", scope, names)
            keys = s.records("constraint", scope, names)
    rows = s.query("select count(*) from shop.orders")  # read-only, guarded

report = af.run(PipelineConfig.of(pg, ["metadata", "lineage"],    # the push API: source to sinks
                                  [{"type": "jsonl", "path": "out/shop"}]))
print(report.status, report.counts)
```

## Quick start: CLI

```bash
agentfrontier definitions
agentfrontier test shop.json
agentfrontier extract shop.json --aspects metadata,lineage --sink jsonl:out/shop
agentfrontier query shop.json "select count(*) from shop.orders" --scope database=shopdb,schema=shop
```

`python -m agentfrontier ...` works the same way. See [docs/cli.md](docs/cli.md).

## The spec

The definitions, the canonical entity model (`entities.json`), the auth catalog, the sink mappings and the
conformance cases form the **AgentFrontier spec**. It is shared by every implementation and owned jointly: a change
to it is made in both repositories (later it moves to a repository of its own). In this repository:

* `src/agentfrontier/spec/` holds the definitions, `entities.json`, `auth-methods.json`, `index.json` and
  `sinks/` as package data;
* `tests/conformance/` holds the conformance cases (`expected.jsonl` files are the contract and are never
  regenerated here);
* `scripts/sync_spec.py <path-to-spec>` re-copies both from the shared spec (`--check` only reports differences).

## Status per definition

| Definition | Protocol | Spec status | Python edition |
|---|---|---|---|
| `postgresql` | sql (psycopg) | verified | Verified on PostgreSQL 9.6, 11, 14 and 17 (integration tests; output byte-identical to Java) |
| `mysql` | sql (PyMySQL) | verified | Verified on MySQL 8.4 |
| `oracle` | sql (python-oracledb) | verified | Verified on Oracle 23 Free |
| `sqlite` | sql (sqlite3) | verified | Conformance case `sqlite-basic` |
| `local_files` | files | verified | Conformance case `files-basic` |
| `elasticsearch` | http | experimental | Conformance case `http-elasticsearch` (fixture server) |
| `unity_catalog` | http | experimental | Conformance case `http-unity-catalog` (fixture server) |
| `opensearch` | http | verified | Same engine path as Elasticsearch; not run against a server here |
| `redshift` | sql (psycopg) | experimental | Not run against a cluster (neither is the Java edition) |
| `snowflake` | sql (snowflake-connector-python) | experimental | Not run; connection block present |
| `sqlserver` | sql (pymssql) | experimental | Not run; the Python block supports `username_password` and `windows_integrated` only |

## Not implemented (yet)

* Object storage for the files engine (S3, ADLS, GCS): only `storage: local`, as in the Java edition.
* Parquet / Avro schema inference: such files are listed as datasets but yield no fields (same as Java).
* `--drivers` is a Java option (JDBC jars); the Python CLI accepts it and ignores it.
* The ad hoc query row cap is applied while fetching; psycopg and PyMySQL client cursors still receive the whole
  result from the server.

## Documentation

* [examples/README.md](examples/README.md): three runnable use cases
* [docs/integration.md](docs/integration.md): add AgentFrontier to your project, integration patterns, production checklist
* [docs/getting-started.md](docs/getting-started.md): install, first connection, first extract
* [docs/python-api.md](docs/python-api.md): the library API
* [docs/cli.md](docs/cli.md): commands and options
* [docs/testing.md](docs/testing.md): the test suites and how to run them

## License

Apache License 2.0, see [LICENSE](LICENSE).
