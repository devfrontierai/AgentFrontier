# Changelog

Versions follow semantic versioning. Each release names the spec tag it implements; see the spec's
[VERSIONING.md](https://github.com/devfrontierai/agentfrontier-spec/blob/main/VERSIONING.md).

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
