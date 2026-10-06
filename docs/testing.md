# Testing

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,postgres,mysql,oracle,neo4j]"
.venv/bin/python -m pytest -q
```

## Suites

| File | What it covers | Needs |
|---|---|---|
| `tests/test_engine_unit.py` | Mirror of Java `EngineUnitTest`: templates and filters, the SQL renderer, version ranges, mapping and coercion, collect, JSONPath and tree records, secrets, path globs, the SQL guard; plus paramstyle rewriting, Jackson-compatible JSON output and page cursors | nothing |
| `tests/test_spec.py` | Mirror of Java `SpecTest`: `index.json` lists exactly the definition files, every definition validates, `extends` and overlays resolve, connection validation; plus `from_environment()` and the spec-copy check | nothing (the copy check needs `AGENTFRONTIER_SPEC_SOURCE`) |
| `tests/test_conformance.py` | Mirror of Java `ConformanceTest`: every case under `tests/conformance` with a fixture HTTP server, an SQLite setup script and a memory sink; records compared as sets of canonical JSON objects after `ignore`, sample rows compared with `expected-sample.json` | nothing |
| `tests/test_engine_behaviour.py` | Engine paths the conformance cases do not reach: every paging mode, alternatives, `onError`, HTTP retries and auth failures, user overlays, file edge cases, json / http sinks, parallel pipelines | nothing |
| `tests/test_cli.py` | Every CLI command against SQLite | nothing |
| `tests/test_database_it.py` | Mirror of Java `DatabaseIT`: the shop fixture on PostgreSQL 9.6 / 11 / 14 / 17, MySQL 8.4 and Oracle 23 (containers, paged datasets, fields, keys, indexes, cross-schema keys, lineage, policies, grants, samples, queries, a full pipeline) | the test databases; skipped when a port is closed or the driver extra is missing |
| `tests/test_neo4j_sink.py` | The uid scheme, and a pipeline into Neo4j | Neo4j on `127.0.0.1:7687` and `AF_TEST_NEO4J_PASSWORD` set (skipped otherwise) |

## The conformance contract

`tests/conformance` is a copy of the shared spec's conformance folder. Its `expected.jsonl` and
`expected-sample.json` files are the contract between implementations: never edit or regenerate them from Python.
On a mismatch the test writes the actual records to `out/conformance-actual/<case>.jsonl` and lists missing and
unexpected records. If an expected value looks wrong, fix the implementation that is wrong (or raise it against the
spec) and resync with `scripts/sync_spec.py`.

To check that the copies match the shared spec:

```bash
AGENTFRONTIER_SPEC_SOURCE=../agentfrontier-java/spec python -m pytest tests/test_spec.py
python scripts/sync_spec.py ../agentfrontier-java/spec --check
```

## Test databases

The integration tests use the docker compose file of the Java repository (`testing/docker-compose.yml`): PostgreSQL
9.6 on port 55496, 11 on 55411, 14 on 55414, 17 on 55417 (database `shopdb`), MySQL 8.4 on 53306 and Oracle 23 Free on
51521 (service `FREEPDB1`), each with the read-only user `af_reader` / `af_reader_pw` and the `shop` and `audit`
schemas.

```bash
docker compose -f ../agentfrontier-java/testing/docker-compose.yml up -d
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

The suite runs on Python 3.10+; it is exercised on 3.14 and 3.11.
