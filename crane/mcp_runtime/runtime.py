"""CRANE MCP runtime execution.

Handles MCP server management, tool discovery, and LangChain integration.
This module is imported by the Go supervisor for MCP integration."""
import json
import asyncio
import logging
from typing import Dict, List, Optional

from crane.mcp_runtime._init_ import (
    mcp_manager,
    MCPServer,
    TRANSPORT_STDIO,
    TRANSPORT_STREAMABLE_HTTP,
    discover_tools,
    convert_mcp_to_langchain_tools,
    get_all_tools,
    initialize_all_servers,
    connect_server,
    disconnect_server,
)

logger = logging.getLogger("crane.mcp.runtime")


async def runtime_health_check() -> dict:
    """Check the health of all MCP connections."""
    results = {}
    for name, server in mcp_manager._servers.items():
        results[name] = {
            "connected": server.is_connected,
            "initialized": server.is_initialized,
            "tool_count": len(server.tools) if server.tools else 0,
        }
    return {"status": "healthy" if all(s.is_connected for s in mcp_manager._servers.values()) else "degraded", "servers": results}


async def discover_all_tools() -> Dict[str, List]:
    """Discover tools from all connected MCP servers."""
    results = {}
    for name, server in mcp_manager._servers.items():
        try:
            if not server._initialized:
                await discover_tools(server)
            results[name] = server.tools or []
        except Exception as e:
            results[name] = [{"error": str(e)}]
    return results


def get_langchain_tools() -> List:
    """Get all MCP tools converted to LangChain Tool objects."""
    return convert_mcp_to_langchain_tools({name: s for name, s in mcp_manager._servers.items() if s.tools})


def list_servers() -> List[dict]:
    """List all registered MCP servers for Go supervisor."""
    return [s.to_dict() for s in mcp_manager._servers.values()]


async def reload_all_servers() -> dict:
    """Reload/reinitialize all MCP servers."""
    disconnect_results = {}
    for name in list(mcp_manager._servers.keys()):
        disconnect_results[name] = await disconnect_server(name)
    
    init_results = await initialize_all_servers()
    
    return {
        "disconnected": disconnect_results,
        "initialized": init_results,
    }