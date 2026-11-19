"""CRANE LangGraph workflow state.

Defines the typed state used by the LangGraph execution engine for task
orchestration, MCP tool invocation, approval workflow, and checkpoint
management. All fields are explicitly typed for inspectability and
serialization compatibility with the Go-owned SQLite store.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Workflow state enum
# ---------------------------------------------------------------------------

class TaskState(str, Enum):
    """Enum representing the lifecycle state of a CRANE task."""

    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_APPROVAL = "waiting_approval"


# ---------------------------------------------------------------------------
# Workflow configuration
# ---------------------------------------------------------------------------

MAX_GRAPH_STEPS = 20
MAX_WALL_CLOCK_MS = 60_000  # 60 seconds
DEFAULT_RETRY_LIMIT = 3
DEFAULT_TOOL_TIMEOUT_MS = 30_000  # 30 seconds


@dataclass
class WorkflowState:
    """Typed state for the CRANE LangGraph execution engine."""

    # Task identification
    task_id: str  # Unique task identifier
    user_input: str  # Original user task description
    submitted_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    # Workflow lifecycle
    state: TaskState = TaskState.QUEUED
    current_step: str = "initialization"
    graph_steps: int = 0
    wall_clock_ms: int = 0

    # MCP tool integration
    available_tools: List[Dict[str, Any]] = field(default_factory=list)
    selected_tool: Optional[Dict[str, Any]] = None
    tool_arguments: Dict[str, Any] = field(default_factory=dict)
    tool_result: Optional[Dict[str, Any]] = None
    tool_error: Optional[str] = None
    tool_retry_count: int = 0

    # Approval workflow
    approval_required: bool = False
    approval_id: Optional[str] = None
    approval_decision: Optional[str] = None
    approval_timestamp: Optional[float] = None

    # Checkpoint and recovery
    checkpoint_identifier: Optional[str] = None
    persistent_state: Dict[str, Any] = field(default_factory=dict)
    last_tool_side_effect: Optional[Dict[str, Any]] = None  # for idempotency

    # Failure and cancellation
    failure_reason: Optional[str] = None
    failure_step: Optional[str] = None
    cancelled: bool = False

    # Results
    result: Optional[Dict[str, Any]] = None
    result_summary: Optional[str] = None

    # Runtime context
    python_runtime_pid: Optional[int] = None
    supervisor_version: str = "0.1.0"


# ---------------------------------------------------------------------------
# Helper functions for state manipulation
# ---------------------------------------------------------------------------

def state_to_dict(state: WorkflowState) -> Dict[str, Any]:
    """Serialize workflow state to a dictionary for SQLite persistence."""
    return {
        "task_id": state.task_id,
        "user_input": state.user_input,
        "submitted_at": state.submitted_at,
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "state": state.state.value,
        "current_step": state.current_step,
        "graph_steps": state.graph_steps,
        "wall_clock_ms": state.wall_clock_ms,
        "available_tools": json.dumps(state.available_tools) if state.available_tools else None,
        "selected_tool": json.dumps(state.selected_tool) if state.selected_tool else None,
        "tool_arguments": json.dumps(state.tool_arguments) if state.tool_arguments else None,
        "tool_result": json.dumps(state.tool_result) if state.tool_result else None,
        "tool_error": state.tool_error,
        "tool_retry_count": state.tool_retry_count,
        "approval_required": state.approval_required,
        "approval_id": state.approval_id,
        "approval_decision": state.approval_decision,
        "approval_timestamp": state.approval_timestamp,
        "checkpoint_identifier": state.checkpoint_identifier,
        "persistent_state": json.dumps(state.persistent_state) if state.persistent_state else None,
        "last_tool_side_effect": json.dumps(state.last_tool_side_effect) if state.last_tool_side_effect else None,
        "failure_reason": state.failure_reason,
        "failure_step": state.failure_step,
        "cancelled": state.cancelled,
        "result": json.dumps(state.result) if state.result else None,
        "result_summary": state.result_summary,
        "python_runtime_pid": state.python_runtime_pid,
        "supervisor_version": state.supervisor_version,
    }


def state_from_dict(data: Dict[str, Any]) -> WorkflowState:
    """Deserialize workflow state from a dictionary loaded from SQLite."""
    # Use the locally defined TaskState enum
    state_map = {s.value: s for s in TaskState}
    ws_state = data.get("state", "queued")
    task_state = state_map.get(ws_state, TaskState.QUEUED)

    return WorkflowState(
        task_id=data["task_id"],
        user_input=data["user_input"],
        submitted_at=data.get("submitted_at", time.time()),
        started_at=data.get("started_at"),
        completed_at=data.get("completed_at"),
        state=task_state,
        current_step=data.get("current_step", "initialization"),
        graph_steps=data.get("graph_steps", 0),
        wall_clock_ms=data.get("wall_clock_ms", 0),
        available_tools=json.loads(data["available_tools"]) if data.get("available_tools") else [],
        selected_tool=json.loads(data["selected_tool"]) if data.get("selected_tool") else None,
        tool_arguments=json.loads(data["tool_arguments"]) if data.get("tool_arguments") else {},
        tool_result=json.loads(data["tool_result"]) if data.get("tool_result") else None,
        tool_error=data.get("tool_error"),
        tool_retry_count=data.get("tool_retry_count", 0),
        approval_required=data.get("approval_required", False),
        approval_id=data.get("approval_id"),
        approval_decision=data.get("approval_decision"),
        approval_timestamp=data.get("approval_timestamp"),
        checkpoint_identifier=data.get("checkpoint_identifier"),
        persistent_state=json.loads(data["persistent_state"]) if data.get("persistent_state") else {},
        last_tool_side_effect=json.loads(data["last_tool_side_effect"]) if data.get("last_tool_side_effect") else None,
        failure_reason=data.get("failure_reason"),
        failure_step=data.get("failure_step"),
        cancelled=data.get("cancelled", False),
        result=json.loads(data["result"]) if data.get("result") else None,
        result_summary=data.get("result_summary"),
        python_runtime_pid=data.get("python_runtime_pid"),
        supervisor_version=data.get("supervisor_version", "0.1.0"),
    )