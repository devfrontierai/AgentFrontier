# agentfrontier-capabilities

Generative and validating capabilities over the AgentFrontier SDK (phase 5 of
[AGENTFRONTIER-PLAN.md](../../../common-docs/AGENTFRONTIER-PLAN.md)). Independent of any agent framework; the LLM is
optional and sits behind a provider protocol.

| Module | What |
|---|---|
| `llm` | Provider protocol and clients: Anthropic, OpenAI, Azure OpenAI, Gemini, OpenAI-compatible. Structured (JSON) output with one retry, a token estimate, a call budget per day and per task, a scripted client for tests |
| `sqlcheck` | `validate_sql`: parse in the dialect, one read-only statement, tables and columns against the catalog (with "did you mean"), lint, transpile, policy per table, `EXPLAIN` cost through a callback. Never executes |
| `nl2sql` | `assemble_context` (relevant tables, join conditions, business terms, policy applied), `generate_sql` (one query, validated, repaired once, run only through the caller's guarded callback, with a provenance manifest), `evaluate` (accuracy against reference queries) |
| `dq` | Data-quality rules recommended from constraints, values and profiles; SQL checks, dbt `schema.yml`, Great Expectations suites, SodaCL |
| `codegen` | dbt model files with `ref()` / `source()`, dbt SQL back to plain SQL for validation, Python compile checks |
| `profiles` | Capability profiles (`capability.json` and `instructions.md`): `nl_to_sql`, `validate_sql`, `dq_rules`, `dbt_model` |

The cache contract and provenance manifests these use are in the core SDK (`agentfrontier.cache`,
`agentfrontier.provenance`).

```bash
pip install -e ../.. -e .
```
