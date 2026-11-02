# CLI

```
agentfrontier <command> [arguments] [options]
python -m agentfrontier <command> [arguments] [options]
```

The commands and options are the same as the Java CLI's. Results go to standard output as JSON (pretty printed the
way Jackson prints them, so the two CLIs produce the same text), logs and progress to standard error. Exit code 0 on
success, 1 on failure, 2 on bad usage.

| Command | What it does |
|---|---|
| `definitions` | lists source definitions (id, name, protocol, category, status, auth methods, origin) |
| `describe <type>` | properties, options, auth methods (fields, `?` = optional), hierarchy, operations (`entity @ scope`) and overlays of a definition |
| `capabilities <type>` | the definition's `capability` records: per operation the produced entity, aspect, raw-stream category, incremental modes and limits (Python edition only for now) |
| `validate [file...]` | without files: checks every definition. With files: each may be a definition, an overlay, a pipeline or a connection. Prints `{"ok": ..., "problems": [...]}` |
| `test <connection.json>` | connects, detects the version, runs the test operation; prints the test result |
| `extract <connection.json>` | runs a pipeline to sinks, prints the run report |
| `run <pipeline.json>` | runs a pipeline file; progress events on standard error, the report on standard output |
| `query <connection.json> "<sql>"` | read-only query, prints columns, rows, `truncated`, `elapsedMs` |
| `sample <connection.json> <dataset>` | first rows of a dataset as a JSON array |
| `policy-decide --rules <rules.json> [request.json]` | decides one policy request (standard input when no file); prints the decision, exit 1 on deny (Python only) |
| `audit-verify <dir>` | checks the audit hash chain a jsonl sink wrote against `run.json`; `--key-env VAR` also checks the signature (Python only) |
| `authority <dir>` | effective grants from an extraction's `principal`, `grant`, `delegation` and `agent_permission` files: `--principal <name>`, `--who-can PRIVILEGE:resource`, or the whole graph (Python only) |

Options:

| Option | Commands | Meaning |
|---|---|---|
| `--definitions <dir>` | all | extra definitions or overlays (repeatable; later ones win) |
| `--drivers <dir>` | all | Java edition only (JDBC jars). Accepted and ignored with a note; install drivers with pip extras instead |
| `--aspects a,b` | extract | aspects to extract (default `metadata`) |
| `--sink <spec>` | extract | `jsonl:<dir>`, `csv:<dir>`, `parquet:<dir>`, `json:<file>`, `console`, `memory`, or a sink JSON file (repeatable; default `console`) |
| `--partition-by tenant,source,category` | extract | folders per value for the `jsonl`, `csv` and `parquet` sinks |
| `--incremental <dir>` `--full` `--reconcile-every N` | extract | emit only changes, deletions and tombstones since the last run, keeping checkpoints in `<dir>` (turns the envelope on); `--full` reads everything and tombstones what is gone |
| `--run-health` | extract | end with `quality_result` records about the run |
| `--parse-lineage` | extract | add column lineage parsed from view SQL (`lineage` extra) |
| `--tracing` | extract | OpenTelemetry spans (`otel` extra) |
| `--parallelism N` `--page-size N` `--batch-size N` `--sample-rows N` | extract | pipeline settings |
| `--envelope` `--tenant <id>` | extract | write raw-stream events (tenant `default` unless given) instead of plain records. Put `--envelope` after the connection file, or write `--envelope true`, since an option takes the next argument as its value |
| `--policy <rules.json>` | extract, query, sample | decide every read with these rules: denies, masks, row filters (see [governance](governance.md)) |
| `--subject <id>` `--purpose <text>` | extract, query, sample | who the run acts for and why, for the policy and the audit trail |
| `--classify` | extract, query, sample | label fields with the built-in classifiers (names, comments, sample values) |
| `--audit` `--audit-key-env VAR` | extract | write hash-chained `audit_event` records; sign the manifest with the key in `$VAR` |
| `--max-rows N` | query | row cap (default 1000) |
| `--scope level=value,...` | query, sample | where to run, for example `--scope database=shopdb,schema=shop` |
| `--limit N` | sample | rows (default 10) |

With the console sink, records go to standard output as JSON Lines and the report to standard error.

Options take the next argument as their value unless it starts with `--`; an option without a value is `true`.

Examples:

```bash
agentfrontier describe postgresql
agentfrontier validate my-definitions/*.json
agentfrontier test shop.json
agentfrontier extract shop.json --aspects metadata,lineage --sink jsonl:out/shop --parallelism 4
agentfrontier extract files.json --sink console > records.jsonl
agentfrontier run pipelines/nightly.json
agentfrontier query shop.json "select * from shop.orders" --scope database=shopdb,schema=shop --max-rows 50
agentfrontier sample shop.json orders --scope database=shopdb,schema=shop --limit 5
```

Note for Windows machines with application control policies: if the generated `agentfrontier.exe` launcher is
blocked, `python -m agentfrontier` is equivalent.
