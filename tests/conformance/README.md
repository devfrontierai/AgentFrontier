# Conformance cases

Every AgentFrontier implementation runs these cases and must produce exactly the records in each case's
`expected.jsonl` (compared as sets of JSON objects, after removing the fields listed under `ignore`).

A case is a folder with a `case.json`:

| Key | Meaning |
|---|---|
| `connection` | The connection. String values may use `{{caseDir}}` (this folder), `{{workDir}}` (a fresh temporary folder) and `{{baseUrl}}` (the fixture HTTP server). |
| `setup.sqlite` | SQL script the harness runs to create the database before the case. |
| `server.routes` | Routes for a fixture HTTP server. `require.headers` must be present on every request (else 401). A route matches `method` (default GET), `path` exactly and every `query` entry (`null` means the parameter must be absent) and answers with the JSON file in `body`. First match wins; no match is 404. |
| `pipeline.aspects` | Aspects to extract into a memory sink. |
| `sample` | Optional: also call `sample(scope, dataset, limit)`; the rows are compared with `expected-sample.json`. |
| `ignore` | Per entity, fields removed before comparing (values that legitimately differ between machines or drivers). |
| `expected` | The expected records, one JSON object per line. |
| `envelope` | Optional: run the same pipeline again with the raw-stream envelope on and tenant `envelope.tenantId`, and compare the events with `envelope.expected`. The harness removes the run-dependent `runId`, `observedAt` and `eventId` and applies `ignore` to each event's `body` before comparing. It also checks that every `eventId` equals the id recomputed from the event's own properties ([docs/envelope.md](../docs/envelope.md)). |

`event-ids.json` holds test vectors for the `eventId` rule: every implementation computes the id of each `input`
and must get exactly `eventId`. Together with the per-case check above, this proves that two implementations give
the same object the same id even when its body holds machine-dependent values.

Regenerate expected files only from an implementation you have checked by hand (Java:
`-Dagentfrontier.updateConformance=true`) and review the diff.
