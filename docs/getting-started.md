# Getting started

## 1. Install

```bash
python -m venv .venv
.venv/bin/pip install "agentfrontier[postgres]"      # Windows: .venv\Scripts\pip
```

The core package has no dependencies. Each database driver is an extra: `postgres` (psycopg 3), `mysql` (PyMySQL),
`oracle` (python-oracledb, thin mode), `snowflake`, `sqlserver` (pymssql), plus `neo4j` for the graph sink. SQLite,
the HTTP sources (Elasticsearch, OpenSearch, Unity Catalog) and local files need nothing extra.

If a driver is missing, opening a connection fails with `UNSUPPORTED` and names the package to install.

## 2. Describe a connection

A connection is a small JSON file. The same file works with the Java edition.

```json
{
  "id": "shop",
  "name": "Shop database",
  "type": "postgresql",
  "properties": {"host": "localhost", "port": 5432, "database": "shopdb"},
  "auth": {"method": "username_password", "username": "reader", "password": "${env:SHOP_PASSWORD}"},
  "options": {"includeSchemas": ["shop", "audit"]}
}
```

* `type` is a definition id or alias (`agentfrontier definitions` lists them; `agentfrontier describe postgresql`
  shows its properties, auth methods, hierarchy and operations).
* `properties` are the definition's connection properties; missing ones take the definition's defaults.
* `auth.method` is one of the definition's auth methods; the other keys are that method's fields. `auth` may also be
  written `{"type": "...", "values": {...}}`.
* Values may be secret references: `${env:NAME}` (environment variable) or `${file:/path}` (file content, trailing
  newline removed). They are resolved when a session opens and never printed.
* `options` are filters and switches: `include<Level>s` / `exclude<Level>s` (for example `includeSchemas`),
  `includeDatasets` / `excludeDatasets` (wildcards `*` and `?`), plus whatever the definition declares.

Check it without connecting:

```bash
agentfrontier validate shop.json
```

## 3. Test it

```bash
export SHOP_PASSWORD=...
agentfrontier test shop.json
```

The result shows the detected product, version and variant and what the definition's test operation returned
(current login, database). `latencyMs` is the total time, connect and version detection included.

## 4. Extract

```bash
agentfrontier extract shop.json --aspects metadata,lineage,governance --sink jsonl:out/shop
```

`out/shop` then holds one `<entity>.jsonl` file per entity (`source`, `container`, `dataset`, `field`,
`constraint`, `index`, `lineage`, `grant`, ...) and `run.json` with the run report. Aspects are `metadata`,
`lineage`, `governance`, `statistics`, `usage` and `data` (sample rows).

For repeatable jobs write a pipeline file and run it with `agentfrontier run job.json`:

```json
{
  "name": "shop-catalog",
  "source": "shop.json",
  "aspects": ["metadata", "lineage", "governance"],
  "scope": {"includeSchemas": ["shop"]},
  "sinks": [{"type": "jsonl", "path": "out/shop"}],
  "settings": {"pageSize": 500, "batchSize": 200, "parallelism": 2, "sampleRows": 10}
}
```

## 5. Use it from Python

```python
from agentfrontier import AgentFrontier, ConnectionConfig, Scope

af = AgentFrontier()
conn = ConnectionConfig.from_file("shop.json")
with af.open(conn) as s:
    scope = Scope.of("database", "shopdb", "schema", "shop")
    tables = s.datasets(scope).records
    fields = s.records("field", scope, [t.string("name") for t in tables])
```

Continue with [python-api.md](python-api.md).

## Adding your own definitions

Put definition files (or overlay files) in a folder and pass it with `--definitions <dir>` on the CLI,
`AgentFrontier(definitions=["dir"])` in code, or `AGENTFRONTIER_DEFINITIONS=dir1:dir2` with
`AgentFrontier.from_environment()`. A file with the id of a built-in definition replaces it; a file with
`"extends": "postgresql"` inherits from it; a file with `"overlay": "postgresql"` and a `when` patches it for some
versions or variants. SQL definitions need a `connection.python` block (driver module, `connect` arguments and per
auth method the arguments or a `dsn`) to run from Python.
