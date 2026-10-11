"""Use case 3 (part 3): ask questions about a data source in plain English, answered by Claude with live data.

    pip install anthropic
    export ANTHROPIC_API_KEY=...
    python examples/claude_agent.py "Which country brings in the most revenue, and what are its top products?"
    python examples/claude_agent.py --connection my-postgres.json "Which tables have no primary key?"

Claude explores the source with the read-only tools from ``agent_tools.py`` (list, describe, sample, query) and the
Anthropic SDK's tool runner drives the loop. Every query passes AgentFrontier's SQL guard, so the agent can read but
never write.
"""

from __future__ import annotations

import argparse
import json
import sys

import anthropic
from anthropic import beta_tool

from agent_tools import DESCRIPTIONS, DataSourceTools
from common import load_connection

MODEL = "claude-opus-5-5"

SYSTEM = """You are a careful data analyst with read-only access to one data source through tools.
Discover before you query: list the datasets, describe the ones you need, and sample rows when values are unclear.
Answer from query results only and never invent numbers. If a query fails, read the error, fix the SQL and retry.
Keep the final answer short: the answer first, then the SQL you relied on in a code block."""


def make_tools(dst: DataSourceTools) -> list:
    def run(tool: str, **arguments) -> str:
        return json.dumps(dst.call(tool, arguments), default=str)

    @beta_tool(name="list_datasets", description=DESCRIPTIONS["list_datasets"])
    def list_datasets(filter: str = "") -> str:
        return run("list_datasets", filter=filter)

    @beta_tool(name="describe_dataset", description=DESCRIPTIONS["describe_dataset"])
    def describe_dataset(name: str) -> str:
        return run("describe_dataset", name=name)

    @beta_tool(name="sample_rows", description=DESCRIPTIONS["sample_rows"])
    def sample_rows(name: str, limit: int = 5) -> str:
        return run("sample_rows", name=name, limit=limit)

    @beta_tool(name="run_query", description=DESCRIPTIONS["run_query"])
    def run_query(sql: str, max_rows: int = 50) -> str:
        return run("run_query", sql=sql, max_rows=max_rows)

    return [list_datasets, describe_dataset, sample_rows, run_query]


def ask(question: str, dst: DataSourceTools) -> str:
    client = anthropic.Anthropic()
    source = f"{dst.connection.id or dst.connection.type} ({dst.connection.type})"
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=16000,
        max_iterations=20,
        system=SYSTEM,
        output_config={"effort": "medium"},
        # If a safety classifier declines a turn, the API retries it on a fallback model inside the same call.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        tools=make_tools(dst),
        messages=[{"role": "user", "content": f"Data source: {source}\n\nQuestion: {question}"}],
    )
    final = None
    for message in runner:
        for block in message.content:
            if block.type == "tool_use":
                print(f"  -> {block.name}({json.dumps(block.input)})", file=sys.stderr)
        final = message
    if final is None:
        return "(no response)"
    if final.stop_reason == "refusal":
        return "The request was declined."
    return "".join(b.text for b in final.content if b.type == "text")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("question", nargs="?", default="Which three customers spent the most, and on what products?")
    p.add_argument("--connection", help="connection JSON file (default: the bundled sample database)")
    args = p.parse_args()
    print(ask(args.question, DataSourceTools(load_connection(args.connection))))


if __name__ == "__main__":
    main()
