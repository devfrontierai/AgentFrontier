"""Use case 3 (part 2): give any MCP client (Claude Code, Claude Desktop, Cursor, ...) read-only access to a source.

    pip install mcp
    python examples/mcp_server.py                         # serves the bundled sample database over stdio
    python examples/mcp_server.py path/to/connection.json # serves your own source

Register it with Claude Code (use absolute paths):

    claude mcp add shop-data -- python /path/to/examples/mcp_server.py /path/to/connection.json

The four tools come from ``agent_tools.DataSourceTools``; see examples/README.md for the Claude Desktop config.
Secrets stay in the connection file as ``${env:NAME}`` references, so pass them through the client's ``env`` block.
"""

from __future__ import annotations

import os
import sys

try:
    from mcp.server.mcpserver import MCPServer  # mcp >= 2
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer  # mcp 1.x
from mcp.types import ToolAnnotations

from agent_tools import DESCRIPTIONS, DataSourceTools
from common import load_connection

connection = load_connection(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENTFRONTIER_CONNECTION"))
tools = DataSourceTools(connection, allow_queries=os.environ.get("AGENTFRONTIER_ALLOW_QUERIES", "1") != "0")

server = MCPServer(
    "agentfrontier",
    instructions=f"Read-only access to the data source '{connection.id or connection.type}' ({connection.type}). "
                 "Start with list_datasets, then describe_dataset before writing SQL for run_query.",
)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


@server.tool(description=DESCRIPTIONS["list_datasets"], annotations=READ_ONLY)
def list_datasets(filter: str = "") -> dict:
    return tools.call("list_datasets", {"filter": filter})


@server.tool(description=DESCRIPTIONS["describe_dataset"], annotations=READ_ONLY)
def describe_dataset(name: str) -> dict:
    return tools.call("describe_dataset", {"name": name})


@server.tool(description=DESCRIPTIONS["sample_rows"], annotations=READ_ONLY)
def sample_rows(name: str, limit: int = 5) -> dict:
    return tools.call("sample_rows", {"name": name, "limit": limit})


@server.tool(description=DESCRIPTIONS["run_query"], annotations=READ_ONLY)
def run_query(sql: str, max_rows: int = 50) -> dict:
    return tools.call("run_query", {"sql": sql, "max_rows": max_rows})


if __name__ == "__main__":
    server.run()  # stdio; standard output belongs to the protocol, so nothing else may print to it
