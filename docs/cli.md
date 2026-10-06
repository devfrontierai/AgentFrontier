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
| `validate [file...]` | without files: checks every definition. With files: each may be a definition, an overlay, a pipeline or a connection. Prints `{"ok": ..., "problems": [...]}` |
| `test <connection.json>` | connects, detects the version, runs the test operation; prints the test result |
| `extract <connection.json>` | runs a pipeline to sinks, prints the run report |
| `run <pipeline.json>` | runs a pipeline file; progress events on standard error, the report on standard output |
| `query <connection.json> "<sql>"` | read-only query, prints columns, rows, `truncated`, `elapsedMs` |
| `sample <connection.json> <dataset>` | first rows of a dataset as a JSON array |

Options:

| Option | Commands | Meaning |
|---|---|---|
| `--definitions <dir>` | all | extra definitions or overlays (repeatable; later ones win) |
| `--drivers <dir>` | all | Java edition only (JDBC jars). Accepted and ignored with a note; install drivers with pip extras instead |
| `--aspects a,b` | extract | aspects to extract (default `metadata`) |
| `--sink <spec>` | extract | `jsonl:<dir>`, `json:<file>`, `console`, `memory`, or a sink JSON file (repeatable; default `console`) |
| `--parallelism N` `--page-size N` `--batch-size N` `--sample-rows N` | extract | pipeline settings |
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
