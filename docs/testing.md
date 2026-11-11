# Testing

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,postgres,mysql,oracle,neo4j]"
.venv/bin/python -m pytest -q
```

From the repository root, `pytest` runs the core suites under `tests/` and both extension suites
(`extensions/capabilities/tests`, `extensions/synth/tests`); `pyproject.toml` puts the extensions' sources on the
path. Each extension's suite also runs on its own from its folder (`../../.venv/bin/python -m pytest -q`). Measured
on 11 Nov 2026: 240 passed and 8 skipped from the root (209 passed and 8 skipped under `tests/`, 25 in
`agentfrontier-capabilities`, 6 in `agentfrontier-synth`); the skipped tests are the database and Neo4j suites
without their servers.

## Suites

| File | What it covers | Needs |
|---|---|---|
| `tests/test_engine_unit.py` | Mirror of Java `EngineUnitTest`: templates and filters, the SQL renderer, version ranges, mapping and coercion, collect, JSONPath and tree records, secrets, path globs, the SQL guard; plus paramstyle rewriting, Jackson-compatible JSON output and page cursors | nothing |
| `tests/test_spec.py` | Mirror of Java `SpecTest`: `index.json` lists exactly the definition files, every definition validates, `extends` and overlays resolve, connection validation; plus `from_environment()` and the spec-copy check | nothing (the copy check needs `AGENTFRONTIER_SPEC_SOURCE`) |
| `tests/test_conformance.py` | Mirror of Java `ConformanceTest`: the 16 cases under `tests/conformance` with a fixture HTTP server, an SQLite setup script and a memory sink; records compared as sets of canonical JSON objects after `ignore`, sample rows compared with `expected-sample.json`; the same run with the envelope on compared with `expected-envelope.jsonl`; the incremental cases run several times against their `expected-<n>.jsonl` files; the `eventId` vectors of `event-ids.json`, the SigV4 and RS256 signing vectors (`auth/`), the CSV sink vectors (`sinks/csv.json`) and the translator vectors (`translators/`) | nothing |
| `tests/test_envelope.py` | Mirror of Java `EnvelopeTest`: `nativeObjectId` per entity, envelope off by default, stable event ids across runs, tenant in the id, events against the envelope JSON Schema, pipeline JSON settings; plus capability records and the `capabilities` command | nothing (`jsonschema`, in the `dev` extra, for the schema test) |
| `tests/test_engine_behaviour.py` | Engine paths the conformance cases do not reach: every paging mode, alternatives, `onError`, HTTP retries and auth failures, user overlays, file edge cases, json / http sinks, parallel pipelines | nothing |
| `tests/test_cli.py` | Every CLI command against SQLite | nothing |
| `tests/test_failure_handling.py` | Mirror of Java `FailureHandlingTest`: a run that cannot start still ends with a report and `run.json`, invalid sink configurations, foreign cursors, boolean coercion | nothing |
| `tests/test_governance.py` | The governance vectors in `tests/conformance/governance` (policy decisions, masking, classification, audit chain), obligations the SDK cannot meet, fail-closed PDPs, the OPA adapter, the authority graph, reconcile modes, pipeline and session enforcement, and the governance CLI commands | nothing |
| `tests/test_phase1.py` | Checkpoint stores, failed runs and replay, reconciliation cadence, partitioned file sinks, Parquet, parsed lineage, run health, managed identity tokens, incremental modes in the capability registry, `extract --incremental` | `pyarrow` and `sqlglot` from the `dev` extra (those tests skip without them) |
| `tests/test_phase3.py` | Graph stores and builders with access filters and history, the translated sinks to files and over HTTP against a fixture server, the dbt project graph | nothing |
| `tests/test_phase5.py` | The semantic cache (keys, mandatory revalidation, invalidation by change and policy events, LRU eviction) and provenance manifests | nothing |
| `extensions/capabilities/tests` | `agentfrontier-capabilities`: SQL validation, NL-to-SQL with a scripted model (repair, policy, guarded execution, evaluation, the retail fixture set), data-quality rules (also run on SQLite), dbt generation, LLM helpers, profiles, proposed terms, notebooks | `sqlglot` |
| `extensions/synth/tests` | `agentfrontier-synth`: generation with foreign-key integrity and scenarios, determinism, exports loaded into SQLite, masking operators, privacy and utility reports | nothing |
| `tests/test_database_it.py` | Mirror of Java `DatabaseIT`: the shop fixture on PostgreSQL 9.6 / 11 / 14 / 17, MySQL 8.4 and Oracle 23 (containers, paged datasets, fields, keys, indexes, cross-schema keys, lineage, policies, grants, samples, queries, a full pipeline) | the test databases; skipped when a port is closed or the driver extra is missing |
| `tests/test_neo4j_sink.py` | The uid scheme, and a pipeline into Neo4j | Neo4j on `127.0.0.1:7687` and `AF_TEST_NEO4J_PASSWORD` set (skipped otherwise) |

## The conformance contract

`tests/conformance` is a copy of the [agentfrontier-spec](https://github.com/devfrontierai/agentfrontier-spec)
conformance folder. Its `expected.jsonl`, `expected-envelope.jsonl`, `expected-sample.json` and `event-ids.json`
files are the contract between implementations: never edit them here. On a mismatch the test writes the actual
records to `out/conformance-actual/<case>.jsonl` (`<case>-envelope.jsonl` for events) and lists missing and
unexpected records. If an expected value looks wrong, fix the implementation that is wrong (or raise it against the
spec) and resync with `scripts/sync_spec.py`.

To check that the copies match the pinned spec tag (`scripts/spec.lock`):

```bash
python scripts/sync_spec.py ../agentfrontier-spec --ref spec-v1.5.0 --check
AGENTFRONTIER_SPEC_SOURCE=../agentfrontier-spec python -m pytest tests/test_spec.py
```

## Test databases

The integration tests use the docker compose file of the Java edition (`agentfrontier-java/testing/docker-compose.yml`
in ai-data-studio): PostgreSQL 9.6 on port 55496, 11 on 55411, 14 on 55414, 17 on 55417 (database `shopdb`), MySQL
8.4 on 53306 and Oracle 23 Free on 51521 (service `FREEPDB1`), each with the read-only user `af_reader` /
`af_reader_pw` and the `shop` and `audit` schemas.

```bash
docker compose -f ../ai-data-studio/agentfrontier-java/testing/docker-compose.yml up -d
python -m pytest -m database -v
```

MySQL logins use `caching_sha2_password` without TLS, which PyMySQL completes with RSA through the `cryptography`
package (installed with the `oracle` extra; install it separately if you only use `mysql`).

## Neo4j

```bash
AF_TEST_NEO4J_URI=neo4j://127.0.0.1:7687 AF_TEST_NEO4J_USER=neo4j AF_TEST_NEO4J_PASSWORD=... python -m pytest -m neo4j
```

The test writes only nodes under a unique `source` id, uses the default mapping without its `setup` statements (so it
creates no constraints on a shared server) and deletes everything it wrote.

## Comparing with the Java edition

Both CLIs write the same JSON Lines. To compare on a database:

```bash
java -jar agentfrontier.jar extract shop.json --aspects metadata,lineage,governance,statistics --sink jsonl:java-out
agentfrontier extract shop.json --aspects metadata,lineage,governance,statistics --sink jsonl:py-out
diff <(sort java-out/field.jsonl) <(sort py-out/field.jsonl)
```

On the test databases every entity file is byte-identical (run `java` with `-Duser.timezone=UTC` if the JVM's zone
name is unknown to PostgreSQL).

## Python versions

The suite runs on Python 3.10+. CI (`.github/workflows/ci.yml`) runs it on 3.10, 3.11, 3.12 and 3.13 on Linux and on
3.12 on Windows, and installs each extension as its own distribution to run its tests on 3.12.
