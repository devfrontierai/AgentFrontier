# Examples: three things you can build with AgentFrontier

Each example is a single script that runs **immediately** against a bundled SQLite sample shop database (customers,
products, orders, order items, a revenue view). To use your own data, pass a connection file. The same script then
works on PostgreSQL, MySQL, Oracle, Snowflake, SQL Server, Redshift, Unity Catalog, Elasticsearch or a folder of
CSV/JSON files, because AgentFrontier returns the same record model for every source.

| # | Use case | Who it's for | Script | Extra install |
|---|---|---|---|---|
| 1 | [Data dictionary generator](#1-data-dictionary-generator) | Data and platform teams who need current schema docs | `data_dictionary.py` | none |
| 2 | [Schema drift check for CI](#2-schema-drift-check-for-ci) | Teams whose apps, pipelines or dashboards break when a schema changes | `schema_drift.py` | none |
| 3 | [Read-only database tools for AI agents](#3-read-only-database-tools-for-ai-agents) | Anyone connecting an LLM agent (Claude, MCP clients, your own loop) to real data | `agent_tools.py`, `mcp_server.py`, `claude_agent.py` | `mcp` and/or `anthropic` |

## Setup (once)

```bash
git clone https://github.com/devfrontierai/AgentFrontier.git
cd AgentFrontier
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
```

Run the examples from the repository root. The sample database is created at `examples/data/shop.db` the first
time an example needs it (`python examples/sample_db.py` rebuilds it).

### Using your own source

Write a connection file (see [docs/getting-started.md](../docs/getting-started.md)) and pass it as the first
argument. For PostgreSQL:

```json
{
  "id": "shop",
  "type": "postgresql",
  "properties": {"host": "localhost", "port": 5432, "database": "shopdb"},
  "auth": {"method": "username_password", "username": "reader", "password": "${env:SHOP_PASSWORD}"},
  "options": {"includeSchemas": ["shop"]}
}
```

```bash
pip install -e ".[postgres]"
agentfrontier test shop.json                       # check connectivity first
python examples/data_dictionary.py shop.json
```

Passwords stay out of the file: `${env:NAME}` and `${file:/path}` references are resolved when a session opens and
are never printed.

---

## 1. Data dictionary generator

**Problem:** schema documentation goes stale the day it is written, and every database documents itself differently.

**What it does:** extracts metadata through the pipeline API into an in-memory sink and renders one Markdown page:
a dataset overview (type, row count, description), a Mermaid ER diagram of the foreign keys, and per dataset its
columns, types, nullability, defaults, PK/FK/UQ markers, comments, references, indexes and view definitions.

```bash
python examples/data_dictionary.py                              # writes out/data-dictionary.md
python examples/data_dictionary.py shop.json -o docs/schema.md  # your own source
```

The output renders on GitHub, GitLab and in most Markdown viewers, with the ER diagram included. On PostgreSQL the
column and table comments (`COMMENT ON ...`) become the descriptions.

**Make it yours:** run it in a nightly job and commit the result, publish it to your wiki, or add aspects such as
`governance` (grants, policies) or `lineage` to the `PipelineConfig` and render those too.

**SDK features shown:** `AgentFrontier.run`, `PipelineConfig`, the `memory` sink, canonical `dataset` / `field` /
`constraint` / `index` records, hierarchy-aware naming (`database.schema.table`).

---

## 2. Schema drift check for CI

**Problem:** someone drops a column or changes a type, and you find out when a dashboard, an ETL job or the app
breaks in production.

**What it does:** `snapshot` records the schema you depend on as a small, sorted JSON file that you commit. `check`
compares the live schema with that baseline and classifies each difference:

| Severity | Examples |
|---|---|
| breaking | table or column removed, type changed, column became NOT NULL, NOT NULL column added without a default |
| warning | constraint added or removed, index removed, column became nullable, default changed |
| info | table, nullable column or index added |

It exits with status 1 when a change reaches `--fail-on` (`breaking` by default; also `warning`, `any`, `never`),
and `--markdown` writes a report you can post as a PR comment.

Try the whole loop on the sample:

```bash
python examples/schema_drift.py snapshot -o out/schema-baseline.json
python examples/sample_db.py --migrate          # a "release": adds, drops and retypes columns
python examples/schema_drift.py check --baseline out/schema-baseline.json --markdown out/drift.md
```

```text
BREAKING  customers: column `phone` removed
BREAKING  products: column `price` type changed: REAL -> TEXT
warning   orders: index removed: index(ordered_at) (queries may slow down)
info      customers: column `loyalty_points` added (INTEGER)
info      orders: column `shipped_at` added (TEXT)
info      shipments: table added
```

Run `python examples/sample_db.py` afterwards to restore the original sample.

**In CI** (GitHub Actions; adapt the same steps to any runner):

```yaml
name: schema-drift
on:
  schedule: [{cron: "0 6 * * *"}]   # daily, and/or on pull requests that touch migrations
  workflow_dispatch:
jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install "agentfrontier[postgres] @ git+https://github.com/devfrontierai/AgentFrontier.git@main"
      - run: python tools/schema_drift.py check db/warehouse.json --baseline db/schema-baseline.json --markdown drift.md
        env:
          WAREHOUSE_PASSWORD: ${{ secrets.WAREHOUSE_PASSWORD }}
      - if: always()
        run: cat drift.md >> "$GITHUB_STEP_SUMMARY"
```

Copy `schema_drift.py` and `common.py` into your repository (here `tools/`) together with `sample_db.py`, or replace
`load_connection` with `ConnectionConfig.from_file`. When a change is intended, run `snapshot` again and commit the
new baseline in the same PR as the migration.

**SDK features shown:** the same pipeline API as use case 1, and comparison by content (constraint and index
names generated by the engine do not cause false alarms).

---

## 3. Read-only database tools for AI agents

**Problem:** agents are useful on real data only if they can discover the schema and query it, but handing an LLM a
database connection is risky, and writing tools for every database is repetitive.

**What it does:** `agent_tools.py` turns any AgentFrontier connection into four tools:

| Tool | Returns |
|---|---|
| `list_datasets(filter)` | tables, views and files with type, description and row count |
| `describe_dataset(name)` | columns, keys, foreign keys and indexes |
| `sample_rows(name, limit)` | the first rows (at most 200) |
| `run_query(sql, max_rows)` | rows of one read-only statement (at most 200) |

Safety comes from the SDK, not from the prompt: `query()` rejects anything but a single read statement (no
`INSERT`/`UPDATE`/`DELETE`, DDL, `GRANT`, multiple statements or dangerous functions), runs it in a rolled-back
transaction with a statement timeout, and caps the rows. Errors come back as `{"error": code, "message": ...}`
so the model can fix its SQL and retry. Connect with a database user that only has `SELECT` on what the agent may
see. That user is the real permission boundary, especially for columns holding personal data.

See the tools work without any LLM:

```bash
python examples/agent_tools.py      # lists, describes and samples, then shows a DELETE being rejected
```

### 3a. As an MCP server (Claude Code, Claude Desktop, Cursor, ...)

```bash
pip install mcp
```

Claude Code (use the Python of the environment where `agentfrontier` and `mcp` are installed):

```bash
claude mcp add shop-data -- /absolute/path/to/.venv/bin/python /absolute/path/to/AgentFrontier/examples/mcp_server.py
```

Claude Desktop (`claude_desktop_config.json`), pointing at your own source:

```json
{
  "mcpServers": {
    "warehouse": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/AgentFrontier/examples/mcp_server.py", "/absolute/path/to/warehouse.json"],
      "env": {"WAREHOUSE_PASSWORD": "..."}
    }
  }
}
```

Then ask: *"Which country brings in the most revenue? Show me the SQL."* Set `AGENTFRONTIER_ALLOW_QUERIES=0` to
expose metadata and samples only. The server works with both `mcp` 1.x and 2.x.

### 3b. With the Claude API (your own app)

```bash
pip install anthropic
export ANTHROPIC_API_KEY=...
python examples/claude_agent.py "Which three customers spent the most, and on what products?"
python examples/claude_agent.py --connection shop.json "Which tables have no primary key?"
```

The script passes the four tools to the Anthropic SDK's tool runner, which runs the loop: Claude lists the
datasets, describes the ones it needs, writes SQL, reads errors and retries, then answers with the SQL it used.
Each tool call is printed to standard error so you can watch it work.

### 3c. Any other framework

`DataSourceTools.call(tool_name, arguments)` takes and returns plain dicts, and `agent_tools.DESCRIPTIONS` holds the
tool descriptions. Wrap them as LangChain `StructuredTool`s, LlamaIndex `FunctionTool`s, OpenAI-style function
schemas or your own dispatcher. That takes a few lines and needs no AgentFrontier changes.

**SDK features shown:** the session (pull) API: `open`, `containers`, `datasets`, `records`, `sample`, `query`
with `QueryRequest`, the SQL guard, stable error codes.

---

## Files

| File | Purpose |
|---|---|
| `sample_db.py` | builds the sample database (`--migrate` applies the drift demo's schema change) |
| `common.py` | `load_connection` (falls back to the sample) and `database.schema.table` naming helpers |
| `data_dictionary.py` | use case 1 |
| `schema_drift.py` | use case 2 |
| `agent_tools.py` | use case 3: the framework-agnostic tools |
| `mcp_server.py` | use case 3: MCP server over stdio |
| `claude_agent.py` | use case 3: Claude tool-use agent |

Next: [docs/integration.md](../docs/integration.md) shows how to add AgentFrontier to your own project.
