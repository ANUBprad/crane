"""Bounded LangGraph workflow execution for CRANE tasks.

The graph is the real execution engine. It emits versioned checkpoint and event
messages to the Go supervisor through stdout; diagnostics remain on stderr.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from langgraph.graph import END, START, StateGraph

from crane.python_runtime import protocol
from crane.python_runtime.provider import (
    ProviderError,
    invoke as invoke_provider,
    prepare_provider,
    provider_details,
    warm_provider,
)
from crane.python_runtime.workflow_state import (
    MAX_GRAPH_STEPS,
    MAX_WALL_CLOCK_MS,
    TaskState,
    WorkflowState,
    state_from_checkpoint,
    state_to_checkpoint,
)


class WorkflowCancelled(RuntimeError):
    """Raised when a cancellation request is observed at a graph boundary."""


logger = logging.getLogger("crane.workflow")

_CANCELLATIONS: Dict[str, threading.Event] = {}
_CANCELLATIONS_LOCK = threading.Lock()


def _register_cancellation(task_id: str, event: threading.Event) -> None:
    with _CANCELLATIONS_LOCK:
        _CANCELLATIONS[task_id] = event


def _unregister_cancellation(task_id: str) -> None:
    with _CANCELLATIONS_LOCK:
        _CANCELLATIONS.pop(task_id, None)


def request_cancellation(task_id: str) -> bool:
    with _CANCELLATIONS_LOCK:
        event = _CANCELLATIONS.get(task_id)
    if event is None:
        return False
    event.set()
    return True


def _check_cancelled(task_id: str) -> None:
    with _CANCELLATIONS_LOCK:
        event = _CANCELLATIONS.get(task_id)
    if event is not None and event.is_set():
        raise WorkflowCancelled("task cancelled by supervisor")


def _as_state(raw: Any) -> WorkflowState:
    if isinstance(raw, WorkflowState):
        return raw
    return state_from_checkpoint(raw)


def _value(raw: Any, key: str, default: Any = None) -> Any:
    if isinstance(raw, dict):
        return raw.get(key, default)
    return getattr(raw, key, default)


def _event(task_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    protocol.write_stdout(protocol.task_event(task_id, event_type, payload))


def _persist_task_state(state: WorkflowState) -> None:
    checkpoint = state_to_checkpoint(state)
    protocol.write_stdout({
        "type": protocol.TYPE_TASK_STATE,
        "task_id": state.task_id,
        "state": state.state.value if isinstance(state.state, TaskState) else str(state.state),
        "current_step": state.current_step,
        "graph_steps": state.graph_steps,
        "wall_clock_ms": state.wall_clock_ms,
        "result_summary": state.result_summary,
        "failure_reason": state.failure_reason,
        "cancelled": state.cancelled,
        "checkpoint": checkpoint,
    })
    _event(state.task_id, "checkpoint", {
        "step": state.current_step,
        "graph_steps": state.graph_steps,
        "state": state.state.value if isinstance(state.state, TaskState) else str(state.state),
    })


def _elapsed_ms(state: WorkflowState) -> int:
    return max(0, int((time.time() - state.submitted_at) * 1000))


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


async def node_normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    _check_cancelled(state.task_id)
    state.state = TaskState.STARTING
    state.current_step = "normalizing"
    state.graph_steps += 1
    state.wall_clock_ms = _elapsed_ms(state)
    state.result = None
    state.result_summary = None
    state.failure_reason = None
    state.cancelled = False
    state.state = TaskState.RUNNING
    state.started_at = state.started_at or time.time()
    details = provider_details()
    state.persistent_state["execution"] = details
    _persist_task_state(state)
    _event(state.task_id, "workflow_started", {"provider": details["name"]})
    return state_to_checkpoint(state)


async def node_plan(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    _check_cancelled(state.task_id)
    state.graph_steps += 1
    state.current_step = "planning"
    state.wall_clock_ms = _elapsed_ms(state)
    if state.graph_steps > MAX_GRAPH_STEPS:
        state.state = TaskState.FAILED
        state.failure_reason = f"Exceeded maximum graph steps ({MAX_GRAPH_STEPS})"
        state.current_step = "failed_max_steps"
        state.completed_at = time.time()
    elif state.wall_clock_ms > MAX_WALL_CLOCK_MS:
        state.state = TaskState.FAILED
        state.failure_reason = f"Exceeded maximum wall-clock time ({MAX_WALL_CLOCK_MS}ms)"
        state.current_step = "failed_wall_clock"
        state.completed_at = time.time()
    _persist_task_state(state)
    return state_to_checkpoint(state)


async def node_execute(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    _check_cancelled(state.task_id)
    state.graph_steps += 1
    state.current_step = "executing"
    state.wall_clock_ms = _elapsed_ms(state)
    try:
        prepare_provider()
        warm_provider()
        output = await asyncio.to_thread(invoke_provider, state.user_input)
        state.result = output
        state.result_summary = f"Completed with {output.get('execution_mode', 'configured')} execution"
        state.state = TaskState.SUCCEEDED
        state.current_step = "succeeded"
        _event(state.task_id, "provider_result", {
            "execution_mode": output.get("execution_mode"),
            "provider": output.get("provider"),
        })
    except ProviderError as exc:
        state.state = TaskState.FAILED
        state.failure_reason = exc.message
        state.current_step = "provider_failed"
        state.result = {"code": exc.code, "message": exc.message}
        state.result_summary = "Configured provider could not complete the task"
        _event(state.task_id, "provider_failed", {"code": exc.code})
    except WorkflowCancelled:
        raise
    except Exception:  # noqa: BLE001 - never leak provider internals across the process boundary
        state.state = TaskState.FAILED
        state.failure_reason = "Workflow execution failed unexpectedly"
        state.current_step = "failed_unexpected"
        state.result_summary = "Workflow execution failed"
    state.wall_clock_ms = _elapsed_ms(state)
    state.completed_at = time.time()
    _persist_task_state(state)
    return state_to_checkpoint(state)


async def node_complete(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    state.state = TaskState.SUCCEEDED
    state.current_step = "completed"
    state.completed_at = state.completed_at or time.time()
    state.wall_clock_ms = _elapsed_ms(state)
    _persist_task_state(state)
    _event(state.task_id, "completed", {"result_summary": state.result_summary})
    return state_to_checkpoint(state)


async def node_fail(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    state.state = TaskState.FAILED
    state.current_step = "failed"
    state.completed_at = state.completed_at or time.time()
    state.wall_clock_ms = _elapsed_ms(state)
    state.result_summary = state.result_summary or "Task failed"
    _persist_task_state(state)
    _event(state.task_id, "failed", {"failure_reason": state.failure_reason})
    return state_to_checkpoint(state)


async def node_cancelled(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    state.state = TaskState.CANCELLED
    state.cancelled = True
    state.current_step = "cancelled"
    state.completed_at = time.time()
    state.wall_clock_ms = _elapsed_ms(state)
    state.result_summary = "Task cancelled before completion"
    _persist_task_state(state)
    _event(state.task_id, "cancelled", {"reason": "supervisor_request"})
    return state_to_checkpoint(state)


def compile_workflow() -> Any:
    graph = StateGraph(WorkflowState)
    graph.add_node("normalize", node_normalize)
    graph.add_node("plan", node_plan)
    graph.add_node("execute", node_execute)
    graph.add_node("complete", node_complete)
    graph.add_node("fail", node_fail)
    graph.add_node("cancelled", node_cancelled)
    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "plan")
    def plan_route(raw: Any) -> str:
        state_value = _value(raw, "state")
        if _value(raw, "cancelled"):
            return "cancelled"
        if state_value in (TaskState.FAILED, TaskState.FAILED.value):
            return "fail"
        return "execute"

    def execute_route(raw: Any) -> str:
        if _value(raw, "cancelled"):
            return "cancelled"
        state_value = _value(raw, "state")
        if state_value in (TaskState.SUCCEEDED, TaskState.SUCCEEDED.value):
            return "complete"
        return "fail"

    graph.add_conditional_edges("plan", plan_route)
    graph.add_conditional_edges("execute", execute_route)
    for terminal in ("complete", "fail", "cancelled"):
        graph.add_edge(terminal, END)
    return graph.compile()


async def execute_task(
    user_input: str,
    python_runtime_pid: Optional[int] = None,
    task_id: Optional[str] = None,
    resume_checkpoint: Optional[Dict[str, Any]] = None,
    cancellation: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Execute one bounded task through LangGraph and return its final state."""
    resolved_id = task_id or str(uuid.uuid4())
    event = cancellation or threading.Event()
    _register_cancellation(resolved_id, event)
    try:
        if resume_checkpoint:
            state = state_from_checkpoint(resume_checkpoint)
            state.task_id = resolved_id
            state.user_input = user_input
            state.python_runtime_pid = python_runtime_pid
            if state.state in (TaskState.QUEUED, TaskState.STARTING, TaskState.RUNNING):
                state.state = TaskState.RUNNING
        else:
            state = WorkflowState(
                task_id=resolved_id,
                user_input=user_input,
                python_runtime_pid=python_runtime_pid,
                state=TaskState.QUEUED,
            )
        graph = compile_workflow()
        raw_result = await graph.ainvoke(
            state_to_checkpoint(state),
            config={"configurable": {"thread_id": resolved_id}},
        )
        final_state = _as_state(raw_result)
        return _result_payload(final_state)
    except WorkflowCancelled:
        cancelled = WorkflowState(
            task_id=resolved_id,
            user_input=user_input,
            state=TaskState.CANCELLED,
            current_step="cancelled",
            cancelled=True,
            result_summary="Task cancelled before completion",
        )
        _persist_task_state(cancelled)
        return _result_payload(cancelled)
    except Exception:  # noqa: BLE001 - Go receives a safe, explicit failure
        logger.exception("workflow execution error for task %s", resolved_id)
        failed = WorkflowState(
            task_id=resolved_id,
            user_input=user_input,
            state=TaskState.FAILED,
            current_step="failed_unexpected",
            failure_reason="Workflow execution failed unexpectedly",
            result_summary="Workflow execution failed",
            completed_at=time.time(),
        )
        _persist_task_state(failed)
        return _result_payload(failed)
    finally:
        _unregister_cancellation(resolved_id)


def _result_payload(state: WorkflowState) -> Dict[str, Any]:
    return {
        "task_id": state.task_id,
        "status": state.state.value if isinstance(state.state, TaskState) else str(state.state),
        "result": state.result,
        "result_summary": state.result_summary,
        "failure_reason": state.failure_reason,
        "graph_steps": state.graph_steps,
        "wall_clock_ms": state.wall_clock_ms,
        "current_step": state.current_step,
        "checkpoint": state_to_checkpoint(state),
    }


__all__ = ["TaskState", "WorkflowState", "compile_workflow", "execute_task", "request_cancellation", "WorkflowCancelled"]
