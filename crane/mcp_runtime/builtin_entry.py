"""Trusted stdio entry point for CRANE's built-in MCP tools.

This script exists so the built-in server can be launched with an ordinary
``python <script>`` command, without widening the general MCP policy to allow
arbitrary ``-m`` module execution.
"""
from __future__ import annotations

from crane.mcp_runtime.builtin_server import mcp


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
