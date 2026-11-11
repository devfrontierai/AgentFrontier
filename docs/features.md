# AgentFrontier feature set

What AgentFrontier can do as of SDK 0.4.0 and spec `spec-v1.5.0` (plan phases 0 to 3), plus the unreleased work in
[CHANGELOG.md](../CHANGELOG.md): the plan's phase 5 slice (the cache contract and provenance manifests in the core,
and `agentfrontier-capabilities` 0.1.0) and phase 6 synthetic data (`agentfrontier-synth` 0.1.0).
[architecture.md](architecture.md) describes how it is built, and the rules behind each feature are in the spec's
`docs/`.

**Editions.** "Both" means the Python and Java editions produce identical output. "Python" means Python only, by the
parity decision.

**Verification levels:**

* **Live:** checked against a real system.
* **Fixture:** a conformance case runs against recorded responses or a local fixture.
* **Not run:** the definition exists but has not run against a system or a fixture.

## 1. Sources

Each source is one JSON definition. There is no per-source code.

| Definition | Protocol | Reads | Verification |
|---|---|---|---|
| `postgresql` | sql | Databases, schemas, tables and views, columns, keys, indexes, routines, view lineage, roles, grants, policies, statistics. Variants: CockroachDB and Aurora; an older-version overlay (`postgresql.pre10.json`) | Live on 9.6, 11, 14 and 17 |
| `mysql` | sql | Databases, tables and views, columns, keys, indexes, routines, view lineage (from 8.0.13), users, grants. Variant: MariaDB | Live on 8.4 |
| `oracle` | sql | Schemas, tables and views, columns, keys, indexes, routines, view lineage, users and roles, grants, policies, statistics. Variant: Autonomous | Live on 23 Free |
| `sqlite` | sql | Tables and views, columns, keys, indexes | Fixture |
| `sqlserver` | sql | Databases, schemas, tables and views, columns, keys, indexes, lineage, principals, grants, policies | Not run |
| `snowflake` | sql | Databases, schemas, tables and views, columns, keys, lineage, grants, policies, tags, query history as usage (incremental) | Not run |
| `redshift` | sql | Extends `postgresql`. A PostgreSQL connection that answers as Redshift switches to this definition | Not run |
| `bigquery` | http | Projects, datasets, tables, columns, access entries, job history as usage (incremental) | Fixture |
| `databricks` | http | Jobs and notebooks as routines, SQL query history as usage (incremental). Tables and lineage come from `unity_catalog` on the same workspace | Fixture |
| `unity_catalog` | http | Catalogs, schemas, tables, columns, table lineage | Fixture |
| `elasticsearch`, `opensearch` | http | Indices, mappings as fields, samples | Fixture (OpenSearch shares Elasticsearch's path) |
| `entra_id` | http | Users, groups, members, service principals, directory roles and role assignments, app role assignments, delegated permission grants | Fixture |
| `google_directory` | http | Users, groups, members, org units, roles, role assignments | Fixture |
| `okta` | http | Users, groups, members, apps, app user and group assignments | Fixture |
| `local_files` | files | Folders, files, inferred fields, samples | Fixture |
| `s3` | files | Buckets and prefixes, objects, inferred fields, tags, grants | Fixture (with STS AssumeRole) |
| `adls` | files | ADLS Gen2 file systems and directories, files, inferred fields, ACL grants | Fixture (with an Entra ID token) |
| `gcs` | files | Buckets, objects, inferred fields, labels as tags, grants | Fixture (with a service-account JWT) |
| `dbt` | files | A dbt `manifest.json`: models, seeds, snapshots and sources in their warehouse schemas, column docs, the model DAG as lineage, metrics, business terms | Fixture (dbt 1.9 manifest) |

Definitions do the following:

* **Detect versions and variants:** they are never configured. A version operation and banner patterns pick the
  variant, or switch to another definition.
* **Apply overlays** for particular versions or variants, written inline or in their own files (for example
  `postgresql.pre10.json`).
* **Run an operation only when a condition holds:** on a version, a variant, an option or a property.
* **Fall back to alternative queries** when one is not permitted (`dba_users`, then `all_users`), and treat a
  failure as `fail`, `warn` or `skip`.
* **Run `forEach` operations:** an operation runs once per record of another operation, for example the members of
  each group.
* **Filter** with include/exclude options per level and per dataset, using `*` and `?` wildcards.
* **Inherit** from another definition with `extends`. A custom definition directory overrides a built-in one.

## 2. Connections, credentials and secrets

* **20 auth methods** in the shared catalog:
  * `none`, `username_only`, `username_password`, `token`, `api_key`, `key_pair`;
  * `oauth2_client_credentials`, `oauth2_refresh_token`, `oauth2_access_token`;
  * `aws_iam`;
  * `azure_service_principal`, `azure_managed_identity`, `azure_ad_password`;
  * `gcp_service_account`;
  * `client_certificate`, `kerberos`, `windows_integrated`, `connection_string`, `ssh_key`, `custom`.
* **Credentials derived at connect time** (both editions, standard library only):
  * OAuth 2.0 tokens;
  * Entra ID and Azure managed identity tokens;
  * Google service-account JWT (RS256);
  * AWS SigV4 request signing with STS AssumeRole.

  The signing code is checked against the spec's vectors, five of them AWS's own.
* **Secret references:** `${env:NAME}` and `${file:path}`, plus resolvers you register (a vault, a cloud secret
  manager). They are resolved only when a session opens and never logged.
* **Version detection** is cached per connection, and a session reports the load it put on its source (`stats`).

## 3. The canonical model

32 entities, the same whatever the source. With aspect `data` you also get `row` samples.

| Aspect | Entities |
|---|---|
| `metadata` | source, container, dataset, field, constraint, index, routine, relationship, capability, change |
| `lineage` | lineage (table and column level, with transformation, confidence and assertion), provenance |
| `governance` | principal, grant, policy, tag, label, obligation, delegation |
| `statistics` | statistic |
| `usage` | usage (one query or access event) |
| `context` | business_term, metric, content, chunk |
| `quality` | quality_result |
| `activity` | activity, audit_event |
| `agents` | agent, agent_tool, agent_permission |
| `cost` | usage_cost (FOCUS-aligned) |

Columns without a canonical home are kept in the record's `properties`, so nothing the source returns is lost.

## 4. Reading data

* **Pull API** (`AgentFrontier.open` → session):
  * `test`, `source`, `containers` and paged `datasets` with opaque cursors;
  * `records(entity, scope, datasets)` and `run(operation)`;
  * `sample` and `query`.
* **Read-only ad hoc queries** (SQL sources):
  * a guard accepts exactly one read statement;
  * the query runs in a read-only session or transaction;
  * results have a row cap and a time limit, and values come back JSON-friendly with coarse column categories for
    charting.
* **Push API** (`AgentFrontier.run(PipelineConfig)`, CLI `extract` / `run`):
  * `parallelism` leaf scopes run at a time;
  * page, batch and sample sizes are configurable;
  * a progress listener reports the run;
  * a failing scope makes the run `PARTIAL` instead of stopping it;
  * the `RunReport` gives counts, warnings, errors and timings.

## 5. Raw-stream events

Both editions:

* **Envelope:** each record becomes an event with its tenant, source system, connector, run, family and category,
  plus a stable `nativeObjectId` and a deterministic `eventId` (SHA-256), pinned by shared vectors.
* **Families:** 8 families and their categories, from Discovery & Structure to Economics.
* **Deduplication:** re-reading an unchanged object gives the same `eventId`.

## 6. Incremental runs

Both editions:

* **Checkpoints** are kept per tenant, source and pipeline in a folder, in memory or in your own store, and committed
  only after a run that did not fail.
* **Full-diff change detection:**
  * new and changed objects become `op: upsert`;
  * objects that disappeared become `op: tombstone`, but only in scopes that were read completely;
  * coverage is tracked per producing operation and scope.
* **Watermark operations** (query histories) read only from the saved high-water mark, with duplicates removed at the
  boundary.
* **Delta-token operations** resume from the saved token or URL, and deletions the source reports become
  `op: delete`.
* **Reconciliation runs:** `--full`, or every N runs.
* **Replay:** the same input gives the same events.

## 7. Lineage and usage

* **Catalog lineage:** view dependencies from database catalogs (PostgreSQL, MySQL, Oracle, SQL Server, Snowflake),
  and Unity Catalog table lineage.
* **Parsed column lineage** (Python, `lineage` extra): column lineage parsed with sqlglot from view SQL, with the
  transformation, a confidence and the assertion `parsed`.
* **dbt lineage:** the model DAG as lineage from the dbt manifest.
* **Query history as `usage`:** Snowflake, BigQuery and Databricks, read incrementally.

## 8. Files and object storage

Both editions:

* **Backends:** local folders, network shares and synced cloud folders; S3 and S3-compatible stores (MinIO); ADLS
  Gen2; Google Cloud Storage.
* **Schema inference** from CSV, TSV, JSON, JSON Lines and Avro, using ranged reads. Parquet works with the `parquet`
  extra (Python).
* **Tags and grants:** object tags, metadata and ACLs become `tag` and `grant` records.
* **The `document` operation** selects records from one JSON file (`records`, `where`, `each`). This is how the `dbt`
  definition reads a manifest.

## 9. Governance (Python)

* **Policy decisions:**
  * the built-in rule PDP, which can include the source's native grants;
  * an OPA adapter;
  * combination of several PDPs and fail-closed wrapping;
  * `agentfrontier policy-decide` on the CLI.
* **Enforcement** before every fetch and before records are released, in pipelines and in the pull API
  (`GovernedSession`):
  * deny;
  * mask by field or by classification label;
  * drop fields;
  * row filters and row limits;
  * approval, raised as `ApprovalRequired`;
  * fail-closed defaults for high-risk actions.
* **Classification:** 16 built-in rules (PII, payment cards with the Luhn check, IBANs, health data, credentials)
  over names, comments and sample values. They produce `label` records with provenance and confidence, and there are
  four modes for reconciling source-declared and inferred labels.
* **Authority graph:** effective grants through groups, roles, `PUBLIC` and delegations, with the path that
  explains each one, plus `who_can` (`agentfrontier authority`).
* **Audit trail:** hash-chained `audit_event` records per run, a manifest with an optional HMAC signature, and
  verification (`agentfrontier audit-verify`).

## 10. Graphs (Python)

* **Stores:** the `GraphStore` contract with in-memory and JSON-file stores, and the `graph` sink.
* **Operations:** `neighborhood`, `traversal`, `snapshot`, `close_missing`, and `as_of`, which gives the graph as it
  was at a time.
* **Mandatory access filter** on every traversal: `ALLOW_ALL` or `policy_access(pdp, ...)`. A hidden node also hides
  what lies behind it.
* **Builders** for the asset, lineage (with column edges), authority, quality and audit graphs:
  * provenance, assertion, confidence and validity intervals on every node and edge;
  * entity resolution by id only;
  * tombstones close nodes instead of deleting them, so history is kept.
* **Neo4j:** the `neo4j` sink writes the same ids through a configurable Cypher mapping (both editions).

## 11. Standard formats and sinks

| Sink | What it writes | Editions | Verification |
|---|---|---|---|
| `jsonl`, `json`, `console`, `memory`, `http` | Records or events as JSON | Both | Conformance |
| `csv` | One CSV per entity with the spec's columns, optionally partitioned by tenant, source and category | Both | Shared vectors |
| `parquet` | The same as Parquet (`parquet` extra) | Python | Tests |
| `neo4j` | A graph through a Cypher mapping (`neo4j` extra) | Both | Tests |
| `graph` | Nodes and edges in a memory or JSON graph store | Python | Tests |
| `openlineage` | OpenLineage 2-0-2 RunEvents with column lineage, to Marquez or any endpoint, or to a file | Python (translator: both) | Live: Marquez 0.50 accepted the events and served the column lineage |
| `openmetadata` | OpenMetadata 1.x create-or-update requests, with lineage looked up by FQN | Python (translator: both) | Fixture |
| `datahub` | DataHub metadata change proposals | Python (translator: both) | Fixture |
| `opa` | An OPA data document of the authority graph | Python (translator: both) | Vectors |
| `otlp` | OTLP/JSON logs of quality results, activities and audit events | Python (translator: both) | Vectors |
| `focus` | FOCUS 1.1-aligned CSV of `usage_cost` records | Python (translator: both) | Vectors (no cost sources yet) |
| `ocsf` | OCSF 1.3 API Activity events of audit events | Python (translator: both) | Vectors |
| `kafka` | Every record or event, keyed by `nativeObjectId`, optionally one topic per category (`kafka` extra) | Python | Live: Redpanda received all 391 events of the demo database |

Translators are pure and deterministic, and their ids are UUIDv8 from SHA-256. Every translated sink can send to a
`url` or write to a `path`.

## 12. Observability and discovery of capabilities

* **Run health:** `runHealth` ends a run with `quality_result` records: status, duration, records, errors, warnings,
  retries, and the lag of incremental history (both editions).
* **Tracing:** OpenTelemetry spans for runs, scopes and operations (Python, `otel` extra).
* **Capability registry:** per operation, the entity it produces, its aspect, its raw-stream category, its
  incremental modes and its limits (`agentfrontier capabilities <type>`; Python).
* **Errors:** stable error codes (`AgentFrontierError.code`), the same across languages.

## 13. CLI

`agentfrontier` (or `python -m agentfrontier`) prints JSON on standard output and logs on standard error. Exit codes
are 0 (success), 1 (failure) and 2 (bad usage).

| Command | Purpose |
|---|---|
| `definitions`, `describe <type>`, `capabilities <type>` | What the SDK can read |
| `validate [files]` | Checks definitions, overlays, pipelines and connections |
| `test <connection>` | Connect, detect, run the test operation |
| `extract <connection>`, `run <pipeline>` | Run a pipeline to sinks |
| `query <connection> "<sql>"`, `sample <connection> <dataset>` | Read-only data access |
| `policy-decide`, `audit-verify`, `authority` | Governance (Python) |

Options of `extract`:

* what to read: `--aspects`;
* where it goes: `--sink`, which is repeatable and takes a file sink spec or a sink JSON file; `--partition-by`;
* incremental: `--incremental`, `--full`, `--reconcile-every`;
* run extras: `--run-health`, `--parse-lineage`, `--tracing`;
* events: `--envelope`, `--tenant`;
* governance: `--policy`, `--subject`, `--purpose`, `--classify`, `--audit`;
* tuning: `--parallelism`, `--page-size`, `--batch-size`, `--sample-rows`.

[cli.md](cli.md) has the full reference.

## 14. Library and extension

* **Entry points:** `AgentFrontier(definitions=..., protocols=..., sinks=..., secret_resolvers=..., governance=...)`, or
  `AgentFrontier.from_environment()`.
* **Configuration:** `ConnectionConfig` and `PipelineConfig` take JSON or builders.
* **Extension:** your own definitions and overlays, protocols and sinks (also through the `agentfrontier.protocols`
  and `agentfrontier.sinks` entry point groups), secret resolvers, checkpoint stores, graph stores, cache stores
  (`CacheStore`), policy decision points and classification rules. The extensions add the LLM `Client` protocol
  and the code-generation `Executor` (`agentfrontier-capabilities`) and the `TokenVault` (`agentfrontier-synth`).
* **Packaging:** standard library only at the core. The extras are `postgres`, `mysql`, `oracle`, `snowflake`,
  `sqlserver`, `neo4j`, `yaml`, `parquet`, `lineage`, `otel` and `kafka`.

[python-api.md](python-api.md) has the reference, and [integration.md](integration.md) has integration patterns and
a production checklist.

## 15. Generative capabilities (`agentfrontier-capabilities`)

A separate distribution in `extensions/capabilities`, independent of any agent framework. The LLM is optional and
sits behind a provider protocol.

* **LLM providers:** one provider-neutral chat model with tool calling, and clients for Anthropic, OpenAI, Azure
  OpenAI, Gemini and OpenAI-compatible endpoints. Also structured (JSON) output with one retry, a token estimate,
  a budget (calls per day and per task, token totals) and a scripted client for tests.
* **SQL validation** (`validate_sql`), never executing:
  * parse in the source's dialect, with the line and column of a parse error;
  * one statement, read-only (writes, DDL and `SELECT INTO` refused);
  * every table and column checked against the catalog, with "did you mean" suggestions;
  * lint (`SELECT *`, joins without a condition);
  * transpile to another dialect;
  * a policy decision per table read (deny, or masking obligations);
  * the database's `EXPLAIN` cost through a callback.
* **NL-to-SQL:** grounded context (relevant tables, foreign-key neighbours, join conditions and business terms,
  with tables and columns the policy hides left out), one read-only query from the model, validation, one repair
  from the validation errors, and execution only through the caller's guarded callback, with a provenance manifest.
  `evaluate` scores a generator against reference queries with `compare_results`, which ignores row and column
  order and by default allows extra columns in the generated result; a labelled set of 15 questions for the retail
  demo database ships with the tests.
* **Data-quality rules:** one rule model (completeness, uniqueness, validity, referential, contract, business),
  recommended from keys, nullability, foreign keys, check constraints, known values and profiles. Translated to SQL
  checks (a query counting the rows that break the rule), dbt `schema.yml`, Great Expectations suites and SodaCL.
* **Code generation:** dbt model files with `ref()` and `source()`, dbt SQL turned back into plain SQL for
  validation, and Python compile checks. Execution is an `Executor` interface that products implement.
* **Business Context Graph v2:** `propose_terms` asks a model for terms from the schema, keeps those tied to
  columns that exist, and marks them `inferred` with a confidence and `pending` review.
* **Notebooks:** typed cells (markdown, SQL, chart, Python) rendered to plain Python, written to `.ipynb` with
  each cell's kind, author and hash, read back (a cell edited since it was written becomes user code), with a
  `query()` prelude that sends SQL through the product's guarded API and a Markdown report of an executed
  notebook. Execution is the product's (Avero runs it in a sandbox).
* **Capability profiles:** `nl_to_sql`, `validate_sql`, `dq_rules` and `dbt_model`, each with a manifest (input and
  output schemas, version) and instructions; inputs are checked before a call.

## 16. Synthetic data (`agentfrontier-synth`)

A separate distribution in `extensions/synth`; standard library only.

* **Specs:** tables, columns (type, nullability, uniqueness, accepted values with weights, ranges, null share,
  labels), rows, scenarios (rows with set values, or duplicates of other rows) and rules (keep one date after
  another; total child rows into a parent column).
* **Generation:** deterministic from the seed; parents before children; unique keys; foreign keys that always
  resolve (composite keys and self references too); invented values (emails on the reserved `example.test` domain,
  phones in the fictional 555-01xx range).
* **Masking operators:** redaction, salted hashing, keyed tokenization (reversible with `detokenize` through a
  `TokenVault`; `MemoryTokenVault` is the in-memory reference), format-preserving pseudonymisation (keyed, not NIST
  FPE) and generalisation; `mask_rows` applies a plan of operators per column.
* **Checks:** keys, not-null, unique, accepted values, ranges and foreign keys on any dataset; k-anonymity;
  exact-match leakage against a real sample; Kolmogorov-Smirnov and total-variation distances.
* **Export:** CSV, JSON Lines, or SQL INSERTs in dependency order, with a fixture manifest.

## 17. Cache contract and provenance (core)

* **`agentfrontier.cache`:** the semantic cache contract.
  * `cache_key`: SHA-256 over the kind (`result`, `context` or `artifact`), the `CacheScope` (tenant, principal,
    purpose, source, and the schema, policy, model and embedding versions) and the normalised request.
  * `SemanticCache.get` serves a hit only after mandatory revalidation: same versions, not past its TTL, and the
    caller's check (such as a fresh policy decision) passes. A stale entry is dropped. `put` stores a value with the
    resources it depends on and an optional TTL.
  * `SemanticCache.on_event` invalidates: change events drop entries that depend on a resource (`*` for a prefix),
    policy events drop entries made under another policy version.
  * `CacheStats` counts hits, misses, stale entries, puts and invalidations per kind; `summary()` adds the entries.
  * `CacheStore` is the store protocol; `MemoryCacheStore` keeps entries in memory and evicts the least recently
    used beyond `max_entries`.
* **`agentfrontier.provenance`:** `Manifest` for generated artifacts (hashed inputs with versions, outputs, tools,
  model, validation, approvals, limitations), with a digest that approvals do not change.

## 18. Not implemented yet

* Live runs of Snowflake, SQL Server, Redshift, BigQuery, Databricks, S3, ADLS and GCS: they are checked against
  recorded responses only.
* Live checks of the OpenMetadata and DataHub sinks.
* Graph queries answered by Neo4j, Neptune or Cosmos stores (the Neo4j sink writes the model but does not serve
  queries).
* An object-storage writer, and loaders into Snowflake, Databricks and BigQuery tables.
* Business terms from Snowflake semantic views, Unity Catalog tags and the OpenMetadata glossary.
* The `etag`, `lsn` and `change_feed` incremental modes; usage from PostgreSQL.
* Cedar, Presidio, Tika, Purview, Macie and Google Sensitive Data Protection adapters; consent and retention readers.
* Lineage parsed from query history (only view SQL is parsed).
* Phase 5 remainders: DS transforms (pandas, scikit-learn); PySpark, script and OpenAPI-client generation (code
  generation covers dbt only); the live NL-to-SQL accuracy run on the labelled retail set.
* Phase 6: cloud cost sources and LLM usage sources normalised to FOCUS, agent registry and permission sources, and
  the actions (write) aspect.
* In the Java edition: governance, graphs, parsed lineage, Parquet, OpenTelemetry, the capability registry, the
  sinks that deliver translated documents, the cache and provenance contracts and the extensions. The Java CLI has
  no `capabilities`, `policy-decide`, `audit-verify` or `authority` command, and `extract` has no `--incremental`,
  `--partition-by`, `--run-health`, `--parse-lineage`, `--tracing` or governance options.

## 19. Roadmap

From `common-docs/AGENTFRONTIER-PLAN.md`:

| Phase | Scope |
|---|---|
| 4 | SaaS, collaboration and content sources:<ul><li>GitHub, Jira, Confluence, ServiceNow, then Salesforce, SharePoint/OneDrive, Google Drive, Slack and Teams;</li><li>OAuth authorization-code and device flows;</li><li>`agentfrontier-content` for PDF/Office/web content with `content` and `chunk` citations.</li></ul> |
| 5 | Generative capabilities (`agentfrontier-capabilities`). **Done in this slice (§15, §17):** NL-to-SQL with static validation and policy checks, data-quality rules, dbt code generation, notebook templates, Business Context Graph v2 proposals, the semantic cache contract and provenance manifests. **Remaining:** DS transforms (pandas, scikit-learn), PySpark, script and OpenAPI-client generation, and the live NL-to-SQL accuracy run. |
| 6 | Cloud and LLM cost sources normalised to FOCUS; agent registry and permission sources; an opt-in actions (write) aspect. **Done, pulled forward (§16):** `agentfrontier-synth` for synthetic data. |
| 7 | `agentfrontier-gateway` (REST and MCP over the SDK); a hybrid worker; domain packs (banking, insurance first). |
