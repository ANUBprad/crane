"""CRANE MCP runtime.

Real MCP client lifecycle, tool discovery, schema validation and invocation.
The Python MCP SDK owns the protocol; LangChain MCP Adapters convert discovered
tools into LangChain tools for the LangGraph workflow.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import jsonschema
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent, Tool as MCPTool

from crane.python_runtime import protocol

__version__ = "0.2.0"

logger = logging.getLogger("crane.mcp")

TRANSPORT_STDIO = "stdio"
TRANSPORT_STREAMABLE_HTTP = "streamable_http"
SUPPORTED_TRANSPORTS = {TRANSPORT_STDIO, TRANSPORT_STREAMABLE_HTTP}

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_INVOCATION_TIMEOUT_SECONDS = 30.0
MIN_TIMEOUT_SECONDS = 0.1
MAX_CONNECT_TIMEOUT_SECONDS = 30.0
MAX_INVOCATION_TIMEOUT_SECONDS = 300.0

POLICY_ALLOWED = "allowed"
POLICY_APPROVAL_REQUIRED = "approval_required"
POLICY_BLOCKED = "blocked"

SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ENV_KEY_RE = re.compile(r"^CRANE_MCP_[A-Z0-9_]{1,127}$")
SENSITIVE_ENV_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|CREDENTIAL|API_?KEY|PRIVATE)", re.I)
DANGEROUS_ARGS_RE = re.compile(r"^(-c|-m|--eval|--exec|-e|--command)$", re.I)

_STDIO_ENV_BASE_ALLOWLIST = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}


def _base_stdio_env() -> Dict[str, str]:
    """Return a minimal environment for stdio MCP servers.

    Full parent environment inheritance can leak unrelated secrets to untrusted
    servers. Only common OS/runtime essentials and explicitly configured
    CRANE_MCP_* values are passed.
    """
    env: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.upper() in _STDIO_ENV_BASE_ALLOWLIST:
            env[key] = value
    return env

_SAFE_IDENTIFIERS = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_READ_ONLY_VERBS = (
    "get_", "list_", "read_", "search_", "find_", "status_", "inspect_",
    "describe_", "fetch_", "echo_", "query_", "check_",
)
_CONSEQUENTIAL_WORDS = (
    "delete", "remove", "destroy", "drop", "write", "update", "create",
    "send", "post", "publish", "execute", "run_", "command", "shell",
    "grant", "revoke", "admin", "permission", "payment", "purchase",
    "secret", "token", "credential", "password", "modify", "set_",
)

_REDACTION_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(token|secret|password|api[_-]?key|credential)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;&}\)]+)"),
)


class MCPError(RuntimeError):
    """Safe, actionable MCP configuration or execution error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def redact(value: Any) -> Any:
    """Redact likely secrets from data that may reach logs, events or models."""
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key).lower()
            if any(word in text_key for word in ("token", "secret", "password", "credential", "api_key", "apikey", "authorization")):
                result[key] = "[redacted]"
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = value
        for pattern in _REDACTION_PATTERNS:
            text = pattern.sub("[redacted]", text)
        if len(text) > 8000:
            text = text[:8000] + "…[truncated]"
        return text
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for exact-action approval fingerprints."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def action_fingerprint(task_id: Optional[str], server_id: str, tool_name: str, arguments: Dict[str, Any]) -> str:
    context = str(task_id or "").strip() or "mcp-daemon"
    payload = canonical_json({
        "task_id": context,
        "server_id": server_id,
        "tool_name": tool_name,
        "arguments": arguments,
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    # Avoid an ipaddress dependency in the hot path; only loopback literals are
    # accepted without explicit remote authorization.
    return host in {"127.0.0.1", "::1"} or host.startswith("127.")


def _parse_timeout(raw: Any, default: float, maximum: float, name: str) -> float:
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise MCPError("mcp_invalid_configuration", f"{name} must be a number of seconds.") from exc
    if value < MIN_TIMEOUT_SECONDS or value > maximum:
        raise MCPError(
            "mcp_invalid_configuration",
            f"{name} must be between {MIN_TIMEOUT_SECONDS:g} and {maximum:g} seconds.",
        )
    return value


@dataclass(frozen=True)
class MCPServerConfig:
    """Validated, non-secret-safe MCP server configuration."""

    server_id: str
    name: str
    transport: str
    command: str = ""
    args: Tuple[str, ...] = ()
    env: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    enabled: bool = True
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    invocation_timeout_seconds: float = DEFAULT_INVOCATION_TIMEOUT_SECONDS
    allow_remote_endpoint: bool = False

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "MCPServerConfig":
        if not isinstance(raw, dict):
            raise MCPError("mcp_invalid_configuration", "MCP server configuration must be an object")
        server_id = str(raw.get("server_id") or raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        transport = str(raw.get("transport") or "").strip().lower()
        if not SERVER_ID_RE.fullmatch(server_id):
            raise MCPError("mcp_invalid_configuration", "MCP server id is invalid")
        if not name or len(name) > 128 or "\n" in name or "\r" in name:
            raise MCPError("mcp_invalid_configuration", "MCP server name is invalid")
        if transport not in SUPPORTED_TRANSPORTS:
            raise MCPError("mcp_invalid_configuration", "MCP transport is not supported")

        command = str(raw.get("command") or "").strip()
        args_raw = raw.get("args") or []
        env_raw = raw.get("env") or {}
        url = str(raw.get("url") or "").strip()
        allow_remote = raw.get("allow_remote_endpoint") is True

        if transport == TRANSPORT_STDIO:
            allowed_commands = {
                item.strip()
                for item in os.environ.get(
                    "CRANE_MCP_ALLOWED_COMMANDS", "python,pythonw,node"
                ).split(",")
                if item.strip()
            }
            if command not in allowed_commands:
                raise MCPError(
                    "mcp_executable_not_allowed",
                    "MCP stdio executable is not on the explicit allowlist",
                )
            if not isinstance(args_raw, (list, tuple)) or len(args_raw) > 32:
                raise MCPError("mcp_invalid_configuration", "MCP stdio arguments are invalid")
            args: List[str] = []
            for item in args_raw:
                if not isinstance(item, str) or not item or len(item) > 1024 or any(ch in item for ch in "\x00\r\n"):
                    raise MCPError("mcp_invalid_configuration", "MCP stdio arguments are invalid")
                if DANGEROUS_ARGS_RE.match(item):
                    raise MCPError(
                        "mcp_executable_not_allowed",
                        "MCP stdio arguments may not request inline code execution",
                    )
                args.append(item)
            if not isinstance(env_raw, dict):
                raise MCPError("mcp_invalid_configuration", "MCP environment configuration is invalid")
            env: Dict[str, str] = {}
            for key, value in env_raw.items():
                if not isinstance(key, str) or not ENV_KEY_RE.fullmatch(key) or SENSITIVE_ENV_RE.search(key):
                    raise MCPError("mcp_invalid_configuration", "MCP environment variable name is not allowed")
                if not isinstance(value, str) or len(value) > 1024:
                    raise MCPError("mcp_invalid_configuration", "MCP environment variable value is invalid")
                env[key] = value
        else:
            if not url:
                raise MCPError("mcp_invalid_configuration", "MCP HTTP endpoint is required")
            try:
                parsed = urllib.parse.urlsplit(url)
                port = parsed.port
            except ValueError as exc:
                raise MCPError("mcp_invalid_configuration", "MCP HTTP endpoint is invalid") from exc
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise MCPError("mcp_invalid_configuration", "MCP HTTP endpoint must be http or https")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise MCPError("mcp_invalid_configuration", "MCP HTTP endpoint must not contain credentials, query or fragment")
            if parsed.path not in {"", "/", "/mcp"}:
                raise MCPError("mcp_invalid_configuration", "MCP HTTP endpoint path is not allowed")
            if not allow_remote and not _is_loopback_host(parsed.hostname):
                raise MCPError(
                    "mcp_remote_endpoint_not_authorized",
                    "MCP endpoint is not local; explicit remote endpoint authorization is required",
                )
            host = parsed.hostname
            netloc = f"[{host}]" if ":" in host else host
            if port is not None:
                netloc += f":{port}"
            url = f"{parsed.scheme.lower()}://{netloc}{parsed.path or '/mcp'}"
            args, env = (), {}

        return cls(
            server_id=server_id,
            name=name,
            transport=transport,
            command=command,
            args=tuple(args),
            env=env,
            url=url,
            enabled=raw.get("enabled") is not False,
            connect_timeout_seconds=_parse_timeout(
                raw.get("connect_timeout_seconds"),
                DEFAULT_CONNECT_TIMEOUT_SECONDS,
                MAX_CONNECT_TIMEOUT_SECONDS,
                "connect_timeout_seconds",
            ),
            invocation_timeout_seconds=_parse_timeout(
                raw.get("invocation_timeout_seconds"),
                DEFAULT_INVOCATION_TIMEOUT_SECONDS,
                MAX_INVOCATION_TIMEOUT_SECONDS,
                "invocation_timeout_seconds",
            ),
            allow_remote_endpoint=allow_remote,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the Go/Python protocol boundary, including env values."""
        return {
            "server_id": self.server_id,
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "url": self.url,
            "enabled": self.enabled,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "invocation_timeout_seconds": self.invocation_timeout_seconds,
            "allow_remote_endpoint": self.allow_remote_endpoint,
        }

    def public_dict(self) -> Dict[str, Any]:
        """Serialize without environment values for logs, events and APIs."""
        data = self.to_dict()
        data["env"] = {}
        data["environment_keys"] = sorted(self.env)
        return data


@dataclass
class MCPToolRecord:
    server_id: str
    tool: MCPTool
    policy_class: str

    @property
    def name(self) -> str:
        return self.tool.name

    def to_dict(self) -> Dict[str, Any]:
        annotations = self.tool.annotations.model_dump() if self.tool.annotations else {}
        return {
            "server_id": self.server_id,
            "name": self.tool.name,
            "description": self.tool.description or "",
            "input_schema": self.tool.inputSchema,
            "annotations": annotations,
            "policy_class": self.policy_class,
        }


@dataclass
class MCPInvocationRecord:
    invocation_id: str
    server_id: str
    server_name: str
    tool_name: str
    arguments: Dict[str, Any]
    status: str
    started_at: float
    completed_at: float = 0.0
    result_text: str = ""
    structured_result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    policy_decision: str = POLICY_ALLOWED
    task_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return redact({
            "invocation_id": self.invocation_id,
            "task_id": self.task_id,
            "server_id": self.server_id,
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "status": self.status,
            "result": {"text": self.result_text, "structured": self.structured_result},
            "error": self.error,
            "policy_decision": self.policy_decision,
            "duration_ms": max(0, int((self.completed_at - self.started_at) * 1000)),
        })


class _ManagedServer:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.session: Optional[ClientSession] = None
        self.stack: Optional[contextlib.AsyncExitStack] = None
        self.tools: Dict[str, MCPToolRecord] = {}
        self.connected = False
        self.last_error: Optional[str] = None

    def clear(self) -> None:
        self.session = None
        self.stack = None
        self.connected = False


def classify_tool(tool: MCPTool) -> str:
    """Classify from static tool metadata only; descriptions never grant access."""
    annotations = tool.annotations
    if annotations is not None and annotations.readOnlyHint is True:
        return POLICY_ALLOWED
    name = tool.name.lower()
    if annotations is not None and annotations.destructiveHint is True:
        return POLICY_APPROVAL_REQUIRED
    if any(word in name for word in _CONSEQUENTIAL_WORDS):
        return POLICY_APPROVAL_REQUIRED
    if name.startswith(_READ_ONLY_VERBS) and not any(word in name for word in ("write", "delete", "send", "update")):
        return POLICY_ALLOWED
    return POLICY_APPROVAL_REQUIRED


def _parse_call_result(result: Any) -> Tuple[str, Optional[Dict[str, Any]], bool]:
    if not isinstance(result, CallToolResult) or not isinstance(result.content, list):
        raise MCPError("mcp_malformed_result", "MCP server returned a malformed tool result")
    parts: List[str] = []
    for item in result.content:
        if isinstance(item, TextContent):
            parts.append(item.text)
        elif hasattr(item, "model_dump"):
            # Keep non-text evidence bounded and inspectable without dumping
            # arbitrary binary data into task history.
            parts.append(f"[{getattr(item, 'type', 'content')} content omitted]")
        else:
            raise MCPError("mcp_malformed_result", "MCP server returned an unsupported content block")
    structured = result.structuredContent
    if structured is not None and not isinstance(structured, dict):
        raise MCPError("mcp_malformed_result", "MCP server returned malformed structured content")
    if structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts), structured, bool(result.isError)


class _PolicyInterceptor:
    """Adapter interceptor that routes every model tool call through CRANE policy."""

    def __init__(self, manager: "MCPManager", server: _ManagedServer) -> None:
        self._manager = manager
        self._server = server

    async def __call__(self, request: Any, handler: Any) -> CallToolResult:
        del handler  # CRANE owns the actual MCP call so it can persist and gate it.
        record = await self._manager.invoke(
            self._server.config.server_id,
            request.name,
            request.args or {},
        )
        if record.status == "succeeded":
            return CallToolResult(
                content=[TextContent(type="text", text=record.result_text or "Tool completed")],
                structuredContent=record.structured_result,
                isError=False,
            )
        detail = record.error or "MCP tool did not complete"
        return CallToolResult(
            content=[TextContent(type="text", text=f"MCP tool failed: {detail}")],
            isError=True,
        )


class MCPManager:
    """Owns MCP sessions, discovery, policy checks and invocation records."""

    def __init__(self, task_id: Optional[str] = None, granted_fingerprints: Optional[Set[str]] = None) -> None:
        self.task_id = task_id
        self.granted_fingerprints: Set[str] = set(granted_fingerprints or ())
        self._servers: Dict[str, _ManagedServer] = {}
        self._lock = asyncio.Lock()
        self._invocations: List[MCPInvocationRecord] = []

    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        protocol.write_stdout(protocol.task_event(
            self.task_id or "mcp-daemon", event_type, redact(payload)
        ))

    def _emit_tool_result(self, record: MCPInvocationRecord) -> None:
        """Emit the exact invocation outcome for durable Go-side persistence."""
        payload = record.to_dict()
        protocol.write_stdout(protocol.tool_result({
            "task_id": self.task_id,
            "invocation_id": record.invocation_id,
            "server_id": record.server_id,
            "server_name": record.server_name,
            "tool_name": record.tool_name,
            "arguments": record.arguments,
            "result": payload["result"],
            "error": record.error,
            "policy_decision": record.policy_decision,
            "duration_ms": payload["duration_ms"],
            "status": record.status,
        }))

    def set_context(
        self,
        task_id: Optional[str] = None,
        granted_fingerprints: Optional[Sequence[str]] = None,
    ) -> None:
        """Bind the manager to one task and its exact approval grants."""
        self.task_id = task_id
        self.granted_fingerprints = set(granted_fingerprints or ())

    def add_config(self, config: MCPServerConfig) -> None:
        self._servers[config.server_id] = _ManagedServer(config)

    def configs(self) -> List[MCPServerConfig]:
        return [server.config for server in self._servers.values() if server.config.enabled]

    def tools(self) -> List[MCPToolRecord]:
        return [tool for server in self._servers.values() for tool in server.tools.values()]

    def server_tools(self, server_id: str) -> Dict[str, MCPToolRecord]:
        server = self._servers.get(server_id)
        return dict(server.tools) if server is not None else {}

    def server_states(self) -> List[Dict[str, Any]]:
        """Return API-safe server state without environment values."""
        return [{
            "config": server.config.public_dict(),
            "connected": server.connected,
            "tool_count": len(server.tools),
            "last_error": redact(server.last_error),
        } for server in self._servers.values()]

    def invocations(self) -> List[Dict[str, Any]]:
        return [record.to_dict() for record in self._invocations]

    def clear_invocations(self) -> None:
        self._invocations.clear()

    async def connect(self, config: MCPServerConfig) -> List[MCPToolRecord]:
        async with self._lock:
            managed = self._servers.get(config.server_id)
            if managed is None:
                managed = _ManagedServer(config)
                self._servers[config.server_id] = managed
            await self._disconnect_locked(managed)
            stack = contextlib.AsyncExitStack()
            try:
                if config.transport == TRANSPORT_STDIO:
                    params = StdioServerParameters(
                        command=config.command,
                        args=list(config.args),
                        env={**_base_stdio_env(), **config.env},
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                else:
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(config.url)
                    )
                session = await stack.enter_async_context(ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=config.connect_timeout_seconds),
                ))
                await session.initialize()
                managed.stack = stack
                managed.session = session
                managed.config = config
                managed.connected = True
                managed.last_error = None
                managed.tools = await self._discover_locked(managed)
                self._emit("mcp_server_discovery", {
                    "server_id": config.server_id,
                    "transport": config.transport,
                    "tool_count": len(managed.tools),
                })
                return list(managed.tools.values())
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - normalized at the boundary
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await stack.aclose()
                managed.clear()
                managed.last_error = redact(str(exc))
                raise self._normalize_error(exc, "mcp_connect_failed") from exc

    async def disconnect(self, server_id: str) -> None:
        async with self._lock:
            managed = self._servers.get(server_id)
            if managed is not None:
                await self._disconnect_locked(managed)

    async def _disconnect_locked(self, managed: _ManagedServer) -> None:
        managed.connected = False
        stack, session = managed.stack, managed.session
        managed.clear()
        if stack is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await stack.aclose()
        del session

    async def discover(self, server_id: str) -> List[MCPToolRecord]:
        async with self._lock:
            managed = self._servers.get(server_id)
            if managed is None:
                raise MCPError("mcp_server_not_found", "MCP server is not registered")
            if not managed.connected or managed.session is None:
                await self._connect_locked(managed)
            managed.tools = await self._discover_locked(managed)
            self._emit("mcp_server_discovery", {
                "server_id": server_id,
                "tool_count": len(managed.tools),
            })
            return list(managed.tools.values())

    async def _connect_locked(self, managed: _ManagedServer) -> None:
        config = managed.config
        stack = contextlib.AsyncExitStack()
        try:
            if config.transport == TRANSPORT_STDIO:
                params = StdioServerParameters(
                    command=config.command,
                    args=list(config.args),
                    env={**_base_stdio_env(), **config.env},
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            else:
                read, write, _ = await stack.enter_async_context(streamable_http_client(config.url))
            session = await stack.enter_async_context(ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=config.connect_timeout_seconds),
            ))
            await session.initialize()
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await stack.aclose()
            managed.clear()
            managed.last_error = redact(str(exc))
            raise self._normalize_error(exc, "mcp_connect_failed") from exc
        managed.stack = stack
        managed.session = session
        managed.connected = True
        managed.last_error = None

    async def _discover_locked(self, managed: _ManagedServer) -> Dict[str, MCPToolRecord]:
        if managed.session is None:
            raise MCPError("mcp_not_connected", "MCP server is not connected")
        tools: Dict[str, MCPToolRecord] = {}
        cursor: Optional[str] = None
        for _ in range(100):
            result = await managed.session.list_tools(cursor=cursor)
            if result.tools:
                for tool in result.tools:
                    if not _SAFE_IDENTIFIERS.fullmatch(tool.name):
                        logger.warning("Skipping MCP tool with an unsafe name")
                        continue
                    tools[tool.name] = MCPToolRecord(
                        server_id=managed.config.server_id,
                        tool=tool,
                        policy_class=classify_tool(tool),
                    )
            cursor = result.nextCursor or None
            if not cursor:
                break
        else:
            raise MCPError("mcp_malformed_result", "MCP tool listing did not terminate")
        return tools

    async def remove(self, server_id: str) -> None:
        async with self._lock:
            managed = self._servers.pop(server_id, None)
            if managed is not None:
                await self._disconnect_locked(managed)

    async def close_all(self) -> None:
        async with self._lock:
            for managed in list(self._servers.values()):
                await self._disconnect_locked(managed)

    @staticmethod
    def _normalize_error(exc: Exception, default_code: str) -> MCPError:
        if isinstance(exc, MCPError):
            return exc
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            return MCPError(code, redact(str(exc)))
        return MCPError(default_code, redact(f"MCP operation failed: {type(exc).__name__}"))

    async def invoke(
        self,
        server_id: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> MCPInvocationRecord:
        args = redact(arguments or {})
        if not isinstance(args, dict):
            raise MCPError("mcp_invalid_arguments", "MCP tool arguments must be an object")
        async with self._lock:
            managed = self._servers.get(server_id)
            if managed is None:
                raise MCPError("mcp_server_not_found", "MCP server is not registered")
            record = MCPInvocationRecord(
                invocation_id=str(uuid.uuid4()),
                server_id=server_id,
                server_name=managed.config.name,
                tool_name=tool_name,
                arguments=args,
                status="running",
                started_at=time.time(),
                task_id=self.task_id,
            )
            try:
                if not managed.connected or managed.session is None:
                    await self._connect_locked(managed)
                    managed.tools = await self._discover_locked(managed)
                tool = managed.tools.get(tool_name)
                if tool is None:
                    raise MCPError("mcp_tool_not_found", "MCP tool was not found on this server")

                try:
                    jsonschema.validate(instance=args, schema=tool.tool.inputSchema)
                except jsonschema.ValidationError as exc:
                    raise MCPError("mcp_schema_validation_failed", redact(exc.message)) from exc

                fingerprint = action_fingerprint(self.task_id, server_id, tool_name, args)
                record.policy_decision = tool.policy_class
                if tool.policy_class != POLICY_ALLOWED and fingerprint not in self.granted_fingerprints:
                    record.status = "blocked"
                    record.policy_decision = POLICY_APPROVAL_REQUIRED
                    record.error = "This tool requires explicit approval for this exact action"
                    self._emit("mcp_tool_blocked", record.to_dict())
                    self._finish(record)
                    self._emit_tool_result(record)
                    return record

                assert managed.session is not None
                result = await asyncio.wait_for(
                    managed.session.call_tool(
                        tool_name,
                        args,
                        read_timeout_seconds=timedelta(
                            seconds=managed.config.invocation_timeout_seconds
                        ),
                    ),
                    timeout=managed.config.invocation_timeout_seconds,
                )
                text, structured, is_error = _parse_call_result(result)
                record.result_text = redact(text)
                record.structured_result = redact(structured) if structured is not None else None
                record.status = "failed" if is_error else "succeeded"
                if is_error:
                    record.error = redact(record.result_text) or "MCP tool reported an error"
                elif tool.policy_class != POLICY_ALLOWED:
                    self.granted_fingerprints.discard(fingerprint)
            except Exception as exc:  # noqa: BLE001 - never leak transport internals
                normalized = self._normalize_error(exc, "mcp_invocation_failed")
                record.status = "failed"
                record.error = f"{normalized.code}: {normalized.message}"
                managed.last_error = record.error
            self._finish(record)
            self._emit("mcp_tool_invocation", record.to_dict())
            self._emit_tool_result(record)
            return record

    def _finish(self, record: MCPInvocationRecord) -> MCPInvocationRecord:
        record.completed_at = time.time()
        self._invocations.append(record)
        return record

    def langchain_tools(self) -> List[Any]:
        """Convert discovered MCP tools to policy-aware LangChain tools."""
        from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool

        tools: List[Any] = []
        for server in self._servers.values():
            if not server.connected or server.session is None:
                continue
            prefix = re.sub(r"[^A-Za-z0-9_]", "_", server.config.server_id)
            for tool in server.tools.values():
                tools.append(convert_mcp_tool_to_langchain_tool(
                    server.session,
                    tool.tool,
                    server_name=prefix,
                    tool_name_prefix=True,
                    tool_interceptors=[_PolicyInterceptor(self, server)],
                    handle_tool_errors=False,
                ))
        return tools


# Global manager used by the one-task-per-process protocol runtime.
mcp_manager = MCPManager()


def configure_manager(
    task_id: Optional[str] = None,
    configs: Optional[Sequence[Dict[str, Any]]] = None,
    granted_fingerprints: Optional[Sequence[str]] = None,
) -> MCPManager:
    """Reset the process-wide manager for one task submission."""
    global mcp_manager
    mcp_manager = MCPManager(task_id, set(granted_fingerprints or ()))
    for raw in configs or []:
        mcp_manager.add_config(MCPServerConfig.from_dict(raw))
    return mcp_manager


async def connect_all(manager: Optional[MCPManager] = None) -> Dict[str, bool]:
    target = manager or mcp_manager
    results: Dict[str, bool] = {}
    for config in target.configs():
        try:
            await target.connect(config)
            results[config.server_id] = True
        except MCPError as exc:
            logger.warning("MCP connect failed for %s (%s)", config.server_id, exc.code)
            results[config.server_id] = False
    return results


async def shutdown_all(manager: Optional[MCPManager] = None) -> None:
    target = manager or mcp_manager
    await target.close_all()


__all__ = [
    "MCPError", "MCPInvocationRecord", "MCPManager", "MCPServerConfig", "MCPToolRecord",
    "POLICY_ALLOWED", "POLICY_APPROVAL_REQUIRED", "POLICY_BLOCKED",
    "TRANSPORT_STDIO", "TRANSPORT_STREAMABLE_HTTP",
    "action_fingerprint", "canonical_json", "classify_tool", "configure_manager",
    "connect_all", "mcp_manager", "redact", "shutdown_all",
]
