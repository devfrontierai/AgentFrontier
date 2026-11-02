# Changelog

Versions follow semantic versioning. Each release names the spec tag it implements; see the spec's
[VERSIONING.md](https://github.com/devfrontierai/agentfrontier-spec/blob/main/VERSIONING.md).

## 0.4.0 (unreleased): plan phase 3

Implements **spec-v1.5.0**.

* **Graphs** (`agentfrontier.graph`): `MemoryGraphStore` and `JsonGraphStore` with `neighborhood`, `traversal`,
  `snapshot`, `close_missing` and `as_of`; a mandatory access filter (`ALLOW_ALL`, or `policy_access(pdp, ...)` from
  any policy decision point) that also stops traversal through hidden nodes; `GraphBuilder` for the asset, lineage,
  authority, quality and audit graphs with provenance and validity intervals (tombstones close nodes, history kept);
  the `graph` sink.
* **Translators** (`agentfrontier.translators`): OpenLineage, OpenMetadata, DataHub, OPA, OTLP logs, FOCUS and OCSF,
  pure and deterministic, with the spec's vectors.
* **Sinks:** `openlineage` (checked against Marquez 0.50: events accepted, column lineage served back), `datahub`,
  `openmetadata` (looks tables up by FQN for lineage), `opa`, `otlp`, `focus`, `ocsf`, and `kafka` (checked against
  Redpanda; `kafka` extra).
* **dbt projects:** the `dbt` definition and the files engine's `document` operation.

## 0.3.0: plan phase 1

Implements **spec-v1.4.0**.

* **Incremental runs.** `incremental` in pipelines (`--incremental <dir>` on the command line): checkpoints
  (`FileCheckpointStore`, `MemoryCheckpointStore`), change detection by `eventId` with `upsert`, `delete` and
  `tombstone` events, coverage per producer and scope (a failing scope or operation never tombstones what it could
  not read; a vanished container takes its contents with it), `watermark` and `delta_token` operations with boundary
  deduplication, reconciliation runs (`full`, `reconcileEvery`), commit only after a run that did not fail.
* **Object storage.** The files engine on S3 (and S3-compatible stores), ADLS Gen2 and GCS: definitions `s3`,
  `adls`, `gcs`, ranged reads capped by `maxBytes`, Avro header schemas, Parquet footers (`parquet` extra), object
  tags, metadata and ACLs. `local_files` lists Avro and Parquet files too.
* **Cloud credentials** with the standard library only: SigV4 signing (checked against AWS's published vectors), STS
  AssumeRole, profiles; Entra ID client credentials (`azure_service_principal`), managed identity, and Google
  service account JWTs (RS256, pure Python). HTTP definitions can ask for SigV4 signing.
* **Data platforms.** `bigquery` and `databricks` definitions; `snowflake` query history as usage. Template filter
  `toEpochMs`.
* **Sinks.** `csv`, `parquet` (extra) and `partitionBy` for `jsonl`, `csv` and `parquet`.
* **Observability.** `run_health` (`quality_result` records about the run), retried requests in the run report,
  OpenTelemetry spans (`otel` extra).
* **Parsed lineage.** `lineage={"parse": True}`: column lineage from view SQL with `sqlglot` (`lineage` extra),
  `assertion: parsed` and a confidence per record.
* The capability registry reports incremental modes (`watermark`, `delta_token`, `full_diff`).

## 0.2.x (plan phase 2: governance)

Implements **spec-v1.3.0**.

* **Policy enforcement.** `agentfrontier.governance`: the decision contract (`Decision`), the built-in `RulePDP`
  (ordered rules, deny overrides allow, fail closed for `write` / `delete` / `execute`, optional native-grant
  checks), `OpaPDP`, `CombinedPDP` and `FailClosedPDP`. Pipelines decide every operation and sample before it
  runs and every record before it reaches a sink; obligations `mask`, `drop_fields`, `filter_rows`,
  `limit_rows`, `approval` and `audit` are applied, and anything else denies. `af.open(conn, governance=...)`
  returns a `GovernedSession` for the pull API (`PolicyDenied` / `ApprovalRequired`).
* **Masking at read time** for samples, pipeline rows and `query()` results: `redact`, `null`, `hash` (salted
  SHA-256) and `partial`, by field name or classification label.
* **Classification.** `RuleClassifier` with the spec's 16 built-in rules over field names, comments and sample
  values; `label` records with engine, rule version, confidence and evidence (`assertion: inferred` in the raw
  stream); `reconcile` with the four modes and inspectable conflicts.
* **Authority graph.** `AuthorityGraph`: effective grants through groups, `PUBLIC` and scoped delegations, with
  the path that explains each; `who_can`, `relationships()` and `export()` for Evia.
* **Audit trail.** `AuditLog`: hash-chained `audit_event` records per run, the manifest in `run.json`
  (`report.audit`), an optional HMAC signature, and `verify` / `verify_directory`.
* **Identity sources** (experimental): `entra_id`, `google_directory`, `okta`, with group memberships
  (`member_of` relationships, read by the authority graph) and app assignments.
* **`forEach` operations** in the engine: one execution per record of another operation (spec-v1.3.0).
* **CLI:** `extract --policy --subject --purpose --classify --audit`, `policy-decide`, `audit-verify`,
  `authority`; `query` and `sample` take `--policy`, `--subject`, `--purpose` and `--classify`.
* Pipeline JSON `governance` block; `RunReport.policy` and `RunReport.audit`; error code `POLICY_DENIED`.
* `yaml` extra for YAML rules files.
* Without a `governance` block, output is unchanged.

## 0.2.0 (unreleased)

Implements **spec-v1.1.0**.

* **Raw-stream events.** `PipelineConfig(envelope=True, tenant_id=...)`, pipeline JSON `"envelope": true,
  "tenantId": ...` and CLI `extract --envelope --tenant <id>` wrap every record in an `Event` with a deterministic
  `eventId`, a stable `nativeObjectId`, its raw-stream family and category, and run provenance. The envelope is off
  by default, so existing pipelines and consumers are unchanged.
* **Capability registry.** `AgentFrontier.capabilities(type)` and CLI `capabilities <type>` list what a connector can
  read, derived from its operations plus an optional `capabilities` block in the definition.
* **Spec moved** to its own repository, [agentfrontier-spec](https://github.com/devfrontierai/agentfrontier-spec).
  `scripts/sync_spec.py --ref <tag>` refreshes the vendored copy from a tag and records it in `scripts/spec.lock`.
* Entity model v2 is packaged: 18 new entities, 5 new aspects and a category on every entity, plus `streams.json`
  and the JSON Schemas under `spec/schemas/`. No connector emits the new entities yet.
* `AgentFrontier.streams` (the `StreamCatalog`), `EntityModel.category_of` and `EntityModel.spec_version`.
* CI on Python 3.10-3.13 (Linux) and 3.12 (Windows), a spec-pin check, a check that the core still has no runtime
  dependencies, and a PyPI Trusted Publishing workflow for `v*` tags.
* `jsonschema` added to the `dev` extra (tests only).

## 0.1.0

First Python edition: a port of the Java reference implementation that passes the same conformance cases.
