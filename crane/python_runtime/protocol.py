"""Strict newline-delimited JSON protocol for the Go/Python runtime boundary.

Only protocol messages are written to stdout. Diagnostics go to stderr.
"""
from __future__ import annotations

import json
import re
import sys
import threading
from typing import Any, Dict, Optional

PROTOCOL = "crane-agent/0.1.0"
VERSION = "0.1.0"
MAX_MESSAGE_BYTES = 1 << 20

TYPE_HEALTH_CHECK = "health_check"
TYPE_SHUTDOWN_REQUEST = "shutdown_request"
TYPE_RUNTIME_IDENTITY = "runtime_identity"
TYPE_TASK_SUBMISSION = "task_submission"
TYPE_TASK_CANCELLATION = "task_cancellation"
TYPE_TASK_STATE = "task_state"
TYPE_TASK_EVENT = "task_event"
TYPE_TASK_RESULT = "task_result"
TYPE_TASK_ERROR = "task_error"
TYPE_ERROR_RESPONSE = "error_response"
TYPE_SUCCESS_RESPONSE = "success_response"
TYPE_MCP_COMMAND = "mcp_command"
TYPE_MCP_RESPONSE = "mcp_response"
TYPE_TOOL_RESULT = "tool_result"

KNOWN_TYPES = {
    TYPE_HEALTH_CHECK,
    TYPE_SHUTDOWN_REQUEST,
    TYPE_RUNTIME_IDENTITY,
    TYPE_TASK_SUBMISSION,
    TYPE_TASK_CANCELLATION,
    TYPE_TASK_STATE,
    TYPE_TASK_EVENT,
    TYPE_TASK_RESULT,
    TYPE_TASK_ERROR,
    TYPE_ERROR_RESPONSE,
    TYPE_SUCCESS_RESPONSE,
    TYPE_MCP_COMMAND,
    TYPE_MCP_RESPONSE,
    TYPE_TOOL_RESULT,
}

MCP_COMMAND_OPERATIONS = {
    "configure",
    "add_server",
    "remove_server",
    "connect",
    "disconnect",
    "discover_tools",
    "invoke_tool",
    "list_servers",
    "list_tools",
    "list_invocations",
    "clear_invocations",
}

_FINGERPRINT_PATTERN = re.compile(r"^[a-f0-9]{64}$")

_WRITE_LOCK = threading.Lock()


class ProtocolError(ValueError):
    """Raised when a message violates the protocol contract."""


def _valid_task_id(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 200:
        return False
    first = value[0]
    if not (first.isascii() and (first.isalnum())):
        return False
    return all(
        ch.isascii() and (ch.isalnum() or ch in "-_.:")
        for ch in value
    )


def encode(message: Dict[str, Any]) -> str:
    """Serialize one protocol message and enforce the size bound."""
    payload = dict(message)
    payload.setdefault("version", VERSION)
    if payload.get("version") != VERSION:
        raise ProtocolError("unsupported protocol version")
    if payload.get("type") not in KNOWN_TYPES:
        raise ProtocolError("unsupported protocol message type")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError("protocol message exceeds size limit")
    return encoded


def write_stdout(message: Dict[str, Any]) -> None:
    """Write exactly one protocol line to stdout."""
    encoded = encode(message)
    with _WRITE_LOCK:
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()


def decode(line: str) -> Dict[str, Any]:
    """Parse and validate one protocol line."""
    if not line or len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError("protocol message is empty or too large")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError("protocol message is not valid JSON") from exc
    if not isinstance(message, dict):
        raise ProtocolError("protocol message must be an object")
    if message.get("version") != VERSION:
        raise ProtocolError("unsupported protocol version")
    if message.get("type") not in KNOWN_TYPES:
        raise ProtocolError("unsupported protocol message type")
    return message


def parse_task_submission(message: Dict[str, Any]) -> Dict[str, Any]:
    if message.get("type") != TYPE_TASK_SUBMISSION:
        raise ProtocolError("expected task_submission")
    task_id = message.get("task_id")
    user_input = message.get("input")
    if not _valid_task_id(task_id):
        raise ProtocolError("invalid task identifier")
    if not isinstance(user_input, str) or not user_input.strip():
        raise ProtocolError("task input must not be empty")
    if len(user_input.encode("utf-8")) > 64 * 1024:
        raise ProtocolError("task input exceeds size limit")
    checkpoint = message.get("checkpoint")
    if checkpoint is not None and not isinstance(checkpoint, dict):
        raise ProtocolError("checkpoint must be an object")

    servers = message.get("mcp_servers") or []
    if not isinstance(servers, list) or len(servers) > 32:
        raise ProtocolError("mcp_servers must be a bounded array")
    if any(not isinstance(item, dict) for item in servers):
        raise ProtocolError("each MCP server configuration must be an object")

    approvals = message.get("granted_approvals") or []
    if not isinstance(approvals, list) or len(approvals) > 128:
        raise ProtocolError("granted_approvals must be a bounded array")
    for fingerprint in approvals:
        if not isinstance(fingerprint, str) or not _FINGERPRINT_PATTERN.fullmatch(fingerprint):
            raise ProtocolError("approval fingerprint is invalid")

    conversation_id = message.get("conversation_id")
    if conversation_id is not None and not _valid_task_id(conversation_id):
        raise ProtocolError("invalid conversation identifier")
    conversation_messages = message.get("conversation_messages") or []
    if not isinstance(conversation_messages, list) or len(conversation_messages) > 200:
        raise ProtocolError("conversation_messages must be a bounded array")
    normalized_messages = []
    for item in conversation_messages:
        if not isinstance(item, dict):
            raise ProtocolError("conversation message must be an object")
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "")
        if role not in {"user", "assistant", "system"}:
            raise ProtocolError("conversation message role is invalid")
        if len(content.encode("utf-8")) > 16 * 1024:
            content = content.encode("utf-8")[: 16 * 1024].decode("utf-8", errors="ignore")
        normalized_messages.append({"role": role, "content": content})

    return {
        "task_id": task_id,
        "input": user_input,
        "resume": message.get("resume") is True,
        "checkpoint": checkpoint,
        "mcp_servers": servers,
        "granted_approvals": approvals,
        "conversation_id": conversation_id,
        "conversation_messages": normalized_messages,
    }


def parse_task_cancellation(message: Dict[str, Any]) -> Dict[str, Any]:
    if message.get("type") != TYPE_TASK_CANCELLATION:
        raise ProtocolError("expected task_cancellation")
    task_id = message.get("task_id")
    if not _valid_task_id(task_id):
        raise ProtocolError("invalid task identifier")
    return {"task_id": task_id, "reason": str(message.get("reason") or "cancelled")}


def parse_mcp_command(message: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a Go-owned MCP daemon command."""
    if message.get("type") != TYPE_MCP_COMMAND:
        raise ProtocolError("expected mcp_command")
    command_id = message.get("command_id")
    operation = message.get("operation")
    payload = message.get("payload") or {}
    if not isinstance(command_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", command_id):
        raise ProtocolError("invalid MCP command identifier")
    if operation not in MCP_COMMAND_OPERATIONS:
        raise ProtocolError("unsupported MCP command operation")
    if not isinstance(payload, dict):
        raise ProtocolError("MCP command payload must be an object")
    return {"command_id": command_id, "operation": operation, "payload": payload}


def mcp_response(
    command_id: str,
    success: bool,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    message: Dict[str, Any] = {
        "type": TYPE_MCP_RESPONSE,
        "command_id": command_id,
        "success": success,
    }
    if success:
        message["result"] = result or {}
    else:
        message["error"] = error or {"code": "mcp_command_failed", "message": "MCP command failed"}
    return message


def tool_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build the durable, versioned tool_result frame consumed by Go."""
    if not isinstance(payload, dict) or not payload.get("invocation_id"):
        raise ProtocolError("tool_result requires an invocation identifier")
    task_id = payload.get("task_id") or "mcp-daemon"
    if not _valid_task_id(task_id):
        raise ProtocolError("tool_result has an invalid task identifier")
    return {
        "type": TYPE_TOOL_RESULT,
        "task_id": task_id,
        "invocation_id": str(payload["invocation_id"]),
        "server_id": str(payload.get("server_id") or ""),
        "server_name": str(payload.get("server_name") or ""),
        "tool_name": str(payload.get("tool_name") or ""),
        "arguments": payload.get("arguments") or {},
        "result": payload.get("result") or {},
        "error": str(payload.get("error") or ""),
        "policy_decision": str(payload.get("policy_decision") or ""),
        "duration_ms": int(payload.get("duration_ms") or 0),
        "status": str(payload.get("status") or "succeeded"),
    }


def task_event(task_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "type": TYPE_TASK_EVENT,
        "task_id": task_id,
        "event_type": event_type,
        "payload": payload or {},
    }
