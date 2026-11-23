"""CRANE LangGraph workflow state.

Defines the typed state used by the LangGraph execution engine for task
orchestration, MCP tool invocation, approval workflow, and checkpoint
management. All fields are explicitly typed for inspectability and
serialization compatibility with the Go-owned SQLite store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


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


class ExecutionStatus(str, Enum):
    """Rich execution outcome exposed in results/checkpoints."""

    FULLY_COMPLETED = "fully_completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_USER_INPUT = "waiting_for_user_input"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    RUNNING = "running"


class EvaluationOutcome(str, Enum):
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    NEEDS_MORE_INFORMATION = "needs_more_information"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_REPLANNING = "needs_replanning"


# ---------------------------------------------------------------------------
# Workflow configuration / limits
# ---------------------------------------------------------------------------

MAX_GRAPH_STEPS = 24
MAX_WALL_CLOCK_MS = 180_000  # 180 seconds overall task timeout
DEFAULT_RETRY_LIMIT = 2
MAX_PLAN_REVISIONS = 3
MAX_EXECUTION_STEPS = 12
MAX_RETRIES_PER_STEP = 2
MAX_TOOL_CALLS_PER_TASK = 20
MAX_MODEL_CALLS_PER_TASK = 24
DEFAULT_TOOL_TIMEOUT_MS = 30_000
MAX_TOOL_OUTPUT_CHARS = 20_000
MAX_TOOL_ERROR_CHARS = 2_000
MAX_CONTEXT_MESSAGES = 40
MAX_MODEL_CONTEXT_CHARS = 48_000


# ---------------------------------------------------------------------------
# Structured records
# ---------------------------------------------------------------------------

@dataclass
class Transition:
    """Records a single state transition in the workflow graph."""

    from_node: str
    to_node: str
    timestamp: float = field(default_factory=time.time)
    reason: str = ""


@dataclass
class PlanStep:
    """A validated, actionable execution step."""

    step_id: str
    description: str
    expected_outcome: str
    dependencies: List[str] = field(default_factory=list)
    required_capabilities: List[str] = field(default_factory=list)
    required_tools: List[str] = field(default_factory=list)
    risk_classification: str = "low"
    approval_required: bool = False
    completion_criteria: str = ""
    status: str = "pending"  # pending|running|completed|failed|skipped|waiting_approval|waiting_input

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "expected_outcome": self.expected_outcome,
            "dependencies": list(self.dependencies),
            "required_capabilities": list(self.required_capabilities),
            "required_tools": list(self.required_tools),
            "risk_classification": self.risk_classification,
            "approval_required": bool(self.approval_required),
            "completion_criteria": self.completion_criteria,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanStep":
        return cls(
            step_id=str(data.get("step_id") or ""),
            description=str(data.get("description") or ""),
            expected_outcome=str(data.get("expected_outcome") or ""),
            dependencies=[str(x) for x in (data.get("dependencies") or []) if str(x).strip()],
            required_capabilities=[str(x) for x in (data.get("required_capabilities") or []) if str(x).strip()],
            required_tools=[str(x) for x in (data.get("required_tools") or []) if str(x).strip()],
            risk_classification=str(data.get("risk_classification") or "low"),
            approval_required=bool(data.get("approval_required", False)),
            completion_criteria=str(data.get("completion_criteria") or ""),
            status=str(data.get("status") or "pending"),
        )


@dataclass
class ToolCallRecord:
    """Records a tool invocation made during workflow execution."""

    invocation_id: str = ""
    step_id: str = ""
    server_id: str = ""
    tool_name: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    status: str = "succeeded"
    duration_ms: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "step_id": self.step_id,
            "server_id": self.server_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "result": self.result,
            "error": self.error,
            "status": self.status,
            "duration_ms": int(self.duration_ms),
            "timestamp": float(self.timestamp),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolCallRecord":
        return cls(
            invocation_id=str(data.get("invocation_id") or ""),
            step_id=str(data.get("step_id") or ""),
            server_id=str(data.get("server_id") or ""),
            tool_name=str(data.get("tool_name") or ""),
            arguments=dict(data.get("arguments") or {}) if isinstance(data.get("arguments"), dict) else {},
            result=data.get("result") if isinstance(data.get("result"), dict) else ({"value": data.get("result")} if data.get("result") is not None else None),
            error=str(data["error"]) if data.get("error") is not None else None,
            status=str(data.get("status") or "succeeded"),
            duration_ms=int(data.get("duration_ms") or 0),
            timestamp=float(data.get("timestamp") or time.time()),
        )


@dataclass
class EvaluationResult:
    """Structured evaluation of a step/action outcome."""

    step_id: str
    outcome: str
    summary: str
    evidence: List[str] = field(default_factory=list)
    next_action: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "outcome": self.outcome,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "next_action": self.next_action,
            "timestamp": float(self.timestamp),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvaluationResult":
        return cls(
            step_id=str(data.get("step_id") or ""),
            outcome=str(data.get("outcome") or EvaluationOutcome.FAILED.value),
            summary=str(data.get("summary") or ""),
            evidence=[str(x) for x in (data.get("evidence") or []) if str(x)],
            next_action=str(data.get("next_action") or ""),
            timestamp=float(data.get("timestamp") or time.time()),
        )


@dataclass
class RecoveryRecord:
    """Records a bounded recovery/replanning attempt."""

    step_id: str
    error: str
    classification: str
    action: str
    timestamp: float = field(default_factory=time.time)
    recovered: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "error": self.error,
            "classification": self.classification,
            "action": self.action,
            "timestamp": float(self.timestamp),
            "recovered": bool(self.recovered),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RecoveryRecord":
        return cls(
            step_id=str(data.get("step_id") or ""),
            error=str(data.get("error") or ""),
            classification=str(data.get("classification") or ""),
            action=str(data.get("action") or ""),
            timestamp=float(data.get("timestamp") or time.time()),
            recovered=bool(data.get("recovered", False)),
        )


@dataclass
class ApprovalRequest:
    """Records a pending approval request for a tool action."""

    request_id: str
    server_id: str
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    requested_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | approved | denied
    expires_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "server_id": self.server_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "fingerprint": self.fingerprint,
            "requested_at": self.requested_at,
            "status": self.status,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApprovalRequest":
        return cls(
            request_id=str(data.get("request_id") or ""),
            server_id=str(data.get("server_id") or ""),
            tool_name=str(data.get("tool_name") or ""),
            arguments=dict(data.get("arguments") or {}) if isinstance(data.get("arguments"), dict) else {},
            fingerprint=str(data.get("fingerprint") or ""),
            requested_at=float(data.get("requested_at") or time.time()),
            status=str(data.get("status") or "pending"),
            expires_at=data.get("expires_at"),
        )


# ---------------------------------------------------------------------------
# Workflow state
# ---------------------------------------------------------------------------

@dataclass
class WorkflowState:
    """Typed state for the LangGraph task execution workflow."""

    # Identity
    task_id: str
    user_input: str
    python_runtime_pid: Optional[int] = None
    conversation_id: Optional[str] = None
    workspace: str = ""
    runtime_context: Dict[str, Any] = field(default_factory=dict)

    # State
    state: TaskState = TaskState.QUEUED
    execution_status: str = ExecutionStatus.RUNNING.value
    current_step: str = "initialization"
    objective: str = ""

    # Execution tracking
    submitted_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    graph_steps: int = 0
    wall_clock_ms: int = 0

    # Planning / execution
    plan: List[PlanStep] = field(default_factory=list)
    pending_steps: List[str] = field(default_factory=list)
    completed_steps: List[str] = field(default_factory=list)
    failed_steps: List[str] = field(default_factory=list)
    current_step_id: Optional[str] = None
    current_step_index: int = -1

    # Tool and approval tracking
    selected_tool: Optional[Dict[str, Any]] = None
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    tool_outputs: List[Dict[str, Any]] = field(default_factory=list)
    approval_requests: List[ApprovalRequest] = field(default_factory=list)
    granted_approvals: List[str] = field(default_factory=list)

    # Evaluation/recovery
    evaluation_results: List[EvaluationResult] = field(default_factory=list)
    retry_count: int = 0
    max_retries: int = DEFAULT_RETRY_LIMIT
    recovery_history: List[RecoveryRecord] = field(default_factory=list)
    plan_revisions: int = 0
    model_calls: int = 0

    # Result
    result: Optional[Dict[str, Any]] = None
    result_summary: Optional[str] = None
    failure_reason: Optional[str] = None
    final_result: Optional[Dict[str, Any]] = None
    cancelled: bool = False

    # Checkpoint / resumption
    transitions: List[Transition] = field(default_factory=list)
    checkpoint_version: int = 0
    resumable: bool = True

    # Context for LangGraph
    persistent_state: Dict[str, Any] = field(default_factory=dict)
    generation: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user_input": self.user_input,
            "objective": self.objective or self.user_input,
            "conversation_id": self.conversation_id,
            "workspace": self.workspace,
            "runtime_context": self.runtime_context,
            "python_runtime_pid": self.python_runtime_pid,
            "state": self.state.value if isinstance(self.state, TaskState) else str(self.state),
            "execution_status": self.execution_status,
            "current_step": self.current_step,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "graph_steps": self.graph_steps,
            "wall_clock_ms": self.wall_clock_ms,
            "plan": [s.to_dict() for s in self.plan],
            "pending_steps": list(self.pending_steps),
            "completed_steps": list(self.completed_steps),
            "failed_steps": list(self.failed_steps),
            "current_step_id": self.current_step_id,
            "current_step_index": self.current_step_index,
            "selected_tool": self.selected_tool,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "tool_outputs": list(self.tool_outputs),
            "approval_requests": [ar.to_dict() for ar in self.approval_requests],
            "granted_approvals": list(self.granted_approvals),
            "evaluation_results": [e.to_dict() for e in self.evaluation_results],
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "recovery_history": [r.to_dict() for r in self.recovery_history],
            "plan_revisions": self.plan_revisions,
            "model_calls": self.model_calls,
            "result": self.result,
            "result_summary": self.result_summary,
            "failure_reason": self.failure_reason,
            "final_result": self.final_result,
            "cancelled": self.cancelled,
            "transitions": [
                {
                    "from_node": t.from_node,
                    "to_node": t.to_node,
                    "timestamp": t.timestamp,
                    "reason": t.reason,
                }
                for t in self.transitions
            ],
            "checkpoint_version": self.checkpoint_version,
            "resumable": self.resumable,
            "persistent_state": {k: v for k, v in self.persistent_state.items() if not k.startswith("_")},
            "generation": dict(self.generation) if isinstance(self.generation, dict) else {},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowState":
        raw_state = data.get("state", "queued")
        try:
            state = TaskState(raw_state)
        except ValueError:
            state = TaskState.QUEUED

        transitions = []
        for t in data.get("transitions", []):
            if isinstance(t, dict):
                transitions.append(Transition(
                    from_node=str(t.get("from_node", "")),
                    to_node=str(t.get("to_node", "")),
                    timestamp=float(t.get("timestamp", time.time())),
                    reason=str(t.get("reason", "")),
                ))

        plan = [PlanStep.from_dict(s) for s in (data.get("plan") or []) if isinstance(s, dict)]
        tool_calls = [ToolCallRecord.from_dict(tc) for tc in (data.get("tool_calls") or []) if isinstance(tc, dict)]
        approval_requests = [ApprovalRequest.from_dict(ar) for ar in (data.get("approval_requests") or []) if isinstance(ar, dict)]
        evaluation_results = [EvaluationResult.from_dict(e) for e in (data.get("evaluation_results") or []) if isinstance(e, dict)]
        recovery_history = [RecoveryRecord.from_dict(r) for r in (data.get("recovery_history") or []) if isinstance(r, dict)]
        tool_outputs = [x for x in (data.get("tool_outputs") or []) if isinstance(x, dict)]

        objective = str(data.get("objective") or data.get("user_input") or "")
        pending_steps = [str(x) for x in (data.get("pending_steps") or []) if str(x)]
        completed_steps = [str(x) for x in (data.get("completed_steps") or []) if str(x)]
        failed_steps = [str(x) for x in (data.get("failed_steps") or []) if str(x)]
        if plan and not (pending_steps or completed_steps or failed_steps):
            for step in plan:
                if step.status == "completed":
                    completed_steps.append(step.step_id)
                elif step.status in {"failed", "skipped"}:
                    failed_steps.append(step.step_id)
                else:
                    pending_steps.append(step.step_id)

        return cls(
            task_id=str(data.get("task_id", "")),
            user_input=str(data.get("user_input", "")),
            objective=objective,
            conversation_id=data.get("conversation_id"),
            workspace=str(data.get("workspace") or ""),
            runtime_context=dict(data.get("runtime_context") or {}) if isinstance(data.get("runtime_context"), dict) else {},
            python_runtime_pid=data.get("python_runtime_pid"),
            state=state,
            execution_status=str(data.get("execution_status") or ExecutionStatus.RUNNING.value),
            current_step=str(data.get("current_step", "initialization")),
            submitted_at=float(data.get("submitted_at", time.time())),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            graph_steps=int(data.get("graph_steps", 0)),
            wall_clock_ms=int(data.get("wall_clock_ms", 0)),
            plan=plan,
            pending_steps=pending_steps,
            completed_steps=completed_steps,
            failed_steps=failed_steps,
            current_step_id=data.get("current_step_id"),
            current_step_index=int(data.get("current_step_index", -1)),
            selected_tool=data.get("selected_tool"),
            tool_calls=tool_calls,
            tool_outputs=tool_outputs,
            approval_requests=approval_requests,
            granted_approvals=list(data.get("granted_approvals", [])),
            evaluation_results=evaluation_results,
            retry_count=int(data.get("retry_count", 0)),
            max_retries=int(data.get("max_retries", DEFAULT_RETRY_LIMIT)),
            recovery_history=recovery_history,
            plan_revisions=int(data.get("plan_revisions", 0)),
            model_calls=int(data.get("model_calls", 0)),
            result=data.get("result"),
            result_summary=data.get("result_summary"),
            failure_reason=data.get("failure_reason"),
            final_result=data.get("final_result") if isinstance(data.get("final_result"), dict) else None,
            cancelled=bool(data.get("cancelled", False)),
            transitions=transitions,
            checkpoint_version=int(data.get("checkpoint_version", 0)),
            resumable=bool(data.get("resumable", True)),
            persistent_state=dict(data.get("persistent_state", {})),
            generation=dict(data.get("generation") or {}) if isinstance(data.get("generation"), dict) else {},
        )


def state_to_checkpoint(state: WorkflowState) -> Dict[str, Any]:
    return state.to_dict()


def state_from_checkpoint(data: Dict[str, Any]) -> WorkflowState:
    return WorkflowState.from_dict(data)
