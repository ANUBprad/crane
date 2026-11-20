"""Small real MCP server used by CRANE integration tests.

It exposes one harmless read-only tool and one consequential tool. It performs
no network access and does not modify real user data.
"""
from __future__ import annotations

import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP(
    "crane-test",
    host="127.0.0.1",
    port=int(os.environ.get("CRANE_TEST_MCP_PORT", "8765")),
)


@mcp.tool(annotations=ToolAnnotations(
    title="Read-only echo",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
))
def get_test_echo(value: str) -> dict:
    """Return the supplied text without changing any state."""
    return {"echo": value}


@mcp.tool(annotations=ToolAnnotations(
    title="Delete test record",
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
))
def delete_test_record(record_id: str) -> dict:
    """Simulate a consequential deletion for policy tests."""
    return {"deleted": record_id, "side_effect": "test_only"}


def main() -> None:
    if "--http" in sys.argv:
        if "--port" in sys.argv:
            index = sys.argv.index("--port")
            if index + 1 >= len(sys.argv):
                raise SystemExit("--port requires a value")
            mcp.settings.port = int(sys.argv[index + 1])
        mcp.run("streamable-http")
        return
    mcp.run("stdio")


if __name__ == "__main__":
    main()
