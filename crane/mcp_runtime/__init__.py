"""CRANE MCP runtime.

Provides MCP server management (stdio and streamable HTTP transports),
tool discovery, and LangChain adapter integration.
"""
__version__ = "0.1.0"

import json
import logging
import asyncio
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import mcp
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.types import (
    CallToolResult,
    ListToolsResult,
    Tool,
    TextContent,
    ImageContent,
    EmbeddedResource,
)

logger = logging.getLogger("crane.mcp")

# Protocol version compatibility with the Go supervisor
MCP_PROTOCOL_VERSION = "0.1.0"

# Supported transport types
TRANSPORT_STDIO = "stdio"
TRANSPORT_STREAMABLE_HTTP = "streamable_http"


class MCPServer:
    """Represents a connected MCP server with its configuration and tools."""
    
    def __init__(
        self,
        name: str,
        transport: str,
        server_id: str,
        parameters: dict,
        capabilities: dict,
        tools: Optional[List[Tool]] = None,
    ):
        self.name = name
        self.transport = transport
        self.server_id = server_id  # Unique identity, preserved across calls
        self.parameters = parameters
        self.capabilities = capabilities
        self.tools = tools or []
        self._session: Optional[any] = None
        self._connected = False
        self._initialized = False
        self._last_tool_discovery: Optional[float] = None
    
    @property
    def is_connected(self) -> bool:
        return self._connected
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized
    
    def to_dict(self) -> dict:
        """Serialize server state for persistence."""
        return {
            "name": self.name,
            "transport": self.transport,
            "server_id": self.server_id,
            "capabilities": self.capabilities,
            "tool_count": len(self.tools),
            "connected": self._connected,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "MCPServer":
        """Deserialize server state from persistence."""
        server = cls(
            name=data["name"],
            transport=data["transport"],
            server_id=data["server_id"],
            parameters=data.get("parameters", {}),
            capabilities=data.get("capabilities", {}),
        )
        server._connected = data.get("connected", False)
        server._initialized = data.get("initialized", False)
        server._last_tool_discovery = data.get("_last_tool_discovery")
        return server


class MCPClientManager:
    """Manages MCP server connections, discovery, and lifecycle."""
    
    def __init__(self):
        self._servers: Dict[str, MCPServer] = {}  # name -> server
        self._server_lock = asyncio.Lock()
    
    async def add_server(
        self,
        name: str,
        transport: str,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
    ) -> MCPServer:
        """Register a new MCP server."""
        async with self._server_lock:
            server = MCPServer(
                name=name,
                transport=transport,
                server_id=str(uuid.uuid4()),
                parameters={
                    "command": command if transport == TRANSPORT_STDIO else "",
                    "url": url if transport == TRANSPORT_STREAMABLE_HTTP else "",
                    "args": args or [],
                    "env": env or {},
                },
                capabilities={},
            )
            self._servers[name] = server
            logger.info(f"Registered MCP server: {name} ({transport})")
            return server
    
    async def remove_server(self, name: str) -> bool:
        """Remove an MCP server and clean up its connection."""
        async with self._server_lock:
            server = self._servers.get(name)
            if server and server._connected:
                await self._safe_disconnect(server)
            self._servers.pop(name, None)
            logger.info(f"Removed MCP server: {name}")
            return True
    
    async def list_servers(self) -> List[MCPServer]:
        """List all registered MCP servers."""
        async with self._server_lock:
            return list(self._servers.values())
    
    async def get_server(self, name: str) -> Optional[MCPServer]:
        """Get a server by name."""
        async with self._server_lock:
            return self._servers.get(name)
    
    async def get_all_tools(self) -> Dict[str, MCPServer]:
        """Get all tools keyed by server name."""
        async with self._server_lock:
            result = {}
            for name, server in self._servers.items():
                if server.is_connected and server.tools:
                    result[name] = server
            return result
    
    async def _safe_disconnect(self, server: MCPServer) -> None:
        """Safely disconnect a server, handling both transport types."""
        try:
            if server.transport == TRANSPORT_STDIO and server._session:
                # stdio_client is an async context manager; we need to exit it
                if hasattr(server._session, 'aclose'):
                    await server._session.aclose()
                elif hasattr(server._session, 'close'):
                    await server._session.close()
                server._connected = False
            elif server.transport == TRANSPORT_STREAMABLE_HTTP and server._session:
                await server._session._client.aclose()
                server._connected = False
        except Exception as e:
            logger.warning(f"Error disconnecting server {server.name}: {e}")
            server._connected = False


mcp_manager = MCPClientManager()


async def _stdio_transport(server: MCPServer) -> any:
    """Initialize a stdio transport connection to an MCP server."""
    params = server.parameters
    server_params = StdioServerParameters(
        command=params.get("command", ""),
        args=params.get("args", []),
        env=params.get("env", {}),
    )
    stdio_ctx = stdio_client(server_params)
    session = await stdio_ctx.__aenter__()
    server._session = stdio_ctx
    server._connected = True
    return session


async def _streamable_http_transport(server: MCPServer) -> any:
    """Initialize a Streamable HTTP transport connection to an MCP server."""
    params = server.parameters
    url = params.get("url", "")
    if not url:
        raise ValueError(f"No URL configured for HTTP transport server {server.name}")
    
    http_ctx = streamable_http_client(url)
    session = await http_ctx.__aenter__()
    server._session = http_ctx
    server._connected = True
    return session


async def discover_tools(server: MCPServer) -> List[Tool]:
    """Discover tools from an MCP server."""
    async with asyncio.timeout(30):
        if server.transport == TRANSPORT_STDIO:
            session = await _stdio_transport(server)
        elif server.transport == TRANSPORT_STREAMABLE_HTTP:
            session = await _streamable_http_transport(server)
        else:
            raise ValueError(f"Unknown transport: {server.transport}")
        
        try:
            # List tools request
            request = mcp.ListToolsRequest(
                headers={},
                params=mcp.ListToolsParams(
                    cursor=None,
                )
            )
            result: ListToolsResult = await session.list_tools(request)
            
            tools = []
            for tool in result.tools:
                langchain_tool = Tool(
                    name=tool.name,
                    description=tool.description or "",
                    # We'll use a wrapper that calls the MCP server
                    func=lambda name=tool.name, tool_name=server.name: _call_tool_mcp(tool_name, name),
                    handle_tool_error=True,
                )
                tools.append(langchain_tool)
            
            server.tools = tools
            server._last_tool_discovery = time.time()
            logger.info(f"Discovered {len(tools)} tools from server {server.name}")
            return tools
        except Exception as e:
            logger.error(f"Failed to discover tools from {server.name}: {e}")
            raise
        finally:
            # Don't disconnect after discovery; keep connection alive
            pass


async def _call_tool_mcp(server_name: str, tool_name: str, arguments: Optional[dict] = None) -> Any:
    """Call a tool on a specific MCP server."""
    server = mcp_manager.get_server(server_name)
    if server is None or not server._connected:
        raise RuntimeError(f"MCP server {server_name} not connected")
    
    async with asyncio.timeout(30):
        if server.transport == TRANSPORT_STDIO:
            session = await _stdio_transport(server)
        elif server.transport == TRANSPORT_STREAMABLE_HTTP:
            session = await _streamable_http_transport(server)
        
        try:
            # Tool call request
            request = mcp.CallToolRequest(
                params=mcp.CallToolParams(
                    name=tool_name,
                    arguments=arguments or {},
                )
            )
            result: CallToolResult = await session.call_tool(request)
            
            # Return structured result
            if result.content:
                # Extract text content
                text_parts = []
                for item in result.content:
                    if hasattr(item, 'text'):
                        text_parts.append(item.text)
                    elif hasattr(item, 'type'):
                        text_parts.append(str(item))
                    else:
                        text_parts.append(str(item))
                return {"text": "\n".join(text_parts), "structured": None}
            return {"text": "", "structured": None}
        except mcp.MCPError as e:
            logger.error(f"MCP error calling {tool_name} on {server_name}: {e}")
            return {"text": f"Error: {e}", "structured": None}
        except Exception as e:
            logger.error(f"Error calling {tool_name} on {server_name}: {e}")
            return {"text": f"Error: {e}", "structured": None}


def convert_mcp_to_langchain_tools(
    servers: Dict[str, MCPServer],
) -> List[Tool]:
    """Convert discovered MCP tools into LangChain Tool objects."""
    tools = []
    for server_name, server in servers.items():
        for tool in server.tools:
            # Wrap each MCP tool with server identity preserved
            wrapped_func = lambda args, sn=server_name, tn=tool.name: _call_tool_mcp(sn, tn, args)
            langchain_tool = Tool(
                name=tool.name,
                description=tool.description or f"MCP tool from {server_name}",
                func=wrapped_func,
                handle_tool_error=True,
            )
            tools.append(langchain_tool)
    return tools


def get_all_tools() -> Dict[str, List[Tool]]:
    """Get all tools organized by server."""
    result = {}
    for name, server in mcp_manager._servers.items():
        if server.tools:
            result[name] = server.tools
    return result


async def initialize_all_servers() -> Dict[str, bool]:
    """Initialize all registered MCP servers and discover their tools."""
    results = {}
    for name, server in mcp_manager._servers.items():
        try:
            if not server._initialized:
                tools = await discover_tools(server)
                server._initialized = True
            results[name] = True
        except Exception as e:
            logger.error(f"Failed to initialize server {name}: {e}")
            results[name] = False
    return results


# Convenience functions for common operations

async def connect_server(name: str) -> bool:
    """Connect to a specific MCP server."""
    server = mcp_manager.get_server(name)
    if server is None:
        return False
    
    if not server._connected:
        try:
            if server.transport == TRANSPORT_STDIO:
                await _stdio_transport(server)
            elif server.transport == TRANSPORT_STREAMABLE_HTTP:
                await _streamable_http_transport(server)
            logger.info(f"Connected to MCP server: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to server {name}: {e}")
            return False
    
    return server._connected


async def disconnect_server(name: str) -> bool:
    """Disconnect from a specific MCP server."""
    server = mcp_manager.get_server(name)
    if server is None:
        return False
    
    await mcp_manager._safe_disconnect(server)
    logger.info(f"Disconnected from MCP server: {name}")
    return True