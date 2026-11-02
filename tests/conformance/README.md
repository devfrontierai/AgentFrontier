# Conformance cases

Every AgentFrontier implementation runs these cases and must produce exactly the records in each case's
`expected.jsonl` (compared as sets of JSON objects, after removing the fields listed under `ignore`).

A case is a folder with a `case.json`:

| Key | Meaning |
|---|---|
| `connection` | The connection. String values may use `{{caseDir}}` (this folder), `{{workDir}}` (a fresh temporary folder) and `{{baseUrl}}` (the fixture HTTP server). |
| `setup.sqlite` | SQL script the harness runs to create the database before the case. |
| `setup.files` | Files to put in the work folder before the case: `copy` maps a path under `{{workDir}}` to a file of the case; `delete` lists paths under `{{workDir}}` to remove. |
| `definitions` | Optional: a folder of extra definitions the case loads (for example one that `extends` a built-in). |
| `server.routes` | Routes for a fixture HTTP server. `require.headers` must be present on every request (else 401); a route's own `require` replaces it for that route (`{}` for an OAuth token endpoint). A route matches `method` (default GET), `path` exactly, every `query` entry and every `form` entry (fields of a form-encoded request body); `null` means the parameter must be absent. It answers with the text file in `body` (with `{{baseUrl}}` replaced), or the bytes of `file` as they are, or nothing when the route has neither, with the route's `status` (default 200), `contentType` (default `application/json`, or `application/octet-stream` for `file`) and response `headers` (a list value sends the header once per item). A required header, `query` or `form` value written as `{"pattern": "<regex>"}` must match that regular expression as a whole (signatures and JWTs change with the time). `{{baseUrl}}` in a body, a header value or a `query` / `form` value is the fixture server's own URL, so absolute next-page links point back at it. First match wins; no match is 404. |
| `pipeline.aspects` | Aspects to extract into a memory sink. |
| `sample` | Optional: also call `sample(scope, dataset, limit)`; the rows are compared with `expected-sample.json`. |
| `ignore` | Per entity, fields removed before comparing (values that legitimately differ between machines or drivers). |
| `expected` | The expected records, one JSON object per line. |
| `incremental` | Optional: incremental runs ([docs/incremental.md](../docs/incremental.md)) of the same pipeline, with the envelope on, tenant `incremental.tenantId` and an in-memory checkpoint store kept across the runs. Each item of `runs` may first apply `before.sqlite` (a SQL script on the case's database) and `before.files` (as `setup.files`) and may set `full` (a reconciliation run); its events are compared with its `expected` file like envelope events. An empty file means the run emits nothing. |
| `envelope` | Optional: run the same pipeline again with the raw-stream envelope on and tenant `envelope.tenantId`, and compare the events with `envelope.expected`. The harness removes the run-dependent `runId`, `observedAt` and `eventId` and applies `ignore` to each event's `body` before comparing. It also checks that every `eventId` equals the id recomputed from the event's own properties ([docs/envelope.md](../docs/envelope.md)). |

`auth/sigv4.json` and `auth/jwt-rs256.json` hold signing vectors for the `aws_sigv4` and `gcp_jwt` providers
([docs/auth.md](../docs/auth.md)); `sinks/csv.json` holds records and the exact CSV files the `csv` sink writes for them
([docs/sinks.md](../docs/sinks.md)).

`event-ids.json` holds test vectors for the `eventId` rule: every implementation computes the id of each `input`
and must get exactly `eventId`. Together with the per-case check above, this proves that two implementations give
the same object the same id even when its body holds machine-dependent values.

Regenerate expected files only from an implementation you have checked by hand (Java:
`-Dagentfrontier.updateConformance=true`; Python: `AGENTFRONTIER_UPDATE_CONFORMANCE=<this folder> pytest
tests/test_conformance.py`) and review the diff.
