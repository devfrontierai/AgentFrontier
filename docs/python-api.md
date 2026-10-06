# Python API

Everything is importable from `agentfrontier`. Names follow Python conventions (snake_case methods, properties
instead of no-argument getters); the JSON the SDK writes (records, reports, CLI output) is identical to the Java
edition's.

## `AgentFrontier`

```python
AgentFrontier(definitions=(), protocols=(), sinks=(), secret_resolvers=(), environment=None, detection_ttl=600.0)
AgentFrontier.from_environment()      # extra definition dirs from AGENTFRONTIER_DEFINITIONS
```

Thread-safe; create one and share it.

| Member | Purpose |
|---|---|
| `definitions` | the `DefinitionRegistry` (`get`, `find`, `ids`, `all`, `origin`, `add`, `add_directory`, `validate_all`) |
| `list_definitions()` / `definition(type)` | definitions sorted by name / one by id or alias (None when unknown) |
| `entity_model`, `auth_catalog`, `spec` | the canonical entities, the auth catalog, the packaged spec files |
| `auth_methods(definition)` | auth methods a definition supports, with their fields |
| `protocols`, `sink_types` | installed protocol ids and sink types |
| `validate(connection)` | list of problems (empty when usable); secrets are not resolved |
| `test(connection)` | `TestResult`; never raises for configuration or connection problems |
| `open(connection)` | a session (validates, resolves secrets, connects, detects version and variant) |
| `run(pipeline_config, listener=None)` | runs a pipeline, returns a `RunReport` |
| `sink(config)` | a sink from its JSON configuration |
| `clear_detection_cache()` | forget cached version detection |
| `secrets` | the `Secrets` resolver (`resolve`, `resolve_all`, `resolve_deep`) |

Extra protocols and sink providers can also be registered through the entry point groups `agentfrontier.protocols`
and `agentfrontier.sinks` (the analogue of Java's `ServiceLoader`).

## Connections

```python
ConnectionConfig(type, properties=None, auth=None, options=None, id=None, name=None)
ConnectionConfig.from_json(text_or_dict)
ConnectionConfig.from_file(path)
ConnectionConfig.builder("postgresql").id("x").property("host", "h").option("includeSchemas", ["a"]).auth(...).build()

AuthConfig(method, values)            # or the factories:
AuthConfig.username_password(u, p), AuthConfig.token(t, username=None), AuthConfig.api_key(k),
AuthConfig.key_pair(u, pem, passphrase=None), AuthConfig.oauth2_client_credentials(url, id, secret, scope=None),
AuthConfig.connection_string(s), AuthConfig.username_only(u), AuthConfig.none()
```

`str(connection)` and `str(auth)` print field names only, never values.

## Sessions (`EngineSession`, alias `SourceSession`)

Use as a context manager (`with af.open(c) as s:`) or call `close()`. Not thread-safe: open one per thread.

| Member | Purpose |
|---|---|
| `connection`, `definition`, `info` | the connection, the definition in effect (after variant switch and overlays), a `SourceInfo` (definition, requested, product, version, banner, variant) |
| `hierarchy` | list of `Level` (`level`, `label`, `list`, `value`, `select`, `connect`, `exclude`) |
| `test()` | `TestResult` of the definition's test operation |
| `source()` | the `source` record |
| `containers(parent_scope)` | containers one level below a scope, after selections and filters |
| `datasets(scope, cursor=None, page_size=0)` | one `Page` (`records`, `next_cursor`, `has_more`) of datasets |
| `dataset_names(scope)` | every dataset name of a leaf scope |
| `records(entity, scope, datasets=None)` | records of an entity: per dataset (`field`, `constraint`, `index`, `statistic`, ...) or per scope (`lineage`, `routine`, `grant`, `policy`, `principal`) |
| `supports(entity)` | whether any active operation produces it |
| `run(operation, scope, params=None)` | one named operation with extra template variables |
| `sample(scope, dataset, limit=10)` | first rows as `row` records |
| `query(request)` | read-only ad hoc query (SQL sources); `request` is a `QueryRequest` or a SQL string |
| `stats` | `SessionStats`; `stats.snapshot()` gives statements, rows, elapsed_ms, connections |
| `warnings` | problems that did not stop the session |

A typical crawl:

```python
def walk(s, scope):
    if scope.depth < len(s.hierarchy):
        for c in s.containers(scope):
            walk(s, scope.with_(c.string("level"), c.string("name")))
        return
    cursor = None
    while True:
        page = s.datasets(scope, cursor, 500)
        names = [r.string("name") for r in page.records]
        fields = s.records("field", scope, names)
        keys = s.records("constraint", scope, names)
        cursor = page.next_cursor
        if cursor is None:
            break
    lineage = s.records("lineage", scope)
```

Page cursors are opaque base64url JSON (`{"o": <operation>, "v": <value>}`), the same format as the Java edition.

## Values

* `Scope`: `Scope.root()`, `Scope.of("database", "x", "schema", "y")`, `Scope.of({...})`, `with_(level, value)`,
  `get`, `has`, `depth`, `is_root`, `as_map()`, `values()`, `up_to(level)`; hashable.
* `Record`: `entity` and `values` (a dict holding `source`, the level values, canonical fields and `properties`);
  helpers `get`, `has`, `string`, `long_value`, `int_value`, `double_value`, `bool(key, fallback=None)`, `strings`,
  `properties`, `get_or_property`, `to_json()`.
* `QueryRequest(sql, scope=None, max_rows=1000, timeout_seconds=60, parameters=None)`: positional `?` parameters
  are rewritten to the driver's paramstyle and bound, never concatenated.
* `QueryResult`: `columns` (`Column(name, type_name, category)` with `Category.NUMBER/TEXT/DATE/TIMESTAMP/BOOLEAN/OTHER`),
  `rows`, `truncated`, `elapsed_ms`, `row_count`, `column(name)`, `to_dict()`.
* Values read from drivers are JSON-friendly and match JDBC's: integral decimals become ints, other decimals floats,
  timestamps `YYYY-MM-DDTHH:MM:SS[.ffffff]`, dates ISO, binary values `<binary N bytes>`, arrays lists.

## Ad hoc queries

`query()` first runs the SQL guard (one statement, starting like a read, no write / DDL / privilege keywords or
dangerous functions outside literals and comments; else `QUERY_REJECTED`), then runs it in a transaction that is
rolled back (unless the definition sets `session.readOnlyTransaction: false`), with a statement timeout where the
driver supports one (PostgreSQL `statement_timeout`, MySQL `max_execution_time`, Oracle `call_timeout`, SQLite a
progress handler) and fetches at most `max_rows + 1` rows.

## Pipelines

```python
PipelineConfig(connection, aspects=None, operations=None, scope=None, sinks=None,
               page_size=0, batch_size=0, parallelism=0, sample_rows=0, name=None)
PipelineConfig.of(connection, aspects, sinks)
PipelineConfig.from_file(path)  /  PipelineConfig.from_json(obj_or_text, base_dir=None)
report = af.run(config, listener=lambda event, data: ...)
```

Defaults: aspects `["metadata"]`, page size 500, batch size 200, parallelism 1, sample rows 10. With
`parallelism > 1` leaf scopes run on a thread pool with one session per worker thread. Listener events: `started`,
`container`, `scope.started`, `scope.finished`, `error`, `finished`.

`RunReport`: `run_id`, `pipeline`, `source`, `definition`, `version`, `variant`, `status` (SUCCEEDED, PARTIAL,
FAILED), `counts`, `warnings`, `errors`, `elapsed_ms`, `load`, `started_at`, `finished_at`, `count(entity)`,
`to_map()` (the JSON shape of `run.json`).

## Sinks

Sink configurations are JSON objects with a `type`:

| Type | Configuration |
|---|---|
| `jsonl` | `path`: a folder; one `<entity>.jsonl` per entity plus `run.json` (old `.jsonl` files are removed first) |
| `json` | `path`: one document `{"run": ..., "records": {"<entity>": [...]}}` |
| `console` | JSON Lines on standard output |
| `memory` | `name` (default `default`); read back with `MemorySink.get(name).records(entity=None)` |
| `http` | `url`, `headers` (secret references allowed), `batchSize` (500); POSTs `begin`, `records` and `end` events |
| `neo4j` | `uri`, `username`, `password`, `database`, `mapping` (`default` or a file), `batchSize`, `removeStale` |

The neo4j sink uses the mapping in `spec/sinks/neo4j-default.json` and the shared uid scheme
(`agentfrontier.sinks.uids`), so a graph written by Python and one written by Java are the same graph. Custom sinks
subclass `Sink` (`begin`, `write`, `end`, `close`) and are registered with a `SinkProvider`.

## Errors

Every failure is an `AgentFrontierError` with a stable `code`: `INVALID_CONFIG`, `INVALID_DEFINITION`,
`UNSUPPORTED`, `CONNECTION_FAILED`, `QUERY_FAILED`, `QUERY_REJECTED`, `AUTH_FAILED`, `SINK_FAILED`. Messages never
contain credentials.

## Lower-level modules

`agentfrontier.template` (template language, SQL renderer), `agentfrontier.mapping` (coercion, JSONPath subset,
name filters, record mapper), `agentfrontier.definitions` (registry, version ranges, conditions, merge),
`agentfrontier.protocols.*` (engines, SQL guard, schema inference) and `agentfrontier.json_util` (Jackson-compatible
JSON output) are importable for tooling and tests; their behaviour is part of the shared spec.

## Differences from the Java edition

* SQL sources use DB-API 2.0 drivers configured by the definition's `connection.python` block instead of JDBC.
  When the auth block gives a `dsn` (connection strings), the base `connect` arguments are not passed, because the
  connection string carries the target and the driver would otherwise let property defaults override it.
* `TestResult.details` keeps the order the test operation returned (Java's `Map.copyOf` has no defined order).
* Definition origins read `package:definitions/...` instead of `classpath:definitions/...`.
* The query row cap is applied while fetching (see the README).
