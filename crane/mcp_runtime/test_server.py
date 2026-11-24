"""Small real MCP server used by CRANE integration tests.

It exposes harmless read-only tools, one consequential tool, and read-only
invocation/call-detail counters. It performs no network access and does not
modify real user data.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from threading import Lock
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP(
    "crane-test",
    host="127.0.0.1",
    port=int(os.environ.get("CRANE_TEST_MCP_PORT", "8765")),
)

_COUNTS: Counter[str] = Counter()
_COUNTS_LOCK = Lock()
_PROBE_STATUS: Dict[str, Any] = {"invoked": False, "values": []}
_PROBE_STATUS_FILE = os.environ.get("CRANE_MCP_TOOL_PROBE_STATUS_FILE", "")


def _record(tool_name: str) -> None:
    with _COUNTS_LOCK:
        _COUNTS[tool_name] += 1


def _write_probe_status() -> None:
    if not _PROBE_STATUS_FILE:
        return
    path = Path(_PROBE_STATUS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_PROBE_STATUS, ensure_ascii=False, sort_keys=True), encoding="utf-8")


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
    title="Tool probe",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
))
def tool_probe(value: str) -> dict:
    """Record and echo the supplied value for end-to-end tool-call verification."""
    global _PROBE_STATUS
    _record("tool_probe")
    with _COUNTS_LOCK:
        _PROBE_STATUS["invoked"] = True
        _PROBE_STATUS["values"].append(value)
        _write_probe_status()
        return {"probe": "TOOL_CALL_OK", "value": value, "invoked": True}


@mcp.tool(annotations=ToolAnnotations(
    title="Get tool probe status",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
))
def get_tool_probe_status() -> dict:
    """Return deterministic observable state for the tool_probe diagnostic."""
    _record("get_tool_probe_status")
    with _COUNTS_LOCK:
        return {
            "invoked": bool(_PROBE_STATUS["invoked"]),
            "values": list(_PROBE_STATUS["values"]),
        }


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
