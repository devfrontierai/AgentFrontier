# AgentFrontier for Python

AgentFrontier is an open-source, configuration-driven data source SDK. It reads the **metadata** of a data source
(databases, schemas, tables, columns, keys, indexes, lineage, grants, policies, statistics) and, on request, sample
rows and read-only queries, and returns everything as one canonical record model.

There is no per-source code. A source is a JSON **definition** (SQL statements, HTTP requests or file operations plus
mappings); a small engine per **protocol** (`sql`, `http`, `files`) runs it. Adding a source means adding a JSON
file.

This is the **Python edition**. It is a faithful port of the Java reference implementation and shares the same
language-neutral spec, so up to the raw-stream layer both produce the same records for the same source: the 16
conformance cases (2 SQLite, 6 files, 8 HTTP) and the shared `eventId`, signing, CSV and translator vectors pass
unchanged in both, and on the test databases the Python and Java CLIs write byte-identical JSON Lines files.
Governance, graphs, the cache and provenance contracts and the two extensions are Python only.

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
| `parquet` | pyarrow: the parquet sink and Parquet schemas in the files engine |
| `lineage` | sqlglot: column lineage parsed from view SQL |
| `otel` | opentelemetry-api: spans for runs, scopes and operations |
| `kafka` | kafka-python: the kafka sink |
| `yaml` | PyYAML: governance rules files written in YAML |

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

## Raw-stream events

With the envelope on, every record becomes a raw-stream event that says which tenant, source system, connector and
run it came from, its family and category (Discovery & Structure / metadata, Traceability & Evidence / lineage, ...),
a stable `nativeObjectId` and a deterministic `eventId`. Re-reading an unchanged object gives the same `eventId`, so
consumers can deduplicate and replay safely.

```python
report = af.run(PipelineConfig(pg, aspects=["metadata", "lineage"], sinks=[{"type": "jsonl", "path": "out/shop"}],
                               envelope=True, tenant_id="acme"))
```

```bash
agentfrontier extract shop.json --aspects metadata,lineage --sink jsonl:out/shop --envelope --tenant acme
agentfrontier capabilities postgresql      # what a connector can read: entity, aspect, raw-stream category, limits
```

The envelope is off by default (switching the default is still open; it is needed for incremental runs). The rules
are in the spec's [docs/envelope.md](https://github.com/devfrontierai/agentfrontier-spec/blob/main/docs/envelope.md).

## Incremental runs

A pipeline with a checkpoint folder emits only what changed since its last run: new and changed objects
(`op: upsert`), deletions the source reports (`op: delete`) and objects that disappeared (`op: tombstone`). Query
histories and delta APIs read only from the saved watermark or delta token; everything else is compared with the
previous run. A failing scope never deletes what it could not read, and the checkpoint is committed only after a
run that did not fail, so replaying gives the same events.

```bash
agentfrontier extract shop.json --aspects metadata,lineage,usage --sink jsonl:out/shop --incremental state/ --tenant acme
agentfrontier extract shop.json --sink csv:out/csv --partition-by tenant,source,category --incremental state/ --full
```

```python
af.run(PipelineConfig(pg, aspects=["metadata"], sinks=[{"type": "csv", "path": "out"}], envelope=True,
                      incremental={"checkpoints": "state/", "reconcileEvery": 7}, run_health=True))
```

`run_health` ends the run with `quality_result` records about the run (status, duration, errors, retries, the lag of
incremental history); `lineage={"parse": True}` adds column lineage parsed from view SQL (the `lineage` extra). The
rules are in the spec's `docs/incremental.md`, `observability.md` and `lineage.md`.

## Object storage and cloud credentials

The files engine reads S3 (and S3-compatible stores such as MinIO), Azure Data Lake Storage Gen2 and Google Cloud
Storage as well as local folders: definitions `s3`, `adls`, `gcs`. Fields are inferred from CSV, TSV, JSON, JSON
Lines and Avro (and Parquet with the `parquet` extra) with ranged reads; object tags, metadata and ACLs become tags
and grants. The `aws_iam` (SigV4, STS AssumeRole), `azure_service_principal`, `azure_managed_identity` and
`gcp_service_account` auth methods work in every HTTP definition too, with the standard library only. New
definitions `bigquery` and `databricks` read the two platforms over REST, and `snowflake` reads its query history.

## Governance

Every read is decided by a policy before it runs, and every record before it is released: deny rules, masking by
field or classification label, row filters, approvals, and a fail-closed default for high-risk actions. Fields
are labelled by built-in classifiers (PII, payment cards, IBANs, health data, credentials) without touching the
source's own labels. The authority graph explains who can access what and through which group or delegation,
and runs can write a hash-chained, verifiable audit trail.

```bash
agentfrontier extract shop.json --aspects metadata,data --sink jsonl:out/shop \
    --policy rules.json --subject alice --purpose analytics --classify --audit
agentfrontier audit-verify out/shop
agentfrontier authority out/acl --principal alice
```

Without a policy nothing changes. See [docs/governance.md](docs/governance.md); the contracts are in the spec's
`docs/policy.md`, `classification.md`, `authority.md` and `audit.md`.

## Graphs and standard formats

A run can build a graph (assets, lineage, authority, quality, audit) in memory or in a JSON file, and every
traversal goes through an access filter, so a node the caller may not see hides what lies behind it too. The same
run can be published in the formats other tools read: OpenLineage (Marquez), OpenMetadata, DataHub, OPA data, OTLP
logs, FOCUS and OCSF, or streamed to Kafka.

```bash
agentfrontier extract shop.json --aspects metadata,lineage --parse-lineage --envelope \
    --sink openlineage.json --sink graph.json
```

with `openlineage.json` `{"type": "openlineage", "url": "http://marquez:5000"}` and `graph.json`
`{"type": "graph", "store": "json", "path": "out/graph.json"}`. The `dbt` definition reads a dbt project's
`manifest.json`: models and sources, column docs, the model DAG as lineage, metrics and business terms. The rules are
in the spec's `docs/graph.md` and `docs/translators.md`.

## Cache, provenance and extensions

* **Semantic cache contract** (`agentfrontier.cache`): cache keys over the kind (`result`, `context`, `artifact`),
  the scope (tenant, principal, purpose, source, and the schema, policy, model and embedding versions) and the
  normalised request; every hit is revalidated before it is served; change and policy events invalidate entries.
  `SemanticCache` works over any `CacheStore`; `MemoryCacheStore` is the in-memory reference store.
* **Provenance manifests** (`agentfrontier.provenance`): `Manifest` records a generated artifact's hashed inputs,
  outputs, tools, model, validation, approvals and limitations, with a digest that approvals do not change.
* **[agentfrontier-capabilities](extensions/capabilities/README.md)** (`extensions/capabilities`, own
  distribution): LLM provider clients, SQL validation, NL-to-SQL, data-quality rules, dbt generation, business term
  proposals, notebooks and capability profiles.
* **[agentfrontier-synth](extensions/synth/README.md)** (`extensions/synth`, own distribution, standard library
  only): synthetic data from a schema with valid foreign keys, masking and tokenization operators, privacy and
  utility checks, and fixture export.

## The spec

The definitions, the canonical entity model (`entities.json`), the raw-stream catalog (`streams.json`), the JSON
Schemas, the auth catalog, the sink mappings and the conformance cases form the **AgentFrontier spec**. It lives in
[agentfrontier-spec](https://github.com/devfrontierai/agentfrontier-spec) and is shared by every implementation.
This repository vendors a copy pinned to a tag (`scripts/spec.lock`):

* `src/agentfrontier/spec/` holds the definitions, `entities.json`, `streams.json`, `schemas/`,
  `classification/`, `auth-methods.json`, `index.json` and `sinks/` as package data;
* `tests/conformance/` holds the conformance cases (`expected.jsonl` and `expected-envelope.jsonl` files are the
  contract and are never regenerated here);
* `python scripts/sync_spec.py ../agentfrontier-spec --ref spec-vX.Y.Z` refreshes both from a spec tag (`--check`
  only reports differences). Never edit the copies by hand.

## Status per definition

| Definition | Protocol | Spec status | Python edition |
|---|---|---|---|
| `postgresql` | sql (psycopg) | verified | Verified on PostgreSQL 9.6, 11, 14 and 17 (integration tests; output byte-identical to Java) |
| `mysql` | sql (PyMySQL) | verified | Verified on MySQL 8.4 |
| `oracle` | sql (python-oracledb) | verified | Verified on Oracle 23 Free |
| `sqlite` | sql (sqlite3) | verified | Conformance case `sqlite-basic` |
| `local_files` | files | verified | Conformance cases `files-basic`, `files-incremental` |
| `s3` | files (S3 API, SigV4) | experimental | Conformance case `files-s3` (fixture server, STS AssumeRole) |
| `adls` | files (ADLS Gen2 API) | experimental | Conformance case `files-adls` (fixture server, Entra ID token) |
| `gcs` | files (JSON API) | experimental | Conformance case `files-gcs` (fixture server, service account JWT) |
| `bigquery` | http | experimental | Conformance case `http-bigquery` (fixture server, incremental job history) |
| `databricks` | http | experimental | Conformance case `http-databricks` (fixture server) |
| `dbt` | files (one JSON document) | experimental | Conformance case `files-dbt` (a dbt 1.9 manifest) |
| `elasticsearch` | http | experimental | Conformance case `http-elasticsearch` (fixture server) |
| `unity_catalog` | http | experimental | Conformance case `http-unity-catalog` (fixture server) |
| `entra_id` | http | experimental | Conformance case `http-entra-id` (fixture server) |
| `google_directory` | http | experimental | Conformance case `http-google-directory` (fixture server) |
| `okta` | http | experimental | Conformance case `http-okta` (fixture server) |
| `opensearch` | http | verified | Same engine path as Elasticsearch; not run against a server here |
| `redshift` | sql (psycopg) | experimental | Not run against a cluster (neither is the Java edition) |
| `snowflake` | sql (snowflake-connector-python) | experimental | Not run; connection block present; query history is a watermark operation |
| `sqlserver` | sql (pymssql) | experimental | Not run; the Python block supports `username_password` and `windows_integrated` only |

## Not implemented (yet)

* Cedar, Presidio, Tika, Purview, Macie and Google Sensitive Data Protection adapters; consent and retention
  readers.
* Live verification of Snowflake, SQL Server, Redshift, BigQuery, Databricks, S3, ADLS and GCS (they run against
  recorded responses only).
* `etag`, `lsn` and `change_feed` incremental modes; usage from PostgreSQL (`pg_stat_statements` has no history).
* Graph queries answered by Neo4j (the neo4j sink writes the same model), Neptune and Cosmos stores; the object
  storage writer and the Snowflake, Databricks and BigQuery table loaders; live checks of the OpenMetadata and
  DataHub sinks (they run against fixtures; OpenLineage and Kafka were checked against Marquez and Redpanda).
* Parquet schemas without the `parquet` extra (such files are datasets without fields, as in the Java edition).
* Lineage parsed from query history (only view SQL is parsed); business terms from Snowflake semantic views, Unity
  Catalog tags and the OpenMetadata glossary (the `dbt` definition reads dbt's).
* Phase 5 remainders: DS transforms (pandas, scikit-learn); PySpark, script and OpenAPI-client generation (code
  generation covers dbt only); the live NL-to-SQL accuracy run on the labelled retail set.
* Phase 6: cloud cost sources and LLM usage sources normalised to FOCUS, agent registry and permission sources, and
  the actions (write) aspect. Only `agentfrontier-synth` is built.
* `--drivers` is a Java option (JDBC jars); the Python CLI accepts it and ignores it.
* The ad hoc query row cap is applied while fetching; psycopg and PyMySQL client cursors still receive the whole
  result from the server.

## Documentation

* [docs/architecture.md](docs/architecture.md): how the SDK is built, the editions and the extension points
* [docs/features.md](docs/features.md): the full feature set, per edition, with verification status
* [extensions/capabilities](extensions/capabilities/README.md) and [extensions/synth](extensions/synth/README.md): the
  capabilities and synthetic-data distributions
* [examples/README.md](examples/README.md): three runnable use cases
* [docs/integration.md](docs/integration.md): add AgentFrontier to your project, integration patterns, production checklist
* [docs/getting-started.md](docs/getting-started.md): install, first connection, first extract
* [docs/python-api.md](docs/python-api.md): the library API
* [docs/governance.md](docs/governance.md): policies, masking, classification, the authority graph, audit
* [docs/cli.md](docs/cli.md): commands and options
* [docs/testing.md](docs/testing.md): the test suites and how to run them

## License

Apache License 2.0, see [LICENSE](LICENSE).
