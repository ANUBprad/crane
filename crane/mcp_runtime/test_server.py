"""Small real MCP server used by CRANE integration tests.

It exposes one harmless read-only tool, one consequential tool, and a read-only
invocation counter. It performs no network access and does not modify real user
data.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from threading import Lock

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP(
    "crane-test",
    host="127.0.0.1",
    port=int(os.environ.get("CRANE_TEST_MCP_PORT", "8765")),
)

_COUNTS: Counter[str] = Counter()
_COUNTS_LOCK = Lock()


def _record(tool_name: str) -> None:
    with _COUNTS_LOCK:
        _COUNTS[tool_name] += 1


@mcp.tool(annotations=ToolAnnotations(
    title="Read-only echo",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
))
def get_test_echo(value: str) -> dict:
    """Return the supplied text without changing any state."""
    _record("get_test_echo")
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
    _record("delete_test_record")
    return {"deleted": record_id, "side_effect": "test_only"}


@mcp.tool(annotations=ToolAnnotations(
    title="Get invocation counts",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
))
def get_invocation_counts() -> dict:
    """Return deterministic observable counts for executed tools."""
    _record("get_invocation_counts")
    with _COUNTS_LOCK:
        return {"counts": dict(_COUNTS)}


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
