"""Long-lived Python MCP daemon driven by the Go supervisor.

The daemon owns real MCP sessions so repeated API invocations do not spawn a
new server process for each call. Stdout remains reserved for versioned NDJSON
protocol frames.
"""
from __future__ import annotations

import asyncio
import os
import queue
import sys
import threading
from typing import Any, Dict, Optional

from crane import mcp_runtime
from crane.mcp_runtime import (
    MCPError,
    MCPServerConfig,
    configure_manager,
    redact,
    shutdown_all,
)
from crane.python_runtime import protocol

_COMMAND_TIMEOUT_SECONDS = 120.0


class MCPProtocolRuntime:
    """Serializes MCP commands on one asyncio task and answers Go."""

    def __init__(self) -> None:
        self.version = "0.2.0"
        self._stop = threading.Event()

    def _read_stdin(self, commands: "queue.Queue[Optional[Dict[str, Any]]]") -> None:
        try:
            for line in sys.stdin:
                if self._stop.is_set():
                    break
                try:
                    commands.put(protocol.decode(line.rstrip("\r\n")))
                except protocol.ProtocolError:
                    # Malformed frames cannot be correlated to a command, and
                    # echoing their contents would risk leaking caller data.
                    print("CRANE MCP: rejected malformed protocol frame", file=sys.stderr)
        finally:
            commands.put(None)

    @staticmethod
    def _server_payload(record: Any) -> Dict[str, Any]:
        if hasattr(record, "to_dict"):
            return redact(record.to_dict())
        return redact(record)

    async def _dispatch(self, command: Dict[str, Any]) -> Dict[str, Any]:
        operation = command["operation"]
        payload = command["payload"]
        manager = mcp_runtime.mcp_manager

        if operation == "configure":
            servers = payload.get("servers") or []
            if not isinstance(servers, list) or len(servers) > 32:
                raise MCPError("mcp_invalid_configuration", "servers must be a bounded array")
            approvals = payload.get("granted_approvals") or []
            if not isinstance(approvals, list) or len(approvals) > 128:
                raise MCPError("mcp_invalid_configuration", "granted_approvals must be a bounded array")
            target = configure_manager(
                task_id=payload.get("task_id"),
                configs=servers,
                granted_fingerprints=[str(item) for item in approvals],
            )
            return {"servers": target.server_states()}

        if operation == "add_server":
            config = MCPServerConfig.from_dict(payload.get("config") or payload)
            manager.add_config(config)
            return {"server": redact(config.public_dict())}

        if operation == "remove_server":
            server_id = str(payload.get("server_id") or "")
            await manager.remove(server_id)
            return {"server_id": server_id}

        if operation == "connect":
            server_id = str(payload.get("server_id") or "")
            configs = [config for config in manager.configs() if not server_id or config.server_id == server_id]
            if not configs:
                raise MCPError("mcp_server_not_found", "MCP server is not registered or enabled")
            tools: list[Dict[str, Any]] = []
            for config in configs:
                tools.extend(self._server_payload(tool) for tool in await manager.connect(config))
            return {"connected": [config.server_id for config in configs], "tools": tools}

        if operation == "disconnect":
            server_id = str(payload.get("server_id") or "")
            if server_id:
                await manager.disconnect(server_id)
            else:
                for config in manager.configs():
                    await manager.disconnect(config.server_id)
            return {"disconnected": server_id or "all"}

        if operation == "discover_tools":
            server_id = str(payload.get("server_id") or "")
            if not server_id:
                raise MCPError("mcp_invalid_arguments", "server_id is required")
            return {"tools": [self._server_payload(tool) for tool in await manager.discover(server_id)]}

        if operation == "invoke_tool":
            server_id = str(payload.get("server_id") or "")
            tool_name = str(payload.get("tool_name") or "")
            arguments = payload.get("arguments") or {}
            if not server_id or not tool_name or not isinstance(arguments, dict):
                raise MCPError("mcp_invalid_arguments", "invoke_tool requires server_id, tool_name and object arguments")
            grants = payload.get("granted_approvals")
            manager.set_context(
                task_id=payload.get("task_id"),
                granted_fingerprints=[str(item) for item in (grants or [])]
                if isinstance(grants, list) else None,
            )
            record = await manager.invoke(server_id, tool_name, arguments)
            return {"invocation": self._server_payload(record)}

        if operation == "list_servers":
            return {"servers": redact(manager.server_states())}

        if operation == "list_tools":
            server_id = str(payload.get("server_id") or "")
            tools = manager.tools()
            if server_id:
                tools = [tool for tool in tools if tool.server_id == server_id]
            return {"tools": [self._server_payload(tool) for tool in tools]}

        if operation == "list_invocations":
            return {"invocations": manager.invocations()}

        if operation == "clear_invocations":
            manager.clear_invocations()
            return {"cleared": True}

        raise MCPError("mcp_unsupported_operation", "MCP command operation is not supported")

    async def _handle(self, message: Dict[str, Any]) -> bool:
        if message.get("type") == protocol.TYPE_HEALTH_CHECK:
            protocol.write_stdout(protocol.mcp_response(
                str(message.get("command_id") or "health"),
                True,
                {"status": "healthy", "version": self.version},
            ))
            return False
        if message.get("type") == protocol.TYPE_SHUTDOWN_REQUEST:
            protocol.write_stdout(protocol.mcp_response(
                str(message.get("command_id") or "shutdown"), True, {"stopped": True}
            ))
            return True
        try:
            command = protocol.parse_mcp_command(message)
        except protocol.ProtocolError as exc:
            protocol.write_stdout(protocol.mcp_response(
                str(message.get("command_id") or "unknown"),
                False,
                error={"code": "bad_request", "message": str(exc)},
            ))
            return False
        try:
            # MCP SDK transports bind anyio task groups to the entering task.
            # Running dispatch inline keeps long-lived sessions on this one task.
            async with asyncio.timeout(_COMMAND_TIMEOUT_SECONDS):
                result = await self._dispatch(command)
            protocol.write_stdout(protocol.mcp_response(command["command_id"], True, result))
        except Exception as exc:  # noqa: BLE001 - never leak transport internals to Go
            if isinstance(exc, MCPError):
                code, detail = exc.code, exc.message
            elif isinstance(exc, TimeoutError):
                code, detail = "mcp_command_timeout", "MCP command timed out"
            else:
                code, detail = "mcp_command_failed", f"{type(exc).__name__} during MCP command"
            protocol.write_stdout(protocol.mcp_response(
                command["command_id"], False, error={"code": code, "message": redact(detail)}
            ))
        return False

    def run(self) -> int:
        protocol.write_stdout({
            "type": protocol.TYPE_RUNTIME_IDENTITY,
            "runtime_version": self.version,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "pid": os.getpid(),
        })
        commands: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        reader = threading.Thread(target=self._read_stdin, args=(commands,), daemon=True)
        reader.start()

        async def serve() -> None:
            while True:
                message = await asyncio.to_thread(commands.get)
                if message is None:
                    break
                if await self._handle(message):
                    break

        async def run_and_close() -> None:
            try:
                await serve()
            finally:
                await shutdown_all()

        try:
            asyncio.run(run_and_close())
        finally:
            self._stop.set()
        return 0


def main() -> int:
    return MCPProtocolRuntime().run()
