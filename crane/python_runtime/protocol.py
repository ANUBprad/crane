"""Strict newline-delimited JSON protocol for the Go/Python runtime boundary.

Only protocol messages are written to stdout. Diagnostics go to stderr.
"""
from __future__ import annotations

import json
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
}

TASK_ID_PATTERN = None  # compiled lazily to avoid importing re for one check

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
    return {
        "task_id": task_id,
        "input": user_input,
        "resume": message.get("resume") is True,
        "checkpoint": checkpoint,
    }


def parse_task_cancellation(message: Dict[str, Any]) -> Dict[str, Any]:
    if message.get("type") != TYPE_TASK_CANCELLATION:
        raise ProtocolError("expected task_cancellation")
    task_id = message.get("task_id")
    if not _valid_task_id(task_id):
        raise ProtocolError("invalid task identifier")
    return {"task_id": task_id, "reason": str(message.get("reason") or "cancelled")}


def task_event(task_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "type": TYPE_TASK_EVENT,
        "task_id": task_id,
        "event_type": event_type,
        "payload": payload or {},
    }
