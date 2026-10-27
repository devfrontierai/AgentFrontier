# Changelog

Versions follow semantic versioning. Each release names the spec tag it implements; see the spec's
[VERSIONING.md](https://github.com/devfrontierai/agentfrontier-spec/blob/main/VERSIONING.md).

## Unreleased (plan phase 2: governance)

Implements **spec-v1.2.0**.

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
* **Identity sources** (experimental): `entra_id`, `google_directory`, `okta`.
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
