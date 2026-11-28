"""Bounded LangGraph workflow execution for CRANE tasks.

The graph is the real execution engine. It emits versioned checkpoint and event
messages to the Go supervisor through stdout; diagnostics remain on stderr.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from langgraph.graph import END, START, StateGraph

from crane.mcp_runtime import (
    MCPManager,
    canonical_json,
    classify_tool,
    configure_manager,
    connect_all,
    redact,
    shutdown_all,
)
from crane.python_runtime import protocol
from crane.python_runtime.context import build_runtime_context
from crane.python_runtime import generation as gen
from crane.python_runtime.provider import (
    ProviderError,
    invoke as invoke_provider,
    invoke_with_tools,
    prepare_provider,
    provider_details,
    selected_provider,
    warm_provider,
)
from crane.python_runtime.provider import MAX_TOOL_ROUNDS
from crane.python_runtime.workflow_state import (
    DEFAULT_TOOL_TIMEOUT_MS,
    EvaluationOutcome,
    EvaluationResult,
    ExecutionStatus,
    MAX_CONTEXT_MESSAGES,
    MAX_EXECUTION_STEPS,
    MAX_GRAPH_STEPS,
    MAX_MODEL_CALLS_PER_TASK,
    MAX_MODEL_CONTEXT_CHARS,
    MAX_PLAN_REVISIONS,
    MAX_RETRIES_PER_STEP,
    MAX_TOOL_CALLS_PER_TASK,
    MAX_TOOL_ERROR_CHARS,
    MAX_TOOL_OUTPUT_CHARS,
    MAX_WALL_CLOCK_MS,
    PlanStep,
    RecoveryRecord,
    TaskState,
    ToolCallRecord,
    Transition,
    WorkflowState,
    state_from_checkpoint,
    state_to_checkpoint,
)


class WorkflowCancelled(RuntimeError):
    """Raised when a cancellation request is observed at a graph boundary."""


logger = logging.getLogger("crane.workflow")

_CANCELLATIONS: Dict[str, threading.Event] = {}
_CANCELLATIONS_LOCK = threading.Lock()
_MANAGERS: Dict[str, MCPManager] = {}
_MANAGERS_LOCK = threading.Lock()


def _set_manager(task_id: str, manager: Optional[MCPManager]) -> None:
    with _MANAGERS_LOCK:
        if manager is None:
            _MANAGERS.pop(task_id, None)
        else:
            _MANAGERS[task_id] = manager


def _get_manager(task_id: str) -> Optional[MCPManager]:
    with _MANAGERS_LOCK:
        return _MANAGERS.get(task_id)


def _pop_manager(task_id: str) -> Optional[MCPManager]:
    with _MANAGERS_LOCK:
        return _MANAGERS.pop(task_id, None)


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


def _cancellation_event(task_id: str) -> Optional[threading.Event]:
    with _CANCELLATIONS_LOCK:
        return _CANCELLATIONS.get(task_id)


async def _await_with_cancellation(awaitable: Any, task_id: str) -> Any:
    """Await model work while reacting to cancellation between blocking calls.

    Model clients invoke synchronous HTTP APIs in worker threads; cancelling the
    asyncio task cannot stop that thread, but it lets the workflow return a real
    cancelled terminal result immediately. The supervisor then stops the child
    process, which owns the worker thread.
    """
    event = _cancellation_event(task_id)
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            if event is not None and event.is_set():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise WorkflowCancelled("task cancelled by supervisor")
            await asyncio.sleep(0.05)
        return task.result()
    finally:
        if not task.done():
            task.cancel()


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
    protocol.write_stdout(protocol.task_event(task_id, event_type, redact(payload or {})))


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
        "execution_status": state.execution_status,
    })


def _elapsed_ms(state: WorkflowState) -> int:
    return max(0, int((time.time() - state.submitted_at) * 1000))


def _text(value: Any, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = str(value)
    text = text.strip()
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


def _tool_output_text(output: Any) -> str:
    if isinstance(output, tuple):
        output = output[0] if output else ""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        if isinstance(output.get("text"), str):
            return output["text"]
        return _text(output)
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(_text(item, 2000))
        return "\n".join(p for p in parts if p)
    return _text(output)


def _normalize_objective(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "")).strip()
    return normalized[:4000]


def _available_tools(manager: Optional[MCPManager]) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = []
    if manager is None:
        return tools
    for record in manager.tools():
        try:
            tools.append(record.to_dict())
        except Exception:
            tools.append({"name": getattr(record, "name", "unknown"), "server_id": getattr(record, "server_id", "")})
    return tools


def _tool_index(manager: Optional[MCPManager]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for tool in _available_tools(manager):
        name = str(tool.get("name") or "")
        if name:
            index[name] = tool
    return index


def _find_tool(manager: Optional[MCPManager], name: str) -> Optional[Dict[str, Any]]:
    if not name:
        return None
    return _tool_index(manager).get(name)


def _record_result_text(value: Any) -> str:
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return _text(text, MAX_TOOL_OUTPUT_CHARS).strip()
        structured = value.get("structured")
        if structured is not None:
            return _text(structured, MAX_TOOL_OUTPUT_CHARS).strip()
    return _text(value, MAX_TOOL_OUTPUT_CHARS).strip()


def _tool_call_matches_invocation(call: Dict[str, Any], invocation: Dict[str, Any]) -> bool:
    call_name = str(call.get("name") or call.get("tool_name") or "")
    invocation_name = str(invocation.get("tool_name") or "")
    if not call_name or not invocation_name:
        return False
    if call_name != invocation_name and not call_name.endswith(f"_{invocation_name}"):
        return False
    call_args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    invocation_args = invocation.get("arguments") if isinstance(invocation.get("arguments"), dict) else {}
    try:
        if canonical_json(redact(call_args)) != canonical_json(redact(invocation_args)):
            return False
    except (TypeError, ValueError):
        return False
    call_result = _record_result_text(call.get("result"))
    invocation_result = _record_result_text(invocation.get("result"))
    return not call_result or not invocation_result or call_result == invocation_result


def _normalize_tool_status(value: Any, result_text: str = "", error: Any = None) -> Tuple[str, Optional[str]]:
    status = str(value or "succeeded").lower()
    if status == "error":
        status = "failed"
    if status in {"failed", "blocked"} and not error:
        error = result_text[:MAX_TOOL_ERROR_CHARS] or "MCP tool call did not complete"
    return status, str(error) if error else None


def _merge_tool_call_records(
    state: WorkflowState,
    step_id: str,
    output: Any,
    manager: Optional[MCPManager],
) -> None:
    """Merge authoritative MCP records with provider observations without duplicates."""
    invocations = manager.invocations() if manager is not None else []
    provider_calls = output.get("tool_calls") if isinstance(output, dict) else None
    if not isinstance(provider_calls, list):
        provider_calls = []
    seen = {tc.invocation_id for tc in state.tool_calls if tc.invocation_id}

    for invocation in invocations:
        invocation_id = str(invocation.get("invocation_id") or "")
        if not invocation_id or invocation_id in seen:
            continue
        result_text = _record_result_text(invocation.get("result"))
        status, error = _normalize_tool_status(invocation.get("status"), result_text, invocation.get("error"))
        arguments = invocation.get("arguments") if isinstance(invocation.get("arguments"), dict) else {}
        raw_result = invocation.get("result")
        record = ToolCallRecord(
            invocation_id=invocation_id,
            step_id=step_id,
            server_id=str(invocation.get("server_id") or ""),
            tool_name=str(invocation.get("tool_name") or ""),
            arguments=arguments,
            result=raw_result if isinstance(raw_result, dict) else ({"value": raw_result} if raw_result is not None else None),
            error=error,
            status=status,
            duration_ms=int(invocation.get("duration_ms") or 0),
        )
        state.tool_calls.append(record)
        state.tool_outputs.append({
            "step_id": step_id,
            "server_id": record.server_id,
            "tool_name": record.tool_name,
            "arguments": record.arguments,
            "status": record.status,
            "result": _text(record.result, MAX_TOOL_OUTPUT_CHARS),
            "error": record.error,
            "duration_ms": record.duration_ms,
        })
        seen.add(invocation_id)

    for call in provider_calls:
        if not isinstance(call, dict):
            continue
        if any(_tool_call_matches_invocation(call, invocation) for invocation in invocations):
            continue
        invocation_id = str(call.get("invocation_id") or "")
        if not invocation_id:
            invocation_id = f"{step_id}-observed-{len(state.tool_calls) + 1}"
        if invocation_id in seen:
            continue
        result_text = _record_result_text(call.get("result"))
        status, error = _normalize_tool_status(call.get("status"), result_text, call.get("error"))
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        raw_result = call.get("result")
        record = ToolCallRecord(
            invocation_id=invocation_id,
            step_id=step_id,
            server_id=str(call.get("server_id") or ""),
            tool_name=str(call.get("name") or call.get("tool_name") or ""),
            arguments=arguments,
            result=raw_result if isinstance(raw_result, dict) else ({"value": raw_result} if raw_result is not None else None),
            error=error,
            status=status,
            duration_ms=int(call.get("duration_ms") or 0),
        )
        state.tool_calls.append(record)
        state.tool_outputs.append({
            "step_id": step_id,
            "server_id": record.server_id,
            "tool_name": record.tool_name,
            "arguments": record.arguments,
            "status": record.status,
            "result": _text(record.result, MAX_TOOL_OUTPUT_CHARS),
            "error": record.error,
            "duration_ms": record.duration_ms,
        })
        seen.add(invocation_id)


def _risk_for_tools(tool_names: List[str], tools_by_name: Dict[str, Dict[str, Any]]) -> Tuple[str, bool]:
    risk = "low"
    approval = False
    for name in tool_names:
        tool = tools_by_name.get(name) or {}
        policy = str(tool.get("policy_class") or "approval_required")
        lowered = name.lower()
        if policy != "allowed" or any(w in lowered for w in ["write", "delete", "exec", "run", "install", "send", "post", "publish"]):
            approval = approval or policy != "allowed"
            risk = "high" if approval else risk
        if any(w in lowered for w in ["fetch", "search", "web", "http"]):
            risk = "medium" if risk == "low" else risk
    return risk, approval


def _validate_plan(state: WorkflowState, plan: List[PlanStep], tools_by_name: Dict[str, Dict[str, Any]]) -> Tuple[bool, str]:
    if not plan:
        return False, "Plan must contain at least one step"
    if len(plan) > MAX_EXECUTION_STEPS:
        return False, f"Plan exceeds maximum execution steps ({MAX_EXECUTION_STEPS})"
    seen: set[str] = set()
    for step in plan:
        if not step.step_id or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", step.step_id):
            return False, f"Invalid step ID: {step.step_id!r}"
        if step.step_id in seen:
            return False, f"Duplicate step ID: {step.step_id}"
        seen.add(step.step_id)
        if not step.description.strip() or not step.expected_outcome.strip() or not step.completion_criteria.strip():
            return False, f"Step {step.step_id} is missing required fields"
        if step.risk_classification not in {"low", "medium", "high"}:
            return False, f"Step {step.step_id} has invalid risk classification"
        for dep in step.dependencies:
            if dep not in {s.step_id for s in plan}:
                return False, f"Step {step.step_id} has unknown dependency {dep}"
        for tool in step.required_tools:
            if tool not in tools_by_name:
                return False, f"Step {step.step_id} references unknown tool {tool}"
    # cycle detection
    deps = {s.step_id: list(s.dependencies) for s in plan}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visited:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for dep in deps.get(node, []):
            if visit(dep):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    for s in plan:
        if visit(s.step_id):
            return False, "Plan contains cyclic dependencies"
    # dependency ordering sanity: no forward-only impossible order beyond cycle; DFS is enough
    return True, ""


GENERATION_INTENT_RE = re.compile(
    r"\b(create|build|generate|scaffold|bootstrap|implement|make|develop|write)\b.{0,80}\b(app|application|project|service|api|cli|website|web app|backend|frontend|full[- ]?stack|script|tool|library|repo|repository|codebase)\b",
    re.IGNORECASE | re.DOTALL,
)


def _has_generation_intent(state: WorkflowState) -> bool:
    obj = state.objective or state.user_input or ""
    return bool(GENERATION_INTENT_RE.search(obj)) or state.runtime_context.get("task_kind") == "create"


def _is_generation_task(state: WorkflowState) -> bool:
    return bool(state.generation.get("enabled") or _has_generation_intent(state))


def _require_generation_workspace(state: WorkflowState) -> bool:
    if not _is_generation_task(state) or state.workspace:
        return True
    failure = (
        "This objective requires file creation or repair, but no authorized workspace was supplied. "
        "Re-run with `agent run --workspace <dir>` or `agent create --workspace <dir>`. No files were created."
    )
    _mark_terminal(state, ExecutionStatus.FAILED, "Authorized workspace required", failure)
    state.current_step = "workspace_required"
    _event(state.task_id, "task_failed", {"error": failure})
    _persist_task_state(state)
    return False


def _generation_evidence_error(state: WorkflowState) -> Optional[str]:
    """Return why a generation task cannot be reported as fully completed."""
    if state.generation.get("plan_only"):
        return None

    applied = state.generation.get("applied")
    if not isinstance(applied, list) or not applied:
        return "No generated files were applied to the authorized workspace."

    try:
        workspace = gen.canonicalize_workspace(state.workspace)
    except Exception as exc:  # noqa: BLE001
        return f"The authorized workspace is no longer valid: {exc}"

    saw_applied_file = False
    for record in applied:
        if not isinstance(record, dict):
            continue
        path = record.get("path")
        operation = record.get("operation")
        status = record.get("status")
        if not isinstance(path, str):
            continue
        try:
            target = gen.validate_relative_path(workspace, path)
        except Exception:  # noqa: BLE001
            continue
        if operation == "write" and status == "written" and target.is_file():
            saw_applied_file = True
        elif operation == "delete" and status in {"deleted", "missing"} and not target.exists():
            saw_applied_file = True
    if not saw_applied_file:
        return "No applied file could be verified in the authorized workspace."

    validations = state.generation.get("validation_results")
    if not isinstance(validations, list):
        return "No validation command output was recorded."
    for record in validations:
        if isinstance(record, dict) and record.get("status") == "succeeded" and "exit_code" in record:
            return None
    return "No successful allowlisted validation command output was recorded."


def _default_generation_plan(state: WorkflowState) -> List[PlanStep]:
    return [
        PlanStep(
            step_id="analyze_requirements",
            description="Analyze the software requirement and produce a structured project specification.",
            expected_outcome="A validated project specification with objective, stack, structure, and validation requirements.",
            dependencies=[],
            required_capabilities=["reasoning"],
            required_tools=[],
            risk_classification="low",
            approval_required=False,
            completion_criteria="Project specification is complete enough to plan file generation.",
        ),
        PlanStep(
            step_id="plan_project",
            description="Create a validated project plan and file manifest for the authorized workspace.",
            expected_outcome="A concrete file manifest and validation plan with no unsafe paths.",
            dependencies=["analyze_requirements"],
            required_capabilities=["planning"],
            required_tools=[],
            risk_classification="low",
            approval_required=False,
            completion_criteria="Plan includes file manifest, validations, and risk classification.",
        ),
        PlanStep(
            step_id="generate_files",
            description="Generate project source files as structured file operations and stage them in the authorized workspace.",
            expected_outcome="All planned files are generated, validated, and staged for review/apply.",
            dependencies=["plan_project"],
            required_capabilities=["filesystem_write"],
            required_tools=[],
            risk_classification="high",
            approval_required=True,
            completion_criteria="Staged files exist and pass manifest/path validation.",
        ),
        PlanStep(
            step_id="validate_project",
            description="Run applicable validation checks against the generated project in the authorized workspace.",
            expected_outcome="Validation commands execute and produce captured results.",
            dependencies=["generate_files"],
            required_capabilities=["controlled_execution"],
            required_tools=[],
            risk_classification="medium",
            approval_required=True,
            completion_criteria="Validation results are captured with actual command outputs.",
        ),
        PlanStep(
            step_id="repair_project",
            description="Diagnose validation failures and apply bounded focused repairs if needed.",
            expected_outcome="Failures are repaired within limits or reported truthfully.",
            dependencies=["validate_project"],
            required_capabilities=["filesystem_write", "controlled_execution"],
            required_tools=[],
            risk_classification="high",
            approval_required=True,
            completion_criteria="Project is revalidated or repair limit/failure is recorded.",
        ),
        PlanStep(
            step_id="final_report",
            description="Produce a verified final project report based on actual files and validation results.",
            expected_outcome="Final report includes location, files, checks, and limitations.",
            dependencies=["repair_project"],
            required_capabilities=["verification"],
            required_tools=[],
            risk_classification="low",
            approval_required=False,
            completion_criteria="Report reflects actual task state and verification outcomes.",
        ),
    ]


def _default_plan(objective: str, tools_by_name: Dict[str, Dict[str, Any]]) -> List[PlanStep]:
    text = objective.lower()
    steps: List[PlanStep] = []
    has_web_search = "web_search" in tools_by_name
    has_fetch = "fetch_url" in tools_by_name
    has_python = "run_python" in tools_by_name

    if has_web_search and any(k in text for k in ["search", "find", "look up", "documentation", "docs", "official"]):
        steps.append(PlanStep(
            step_id="search",
            description="Search for relevant authoritative sources for the objective.",
            expected_outcome="Search results identifying relevant official or authoritative URLs.",
            dependencies=[],
            required_capabilities=["web_search"],
            required_tools=["web_search"],
            risk_classification="medium",
            approval_required=False,
            completion_criteria="At least one relevant source URL or result is returned.",
        ))
    if has_fetch and any(k in text for k in ["fetch", "retrieve", "read", "documentation", "docs", "page", "url", "http", "extract"]):
        deps = ["search"] if any(s.step_id == "search" for s in steps) else []
        steps.append(PlanStep(
            step_id="retrieve",
            description="Retrieve the most relevant page/content needed to satisfy the objective.",
            expected_outcome="Relevant page content or extracted information is retrieved.",
            dependencies=deps,
            required_capabilities=["url_retrieval"],
            required_tools=["fetch_url"],
            risk_classification="medium",
            approval_required=False,
            completion_criteria="Retrieved content contains information relevant to the objective.",
        ))
    if has_python and any(k in text for k in ["python", "calculate", "compute", "run code", "execute", "fibonacci", "program"]):
        dep_list = [steps[-1].step_id] if steps else []
        steps.append(PlanStep(
            step_id="execute_code",
            description="Execute required Python code to produce verifiable output.",
            expected_outcome="Code executes successfully and produces the requested output.",
            dependencies=dep_list,
            required_capabilities=["controlled_code_execution"],
            required_tools=["run_python"],
            risk_classification="medium",
            approval_required=False,
            completion_criteria="Program output is captured and matches the requested computation.",
        ))

    if not steps:
        # informational/default single step
        steps.append(PlanStep(
            step_id="respond",
            description="Analyze the objective using available runtime context and produce a factual response.",
            expected_outcome="A factual response that addresses the objective or clearly states limitations.",
            dependencies=[],
            required_capabilities=["reasoning"],
            required_tools=[],
            risk_classification="low",
            approval_required=False,
            completion_criteria="Response addresses the objective without fabricating unavailable results.",
        ))
    else:
        deps = [steps[-1].step_id]
        steps.append(PlanStep(
            step_id="verify_and_summarize",
            description="Verify collected evidence and summarize what was accomplished against the objective.",
            expected_outcome="A verified final summary with evidence and any remaining gaps.",
            dependencies=deps,
            required_capabilities=["verification"],
            required_tools=[],
            risk_classification="low",
            approval_required=False,
            completion_criteria="Final response distinguishes completed, verified, failed, and remaining work.",
        ))

    # Normalize risk/approval based on tools.
    for step in steps:
        risk, approval = _risk_for_tools(step.required_tools, tools_by_name)
        step.risk_classification = risk
        if step.required_tools:
            # trust explicit policy from tool metadata
            approval = any(str((tools_by_name.get(t) or {}).get("policy_class") or "") != "allowed" for t in step.required_tools)
        step.approval_required = bool(approval)
    return steps[:MAX_EXECUTION_STEPS]


def _set_plan(state: WorkflowState, plan: List[PlanStep]) -> None:
    state.plan = plan
    state.pending_steps = [s.step_id for s in plan if s.status == "pending"]
    state.completed_steps = [s.step_id for s in plan if s.status == "completed"]
    state.failed_steps = [s.step_id for s in plan if s.status in {"failed", "skipped"}]
    state.current_step_index = -1
    state.current_step_id = None


def _plan_status_sync(state: WorkflowState) -> None:
    pending, completed, failed = [], [], []
    for s in state.plan:
        if s.status == "completed":
            completed.append(s.step_id)
        elif s.status in {"failed", "skipped"}:
            failed.append(s.step_id)
        elif s.status == "pending":
            pending.append(s.step_id)
        else:
            # running/waiting states remain pending for scheduling purposes
            pending.append(s.step_id)
    state.pending_steps = pending
    state.completed_steps = completed
    state.failed_steps = failed


def _next_runnable_step(state: WorkflowState) -> Optional[PlanStep]:
    completed = set(state.completed_steps)
    failed = set(state.failed_steps)
    for idx, step in enumerate(state.plan):
        if step.status != "pending":
            continue
        ready = True
        for dep in step.dependencies:
            if dep in completed:
                continue
            if step.step_id == "repair_project" and dep == "validate_project" and dep in failed:
                continue
            ready = False
            break
        if ready:
            state.current_step_index = idx
            state.current_step_id = step.step_id
            return step
    return None


def _record_transition(state: WorkflowState, from_node: str, to_node: str, reason: str = "") -> None:
    state.transitions.append(Transition(from_node=from_node, to_node=to_node, reason=reason))
    if len(state.transitions) > 200:
        state.transitions = state.transitions[-200:]


def _mark_terminal(state: WorkflowState, execution_status: ExecutionStatus, summary: str, failure_reason: Optional[str] = None) -> None:
    state.execution_status = execution_status.value
    state.result_summary = summary
    state.failure_reason = failure_reason
    state.completed_at = state.completed_at or time.time()
    state.wall_clock_ms = _elapsed_ms(state)
    if execution_status != ExecutionStatus.WAITING_FOR_APPROVAL:
        # A terminal state must not retain an approval checkpoint that would
        # make a failed or completed task look resumable through a stale scope.
        state.generation.pop("pending_approval", None)
    if execution_status == ExecutionStatus.FULLY_COMPLETED:
        state.state = TaskState.SUCCEEDED
    elif execution_status == ExecutionStatus.CANCELLED:
        state.state = TaskState.CANCELLED
        state.cancelled = True
    elif execution_status == ExecutionStatus.WAITING_FOR_APPROVAL:
        # Protocol terminal statuses are limited; represent as failed with rich execution_status.
        state.state = TaskState.FAILED
        state.failure_reason = failure_reason or "Approval required before continuing"
    else:
        state.state = TaskState.FAILED
        if not state.failure_reason and execution_status in {ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT, ExecutionStatus.PARTIALLY_COMPLETED, ExecutionStatus.WAITING_FOR_USER_INPUT}:
            state.failure_reason = failure_reason or summary


def _final_response(state: WorkflowState) -> Dict[str, Any]:
    tools_used = []
    seen = set()
    for tc in state.tool_calls:
        key = (tc.server_id, tc.tool_name)
        if key in seen:
            continue
        seen.add(key)
        tools_used.append({"server_id": tc.server_id, "tool_name": tc.tool_name})
    completed = [s.to_dict() for s in state.plan if s.status == "completed"]
    failed = [s.to_dict() for s in state.plan if s.status in {"failed", "skipped"}]
    remaining = [s.to_dict() for s in state.plan if s.status not in {"completed", "failed", "skipped"}]
    verification = [e.to_dict() for e in state.evaluation_results[-10:]]
    evidence = state.tool_outputs[-10:]
    status_label = {
        ExecutionStatus.FULLY_COMPLETED.value: "Fully completed",
        ExecutionStatus.PARTIALLY_COMPLETED.value: "Partially completed",
        ExecutionStatus.FAILED.value: "Failed",
        ExecutionStatus.WAITING_FOR_APPROVAL.value: "Waiting for approval",
        ExecutionStatus.WAITING_FOR_USER_INPUT.value: "Waiting for user input",
        ExecutionStatus.CANCELLED.value: "Cancelled",
        ExecutionStatus.TIMED_OUT.value: "Timed out",
    }.get(state.execution_status, state.execution_status)

    lines = [
        f"Status: {status_label}",
        f"Objective: {state.objective or state.user_input}",
        f"Completed steps: {len(completed)}/{len(state.plan)}",
    ]
    if tools_used:
        lines.append("Tools used: " + ", ".join(f"{t['server_id']}.{t['tool_name']}" if t.get('server_id') else t['tool_name'] for t in tools_used))
    if state.failure_reason:
        lines.append(f"Issue: {state.failure_reason}")
    if remaining:
        lines.append("Remaining: " + "; ".join(s["description"] for s in remaining[:5]))
    model_output = "\n".join(lines)

    return {
        "execution_status": state.execution_status,
        "objective": state.objective or state.user_input,
        "intended": [s.to_dict() for s in state.plan],
        "completed_steps": completed,
        "failed_steps": failed,
        "remaining_steps": remaining,
        "tools_used": tools_used,
        "tool_calls": [tc.to_dict() for tc in state.tool_calls],
        "tool_outputs": evidence,
        "verification": verification,
        "recovery_history": [r.to_dict() for r in state.recovery_history],
        "summary": state.result_summary,
        "failure_reason": state.failure_reason,
        "model_output": model_output,
    }



# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


async def node_normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    _check_cancelled(state.task_id)
    state.state = TaskState.STARTING
    state.current_step = "understanding_objective"
    state.graph_steps += 1
    state.wall_clock_ms = _elapsed_ms(state)
    state.started_at = state.started_at or time.time()
    state.objective = _normalize_objective(state.user_input)
    details = provider_details()
    state.persistent_state["execution"] = details
    state.persistent_state.setdefault("limits", {
        "max_plan_revisions": MAX_PLAN_REVISIONS,
        "max_execution_steps": MAX_EXECUTION_STEPS,
        "max_retries_per_step": MAX_RETRIES_PER_STEP,
        "max_tool_calls_per_task": MAX_TOOL_CALLS_PER_TASK,
        "max_model_calls_per_task": MAX_MODEL_CALLS_PER_TASK,
        "max_wall_clock_ms": MAX_WALL_CLOCK_MS,
        "max_tool_output_chars": MAX_TOOL_OUTPUT_CHARS,
        "max_model_context_chars": MAX_MODEL_CONTEXT_CHARS,
    })

    if not _require_generation_workspace(state):
        return state_to_checkpoint(state)

    if state.workspace:
        try:
            ws = gen.canonicalize_workspace(state.workspace)
            state.workspace = str(ws)
            state.runtime_context.setdefault("workspace", {})
            state.runtime_context["workspace"] = {"path": state.workspace, "authorized": True}
        except Exception as exc:  # noqa: BLE001
            _mark_terminal(state, ExecutionStatus.FAILED, "Invalid or unauthorized workspace", str(exc))
            state.current_step = "workspace_invalid"
            _event(state.task_id, "task_failed", {"error": str(exc)})
            _persist_task_state(state)
            return state_to_checkpoint(state)

    if _is_generation_task(state):
        state.generation = dict(state.generation or {})
        plan_only = bool(state.generation.get("plan_only")) or "[PLAN ONLY]" in (state.user_input or "").upper()
        state.generation.update({
            "enabled": True,
            "plan_only": plan_only,
            "status": state.generation.get("status") or "planning",
            "workspace": state.workspace,
            "spec": state.generation.get("spec") or {},
            "manifest": state.generation.get("manifest") or [],
            "staged": state.generation.get("staged") or {},
            "applied": state.generation.get("applied") or [],
            "validation_results": state.generation.get("validation_results") or [],
            "repair_attempts": int(state.generation.get("repair_attempts") or 0),
            "max_repair_attempts": gen.MAX_REPAIR_ATTEMPTS,
            "approvals": state.generation.get("approvals") or {},
            "report": state.generation.get("report") or {},
        })
        if not state.generation.get("resumed"):
            _event(state.task_id, "generation.started", {"workspace": state.workspace, "plan_only": plan_only})

    # Resume: reset waiting approval/input steps if approvals/grants present.
    if state.granted_approvals or state.execution_status in {ExecutionStatus.WAITING_FOR_APPROVAL.value, ExecutionStatus.WAITING_FOR_USER_INPUT.value}:
        for s in state.plan:
            if s.status in {"waiting_approval", "waiting_input"}:
                s.status = "pending"
        if state.execution_status in {ExecutionStatus.WAITING_FOR_APPROVAL.value, ExecutionStatus.WAITING_FOR_USER_INPUT.value}:
            state.failure_reason = None

    state.state = TaskState.RUNNING
    state.execution_status = ExecutionStatus.RUNNING.value
    _record_transition(state, "start", "normalize", "task accepted")
    _event(state.task_id, "workflow_started", {"provider": details.get("name"), "objective": state.objective, "generation": bool(state.generation.get("enabled"))})
    _persist_task_state(state)
    return state_to_checkpoint(state)


async def _build_plan_with_model(state: WorkflowState, tools: List[Dict[str, Any]]) -> Optional[List[PlanStep]]:
    if selected_provider() != "ollama":
        return None
    tool_desc = [{"name": t.get("name"), "description": t.get("description", ""), "policy_class": t.get("policy_class", "")} for t in tools[:40]]
    prompt = (
        "Create a JSON execution plan for this objective. Return ONLY JSON with key 'steps' as an array. "
        "Each step must have: step_id, description, expected_outcome, dependencies (array of step_id), "
        "required_capabilities (array), required_tools (array of exact available tool names), risk_classification (low|medium|high), "
        "approval_required (boolean), completion_criteria. Do not invent tools. Use empty required_tools for reasoning/verification steps.\n\n"
        f"Objective: {state.objective}\nAvailable tools: {json.dumps(tool_desc, ensure_ascii=False)}\n"
    )
    try:
        out = await _await_with_cancellation(
            asyncio.to_thread(
                invoke_provider, prompt, None,
                "You are a planning component. Output strict JSON only.", [],
                state.runtime_context,
            ),
            state.task_id,
        )
        state.model_calls += 1
        text = str((out or {}).get("model_output") or "")
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        raw_steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(raw_steps, list):
            return None
        plan: List[PlanStep] = []
        for i, raw in enumerate(raw_steps[:MAX_EXECUTION_STEPS]):
            if not isinstance(raw, dict):
                continue
            step_id = str(raw.get("step_id") or f"step_{i+1}")
            step_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", step_id)[:64] or f"step_{i+1}"
            req_tools = [str(t) for t in (raw.get("required_tools") or []) if str(t).strip()]
            plan.append(PlanStep(
                step_id=step_id,
                description=str(raw.get("description") or "").strip()[:500],
                expected_outcome=str(raw.get("expected_outcome") or "").strip()[:500],
                dependencies=[str(d) for d in (raw.get("dependencies") or []) if str(d).strip()],
                required_capabilities=[str(c) for c in (raw.get("required_capabilities") or []) if str(c).strip()],
                required_tools=req_tools,
                risk_classification=str(raw.get("risk_classification") or "low").lower(),
                approval_required=bool(raw.get("approval_required", False)),
                completion_criteria=str(raw.get("completion_criteria") or "").strip()[:500],
            ))
        return plan or None
    except WorkflowCancelled:
        raise
    except Exception:
        return None


async def node_plan(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    _check_cancelled(state.task_id)
    state.graph_steps += 1
    state.current_step = "planning"
    state.wall_clock_ms = _elapsed_ms(state)

    if state.wall_clock_ms > MAX_WALL_CLOCK_MS:
        _mark_terminal(state, ExecutionStatus.TIMED_OUT, "Task timed out before planning completed", f"Exceeded maximum wall-clock time ({MAX_WALL_CLOCK_MS}ms)")
        _persist_task_state(state)
        return state_to_checkpoint(state)
    if state.graph_steps > MAX_GRAPH_STEPS:
        _mark_terminal(state, ExecutionStatus.FAILED, "Task exceeded graph step limit during planning", f"Exceeded maximum graph steps ({MAX_GRAPH_STEPS})")
        _persist_task_state(state)
        return state_to_checkpoint(state)
    if not _require_generation_workspace(state):
        return state_to_checkpoint(state)

    manager = _get_manager(state.task_id)
    tools_by_name = _tool_index(manager)
    tools = _available_tools(manager)
    has_mcp = bool(manager is not None and manager.configs())

    # Resume: preserve existing plan/progress when possible.
    if state.plan and (state.plan_revisions > 0 or any(s.status != "pending" for s in state.plan) or state.generation.get("manifest")):
        _plan_status_sync(state)
        state.current_step = "planning_resumed"
        _event(state.task_id, "plan_resumed", {"steps": [s.step_id for s in state.plan]})
        _persist_task_state(state)
        return state_to_checkpoint(state)

    generation_task = _is_generation_task(state)
    if generation_task:
        state.generation["enabled"] = True
        state.generation["status"] = "planned"
        state.generation["workspace"] = state.workspace
        plan = _default_generation_plan(state)
    else:
        use_model_planning = selected_provider() == "ollama" and not has_mcp
        plan = await _build_plan_with_model(state, tools) if use_model_planning else None
        if plan is None:
            plan = _default_plan(state.objective, tools_by_name)

    ok, err = _validate_plan(state, plan, tools_by_name)
    if not ok:
        repaired = _default_generation_plan(state) if generation_task else _default_plan(state.objective, tools_by_name)
        ok2, err2 = _validate_plan(state, repaired, tools_by_name)
        if not ok2:
            _mark_terminal(state, ExecutionStatus.FAILED, "Planner produced an invalid plan", err2 or err)
            state.current_step = "planning_failed"
            _event(state.task_id, "plan_rejected", {"error": err2 or err})
            _persist_task_state(state)
            return state_to_checkpoint(state)
        plan = repaired
        _event(state.task_id, "plan_repaired", {"reason": err})

    _set_plan(state, plan)
    state.plan_revisions += 1
    _record_transition(state, "normalize", "plan", "plan created")
    _event(state.task_id, "plan_created", {
        "steps": [s.step_id for s in plan],
        "revision": state.plan_revisions,
    })
    _persist_task_state(state)
    return state_to_checkpoint(state)


def _step_needs_tool(step: PlanStep, tools_by_name: Dict[str, Dict[str, Any]]) -> bool:
    return any(t in tools_by_name for t in step.required_tools)


def _select_tool_for_step(state: WorkflowState, step: PlanStep, tools_by_name: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for name in step.required_tools:
        tool = tools_by_name.get(name)
        if tool:
            return tool
    text = (step.description + " " + state.objective).lower()
    if "search" in text and "web_search" in tools_by_name:
        return tools_by_name["web_search"]
    if any(k in text for k in ["fetch", "url", "page", "documentation", "docs"]) and "fetch_url" in tools_by_name:
        return tools_by_name["fetch_url"]
    if any(k in text for k in ["python", "code", "calculate", "compute", "execute"]) and "run_python" in tools_by_name:
        return tools_by_name["run_python"]
    return None


def _build_tool_arguments(step: PlanStep, tool: Dict[str, Any], state: WorkflowState) -> Dict[str, Any]:
    name = str(tool.get("name") or "")
    objective = state.objective
    prior_text = ""
    for out in state.tool_outputs[-3:]:
        prior_text += "\n" + _text(out.get("result"), 2000)
    if name == "web_search":
        query = objective
        m = re.search(r"(?:for|about)\s+(.+)", objective, re.IGNORECASE)
        if m:
            query = m.group(1)
        query = re.sub(r"\b(search|find|look up|official documentation|documentation|docs|retrieve|summarize|extract)\b", " ", query, flags=re.IGNORECASE)
        query = re.sub(r"\s+", " ", query).strip(" .?:") or objective
        return {"query": query[:200], "max_results": 5}
    if name == "fetch_url":
        url_match = re.search(r"https?://[^\s\"'<>]+", objective)
        if not url_match:
            url_match = re.search(r"https?://[^\s\"'<>]+", prior_text)
        if url_match:
            return {"url": url_match.group(0).rstrip(").,]")}
        return {}
    if name == "run_python":
        lower = objective.lower()
        if "fibonacci" in lower and "20" in lower:
            code = "fib=[0,1]\nfor _ in range(18):\n    fib.append(fib[-1]+fib[-2])\nprint(fib[:20])"
            return {"code": code}
        m = re.search(r"```python\s*(.*?)```", state.user_input, re.DOTALL | re.IGNORECASE)
        if m:
            return {"code": m.group(1).strip()[:20000]}
        return {}
    return {}


async def node_select_next(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    _check_cancelled(state.task_id)
    state.graph_steps += 1
    state.current_step = "selecting_next_step"
    state.wall_clock_ms = _elapsed_ms(state)
    if state.wall_clock_ms > MAX_WALL_CLOCK_MS:
        _mark_terminal(state, ExecutionStatus.TIMED_OUT, "Task timed out", f"Exceeded maximum wall-clock time ({MAX_WALL_CLOCK_MS}ms)")
        _persist_task_state(state)
        return state_to_checkpoint(state)

    _plan_status_sync(state)
    step = _next_runnable_step(state)
    if step is None:
        has_pending = any(s.status == "pending" for s in state.plan)
        if has_pending:
            _mark_terminal(state, ExecutionStatus.PARTIALLY_COMPLETED, "No runnable steps remain", "Dependencies or step states prevent further progress")
        state.current_step_id = None
        state.current_step = "verifying_completion" if not has_pending else "blocked"
        _persist_task_state(state)
        return state_to_checkpoint(state)

    step.status = "running"
    state.current_step = f"executing:{step.step_id}"
    _record_transition(state, "select_next", "execute_step", f"selected {step.step_id}")
    _event(state.task_id, "step_selected", {"step_id": step.step_id, "description": step.description})
    _persist_task_state(state)
    return state_to_checkpoint(state)


async def _invoke_model_for_step(state: WorkflowState, step: PlanStep, manager: Optional[MCPManager], tools: List[Any]) -> Dict[str, Any]:
    conversation_messages = state.persistent_state.get("conversation_messages") or []
    system_prompt = (state.runtime_context.get("system_prompt") or "")

    def _on_delta(text: str) -> None:
        if not text:
            return
        event = _cancellation_event(state.task_id)
        if event is not None and event.is_set():
            return
        _event(state.task_id, "assistant.delta", {"delta": text})

    if selected_provider() == "ollama":
        prepare_provider()
        warm_provider()
        state.model_calls += 1
        if state.model_calls > MAX_MODEL_CALLS_PER_TASK:
            raise ProviderError("model_call_limit", f"Exceeded maximum model calls per task ({MAX_MODEL_CALLS_PER_TASK})")
        if tools:
            limits = state.persistent_state.get("limits") if isinstance(state.persistent_state.get("limits"), dict) else {}
            remaining_model_calls = MAX_MODEL_CALLS_PER_TASK - state.model_calls + 1
            max_tool_rounds = max(1, min(MAX_TOOL_ROUNDS, remaining_model_calls))
            used_tool_calls = max(len(state.tool_calls), len(manager.invocations()) if manager is not None else 0)
            remaining_tool_calls = MAX_TOOL_CALLS_PER_TASK - used_tool_calls
            if remaining_tool_calls < 1:
                raise ProviderError("tool_call_limit", f"Exceeded maximum tool calls per task ({MAX_TOOL_CALLS_PER_TASK})")
            out = await _await_with_cancellation(
                invoke_with_tools(
                    f"Objective: {state.objective}\nCurrent step: {step.description}\nExpected outcome: {step.expected_outcome}\nUse available tools as needed. Return a factual result based on actual tool outputs.",
                    tools,
                    system_prompt=system_prompt,
                    messages=conversation_messages,
                    max_tool_rounds=max_tool_rounds,
                    max_tool_calls=remaining_tool_calls,
                    max_tool_output_chars=int(limits.get("max_tool_output_chars") or MAX_TOOL_OUTPUT_CHARS),
                ),
                state.task_id,
            )
            model_output = str((out or {}).get("model_output") or "")
            if model_output:
                _on_delta(model_output)
            return out
        out = await _await_with_cancellation(
            asyncio.to_thread(
                invoke_provider,
                f"Objective: {state.objective}\nCurrent step: {step.description}\nExpected outcome: {step.expected_outcome}",
                None,
                system_prompt,
                conversation_messages,
                state.runtime_context,
                _on_delta,
            ),
            state.task_id,
        )
        return out
    # local/deterministic path
    out = await _await_with_cancellation(
        asyncio.to_thread(
            invoke_provider,
            state.objective,
            None,
            system_prompt,
            conversation_messages,
            state.runtime_context,
            _on_delta,
        ),
        state.task_id,
    )
    return out


async def _execute_tool_direct(state: WorkflowState, step: PlanStep, manager: MCPManager, tool: Dict[str, Any]) -> Dict[str, Any]:
    name = str(tool.get("name") or "")
    server_id = str(tool.get("server_id") or "")
    args = _build_tool_arguments(step, tool, state)
    if not args and name in {"fetch_url", "run_python"}:
        return {"status": "failed", "error": f"Could not determine safe arguments for tool {name}", "tool_name": name, "server_id": server_id, "arguments": args}
    policy = str(tool.get("policy_class") or "approval_required")
    _check_cancelled(state.task_id)
    state.selected_tool = {"server_id": server_id, "tool_name": name, "arguments": redact(args)}
    _event(state.task_id, "tool_selected", {"step_id": step.step_id, "tool_name": name, "server_id": server_id})

    # approval gate
    if policy != "allowed":
        step.approval_required = True
        return {"status": "blocked", "error": "approval_required", "tool_name": name, "server_id": server_id, "arguments": args, "policy": policy}

    if len(state.tool_calls) >= MAX_TOOL_CALLS_PER_TASK:
        return {"status": "failed", "error": f"Exceeded maximum tool calls per task ({MAX_TOOL_CALLS_PER_TASK})", "tool_name": name, "server_id": server_id, "arguments": args}

    start = time.time()
    try:
        record = await asyncio.wait_for(manager.invoke(server_id, name, args), timeout=DEFAULT_TOOL_TIMEOUT_MS / 1000)
        duration = int((time.time() - start) * 1000)
        tc = ToolCallRecord(
            invocation_id=getattr(record, "invocation_id", ""),
            step_id=step.step_id,
            server_id=server_id,
            tool_name=name,
            arguments=redact(args),
            result=redact({"text": getattr(record, "result_text", ""), "structured": getattr(record, "structured_result", None)}),
            error=getattr(record, "error", None),
            status=getattr(record, "status", "failed"),
            duration_ms=duration,
            timestamp=start,
        )
        state.tool_calls.append(tc)
        output = {
            "step_id": step.step_id,
            "server_id": server_id,
            "tool_name": name,
            "arguments": redact(args),
            "status": tc.status,
            "result": _text(tc.result, MAX_TOOL_OUTPUT_CHARS),
            "error": tc.error,
            "duration_ms": duration,
        }
        state.tool_outputs.append(output)
        _event(state.task_id, "tool_observed", {"step_id": step.step_id, "tool_name": name, "status": tc.status})
        return output
    except asyncio.TimeoutError:
        duration = int((time.time() - start) * 1000)
        err = f"Tool {name} timed out after {DEFAULT_TOOL_TIMEOUT_MS}ms"
        tc = ToolCallRecord(step_id=step.step_id, server_id=server_id, tool_name=name, arguments=redact(args), error=err, status="failed", duration_ms=duration, timestamp=start)
        state.tool_calls.append(tc)
        state.tool_outputs.append({"step_id": step.step_id, "server_id": server_id, "tool_name": name, "arguments": redact(args), "status": "failed", "error": err, "duration_ms": duration})
        return {"status": "failed", "error": err, "tool_name": name, "server_id": server_id, "arguments": args}
    except Exception as exc:  # noqa: BLE001
        duration = int((time.time() - start) * 1000)
        err = f"{type(exc).__name__}: {exc}"
        tc = ToolCallRecord(step_id=step.step_id, server_id=server_id, tool_name=name, arguments=redact(args), error=err, status="failed", duration_ms=duration, timestamp=start)
        state.tool_calls.append(tc)
        state.tool_outputs.append({"step_id": step.step_id, "server_id": server_id, "tool_name": name, "arguments": redact(args), "status": "failed", "error": err, "duration_ms": duration})
        return {"status": "failed", "error": err, "tool_name": name, "server_id": server_id, "arguments": args}
    finally:
        state.selected_tool = None


def _validation_event_details(result: Any, limit: int = 2000) -> Dict[str, Any]:
    """Bound the real validation output carried by a durable event."""
    if not isinstance(result, dict):
        return {}
    details: Dict[str, Any] = {}
    for key in ("name", "status", "exit_code", "error"):
        if result.get(key) is not None:
            details[key] = result[key]
    for key in ("stdout", "stderr"):
        text = str(result.get(key) or "")
        if text:
            details[key] = text[-limit:]
    return details


def _generation_digest(state: WorkflowState, action: str, payload: Any) -> str:
    workspace = os.path.normcase(str(state.workspace)).lower() if os.name == "nt" else str(state.workspace)
    bound = {"task_id": state.task_id, "workspace": workspace, "action": action, "payload": payload}
    return hashlib.sha256(canonical_json(bound).encode("utf-8")).hexdigest()[:16]


def _gen_scope(state: WorkflowState, name: str, digest: str = "") -> str:
    base = f"generation:{name}:{state.task_id}"
    return f"{base}:{digest}" if digest else base


def _has_grant(state: WorkflowState, scope: str) -> bool:
    return scope in (state.granted_approvals or []) or scope in (state.generation.get("approvals_granted") or [])


def _set_pending_approval(state: WorkflowState, scope: str, digest: str, action: str, **details: Any) -> None:
    state.generation["pending_approval"] = {
        "scope": scope, "digest": digest, "action": action, **details,
    }


def _pending_scope_matches(state: WorkflowState, scope: str, digest: str) -> bool:
    pending = state.generation.get("pending_approval")
    return isinstance(pending, dict) and pending.get("scope") == scope and pending.get("digest") == digest


def _consume_grant(state: WorkflowState, scope: str) -> bool:
    if not _has_grant(state, scope):
        return False
    state.granted_approvals = [g for g in (state.granted_approvals or []) if g != scope]
    existing = [g for g in (state.generation.get("approvals_granted") or []) if g != scope]
    state.generation["approvals_granted"] = existing
    consumed = state.generation.setdefault("consumed_approvals", [])
    if scope not in consumed:
        consumed.append(scope)
    return True


def _parse_model_json(text: str) -> Optional[Dict[str, Any]]:
    if not isinstance(text, str):
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


async def _model_json(state: WorkflowState, prompt: str, system: str) -> Optional[Dict[str, Any]]:
    if selected_provider() != "ollama":
        return None
    try:
        prepare_provider()
        warm_provider()
        state.model_calls += 1
        if state.model_calls > MAX_MODEL_CALLS_PER_TASK:
            return None
        out = await _await_with_cancellation(
            asyncio.to_thread(
                invoke_provider, prompt, None, system,
                state.persistent_state.get("conversation_messages") or [],
                state.runtime_context,
            ),
            state.task_id,
        )
        return _parse_model_json(str((out or {}).get("model_output") or ""))
    except WorkflowCancelled:
        raise
    except Exception:
        return None


def _default_generation_spec(state: WorkflowState) -> Dict[str, Any]:
    obj = state.objective
    lower = obj.lower()
    stack = "python"
    project_type = "python_app"
    if "fastapi" in lower:
        stack = "python-fastapi"
        project_type = "api_service"
    elif "react" in lower or "next.js" in lower or "nextjs" in lower or "typescript" in lower:
        stack = "node-react-typescript" if "react" in lower else "node-typescript"
        project_type = "web_app"
    elif "go" in lower and "cli" in lower:
        stack = "go"
        project_type = "cli_tool"
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", obj.lower()).strip("-")[:40] or "generated-project"
    return {
        "project_name": name,
        "project_type": project_type,
        "objective": obj,
        "features": [],
        "stack": stack,
        "runtime_requirements": [],
        "structure": [],
        "dependencies": [],
        "validation_requirements": [],
        "security_considerations": ["no secrets", "authorized workspace only"],
        "assumptions": ["Generated minimal verifiable scaffold based on request; user should review and extend."],
    }


def _default_generation_manifest(state: WorkflowState, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    stack = str(spec.get("stack") or "").lower()
    name = str(spec.get("project_name") or "generated-project")
    obj = str(spec.get("objective") or state.objective)
    if "fastapi" in stack or ("python" in stack and "api" in str(spec.get("project_type") or "")):
        return [
            {"path": "README.md", "operation": "write", "description": "Project overview", "content": f"# {name}\n\n{obj}\n\n## Run\n\n```bash\npython -m venv .venv\n# install dependencies with approval\n# uvicorn app.main:app --reload\n```\n\n## Tests\n\n```bash\npython -m pytest -q\n```\n"},
            {"path": "requirements.txt", "operation": "write", "description": "Python dependencies", "content": "fastapi\nuvicorn\npytest\nhttpx\n"},
            {"path": "app/__init__.py", "operation": "write", "description": "App package", "content": ""},
            {"path": "app/main.py", "operation": "write", "description": "FastAPI app backed by SQLite", "content": '''import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

app = FastAPI(title="Task API")


def database_path() -> Path:
    return Path(os.environ.get("TASKS_DB_PATH", "tasks.db"))


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def database():
    with closing(connect()) as connection:
        with connection:
            yield connection


def initialize_database() -> None:
    with database() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
                completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0, 1))
            )
            """
        )


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    completed: bool = False


class Task(TaskIn):
    id: int


def row_to_task(row: sqlite3.Row) -> Task:
    return Task(id=row["id"], title=row["title"], completed=bool(row["completed"]))


initialize_database()


@app.get('/health')
def health():
    with database() as connection:
        connection.execute('SELECT 1').fetchone()
    return {'status': 'ok'}


@app.get('/tasks', response_model=list[Task])
def list_tasks():
    with database() as connection:
        rows = connection.execute('SELECT id, title, completed FROM tasks ORDER BY id').fetchall()
    return [row_to_task(row) for row in rows]


@app.post('/tasks', response_model=Task, status_code=201)
def create_task(payload: TaskIn):
    with database() as connection:
        cursor = connection.execute(
            'INSERT INTO tasks (title, completed) VALUES (?, ?)',
            (payload.title, int(payload.completed)),
        )
        row = connection.execute(
            'SELECT id, title, completed FROM tasks WHERE id = ?', (cursor.lastrowid,)
        ).fetchone()
    return row_to_task(row)


@app.get('/tasks/{task_id}', response_model=Task)
def get_task(task_id: int):
    with database() as connection:
        row = connection.execute(
            'SELECT id, title, completed FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail='Task not found')
    return row_to_task(row)


@app.put('/tasks/{task_id}', response_model=Task)
def update_task(task_id: int, payload: TaskIn):
    with database() as connection:
        cursor = connection.execute(
            'UPDATE tasks SET title = ?, completed = ? WHERE id = ?',
            (payload.title, int(payload.completed), task_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail='Task not found')
        row = connection.execute(
            'SELECT id, title, completed FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
    return row_to_task(row)


@app.delete('/tasks/{task_id}', status_code=204)
def delete_task(task_id: int):
    with database() as connection:
        cursor = connection.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail='Task not found')
    return Response(status_code=204)
'''},
            {"path": "tests/test_app.py", "operation": "write", "description": "API and SQLite tests", "content": '''import os
import sqlite3
import tempfile
from pathlib import Path

test_root = tempfile.mkdtemp(prefix="crane-fastapi-test-")
os.environ["TASKS_DB_PATH"] = str(Path(test_root) / "tasks.db")

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health():
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


def test_crud_persists_to_sqlite():
    created = client.post('/tasks', json={'title': 'Write tests', 'completed': False})
    assert created.status_code == 201
    task_id = created.json()['id']

    listed = client.get('/tasks')
    assert listed.status_code == 200
    assert any(item['id'] == task_id for item in listed.json())

    fetched = client.get(f'/tasks/{task_id}')
    assert fetched.status_code == 200
    assert fetched.json()['title'] == 'Write tests'

    updated = client.put(f'/tasks/{task_id}', json={'title': 'Write tests', 'completed': True})
    assert updated.status_code == 200
    assert updated.json()['completed'] is True

    connection = sqlite3.connect(os.environ['TASKS_DB_PATH'])
    try:
        stored = connection.execute(
            'SELECT title, completed FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
    finally:
        connection.close()
    assert stored == ('Write tests', 1)

    deleted = client.delete(f'/tasks/{task_id}')
    assert deleted.status_code == 204
    assert client.get(f'/tasks/{task_id}').status_code == 404


def test_validation_rejects_empty_title():
    response = client.post('/tasks', json={'title': ''})
    assert response.status_code == 422
'''},
        ]
    if "go" in stack:
        return [
            {"path": "README.md", "operation": "write", "description": "Project overview", "content": f"# {name}\n\n{obj}\n\n## Run\n\n```bash\ngo run .\n```\n\n## Test\n\n```bash\ngo test ./...\n```\n"},
            {"path": "go.mod", "operation": "write", "description": "Go module", "content": "module example.com/generated\n\ngo 1.22\n"},
            {"path": "main.go", "operation": "write", "description": "CLI entrypoint", "content": "package main\n\nimport (\n\t\"fmt\"\n\t\"os\"\n)\n\nfunc Run(args []string) string {\n\tif len(args) == 0 {\n\t\treturn \"generated CLI: ok\"\n\t}\n\treturn fmt.Sprintf(\"generated CLI: %s\", args[0])\n}\n\nfunc main() {\n\tfmt.Fprintln(os.Stdout, Run(os.Args[1:]))\n}\n"},
            {"path": "main_test.go", "operation": "write", "description": "Basic test", "content": "package main\n\nimport \"testing\"\n\nfunc TestRun(t *testing.T) {\n\tif got := Run(nil); got != \"generated CLI: ok\" {\n\t\tt.Fatalf(\"unexpected: %s\", got)\n\t}\n}\n"},
        ]
    # Generic Python scaffold
    return [
        {"path": "README.md", "operation": "write", "description": "Project overview", "content": f"# {name}\n\n{obj}\n\n## Run\n\n```bash\npython -m app\n```\n"},
        {"path": "app/__init__.py", "operation": "write", "description": "Package", "content": ""},
        {"path": "app/__main__.py", "operation": "write", "description": "Entrypoint", "content": "from .core import summarize\n\nif __name__ == '__main__':\n    print(summarize())\n"},
        {"path": "app/core.py", "operation": "write", "description": "Core logic", "content": f"def summarize() -> str:\n    return {obj!r}\n"},
        {"path": "tests/test_core.py", "operation": "write", "description": "Basic test", "content": "from app.core import summarize\n\ndef test_summarize():\n    assert isinstance(summarize(), str)\n    assert summarize()\n"},
    ]


MAX_REPAIR_CONTEXT_FILES = 24
MAX_REPAIR_CONTEXT_FILE_CHARS = 4000
MAX_REPAIR_CONTEXT_CHARS = 24000


def _generation_repair_source_context(state: WorkflowState, workspace: Any) -> str:
    """Read a bounded, redacted snapshot of generated files relevant to repair.

    Only paths already present in the validated generation manifest or apply
    records are considered. The workspace is revalidated for every path; no
    directory walk or arbitrary user-file discovery is performed.
    """
    raw_paths: List[str] = []
    for records in (state.generation.get("manifest"), state.generation.get("applied")):
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, dict):
                value = record.get("path")
                if isinstance(value, str) and value.strip():
                    raw_paths.append(value)

    unique_paths: List[str] = []
    seen: set[str] = set()
    for rel_path in raw_paths:
        key = os.path.normcase(rel_path.replace("\\", "/"))
        if key not in seen:
            seen.add(key)
            unique_paths.append(rel_path)
    if not unique_paths:
        return "(No validated generated file paths are available.)"

    sections: List[str] = []
    total_chars = 0
    for rel_path in unique_paths[:MAX_REPAIR_CONTEXT_FILES]:
        remaining = MAX_REPAIR_CONTEXT_CHARS - total_chars
        if remaining <= 0:
            break
        try:
            source_path = gen.validate_relative_path(workspace, rel_path)
            info = gen._lstat_path(source_path, rel_path)
            if info is None or not stat.S_ISREG(info.st_mode):
                continue
            with source_path.open("r", encoding="utf-8", errors="replace") as stream:
                content = stream.read(MAX_REPAIR_CONTEXT_FILE_CHARS + 1)
        except (OSError, gen.GenerationError):
            continue

        truncated = len(content) > MAX_REPAIR_CONTEXT_FILE_CHARS
        content = content[:MAX_REPAIR_CONTEXT_FILE_CHARS]
        content = gen.redact_validation_result({"source": content}).get("source", "")
        content = str(content)[:remaining]
        if not content:
            continue
        suffix = "\n...[file context truncated]" if truncated else ""
        section = f"--- {rel_path} ---\n{content}{suffix}"
        if len(section) > remaining:
            section = section[:remaining]
        sections.append(section)
        total_chars += len(section)

    if not sections:
        return "(No readable regular generated source files were found in the authorized workspace.)"
    return "\n\n".join(sections)


async def _exec_generation_step(state: WorkflowState, step: PlanStep) -> Dict[str, Any]:
    if not state.workspace:
        return {"status": "failed", "error": "No authorized workspace provided for generation task"}
    try:
        ws = gen.canonicalize_workspace(state.workspace)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}

    state.generation.setdefault("enabled", True)
    state.generation.setdefault("approvals_granted", list(state.granted_approvals))
    sid = step.step_id

    if sid == "analyze_requirements":
        spec = None
        if selected_provider() == "ollama":
            prompt = (
                "Return ONLY JSON for a software project specification with keys: "
                "project_name, project_type, objective, features (array), stack, runtime_requirements (array), "
                "structure (array), dependencies (array), validation_requirements (array), security_considerations (array), assumptions (array). "
                f"User request: {state.user_input}\nAuthorized workspace: {state.workspace}\n"
            )
            spec = await _model_json(state, prompt, "You produce strict JSON project specifications. No markdown.")
        if not isinstance(spec, dict):
            spec = _default_generation_spec(state)
        spec["objective"] = spec.get("objective") or state.objective
        state.generation["spec"] = spec
        state.generation["status"] = "spec_ready"
        _event(state.task_id, "generation.spec_ready", {"project_name": spec.get("project_name"), "stack": spec.get("stack")})
        return {"status": "succeeded", "output": {"spec": spec}}

    if sid == "plan_project":
        spec = state.generation.get("spec") if isinstance(state.generation.get("spec"), dict) else _default_generation_spec(state)
        manifest = None
        if selected_provider() == "ollama":
            prompt = (
                "Return ONLY JSON with keys: files (array of {path, operation, description, content}), "
                "validation_commands (array of {name, argv, cwd, timeout_seconds, required}), "
                "notes (array). operation must be exactly \"write\" for new or updated files or exactly \"delete\" for removals. "
                "argv[0] must be an allowlisted runtime executable; use python -m pytest -q for Python tests, never bare pytest. "
                "Paths must be relative to the authorized workspace. No absolute paths, no .., no .git/env writes. "
                "Generate complete file contents, no placeholders. "
                f"Specification: {json.dumps(spec, ensure_ascii=False)[:12000]}\n"
            )
            obj = await _model_json(state, prompt, "You produce strict JSON file manifests for safe project generation.")
            if isinstance(obj, dict) and isinstance(obj.get("files"), list):
                manifest = obj.get("files")
                state.generation["validation_plan"] = obj.get("validation_commands") if isinstance(obj.get("validation_commands"), list) else []
                state.generation["plan_notes"] = obj.get("notes") if isinstance(obj.get("notes"), list) else []
        if not isinstance(manifest, list) or not manifest:
            manifest = _default_generation_manifest(state, spec)
            state.generation.setdefault("validation_plan", [])
        try:
            ops = gen.validate_file_operations(ws, manifest)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": f"Invalid file manifest: {exc}"}
        state.generation["manifest"] = [op.to_dict() for op in ops]
        state.generation["status"] = "planned"
        _event(state.task_id, "plan.ready", {"file_count": len(ops)})
        return {"status": "succeeded", "output": {"file_count": len(ops), "files": [op.path for op in ops]}}

    if sid == "generate_files":
        manifest = state.generation.get("manifest") if isinstance(state.generation.get("manifest"), list) else []
        if not manifest:
            return {"status": "failed", "error": "No validated file manifest available"}
        try:
            ops = gen.validate_file_operations(ws, manifest)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": f"Manifest validation failed: {exc}"}
        collisions = gen.inspect_collisions(ws, ops)
        overwrite_paths = [c["path"] for c in collisions if c.get("exists")]
        needs_overwrite = any(c.get("exists") for c in collisions)
        action_payload = {
            "manifest": [op.to_dict() for op in ops],
            "overwrite_paths": overwrite_paths,
            "plan_only": bool(state.generation.get("plan_only")),
        }
        digest = _generation_digest(state, "apply", action_payload)
        apply_scope = _gen_scope(state, "apply", digest)
        overwrite_scope = _gen_scope(state, "overwrite", digest)
        required_scope = overwrite_scope if needs_overwrite else apply_scope
        if not (_has_grant(state, required_scope) and _pending_scope_matches(state, required_scope, digest)):
            if needs_overwrite:
                reason = "Applying generated files will overwrite existing workspace files."
                details = {"paths": overwrite_paths}
            else:
                reason = "Approval required before writing generated files to the authorized workspace."
                details = {"paths": [op.path for op in ops], "file_count": len(ops)}
            _set_pending_approval(state, required_scope, digest, "overwrite" if needs_overwrite else "apply", reason=reason, **details)
            _event(state.task_id, "approval_required", {"scope": required_scope, "digest": digest, **details})
            return {"status": "blocked", "error": "approval_required", "approval_scope": required_scope, "paths": details.get("paths", [])[:50]}

        if not _consume_grant(state, required_scope):
            return {"status": "failed", "error": "generation approval grant is stale"}
        state.generation.pop("pending_approval", None)
        if state.generation.get("plan_only"):
            state.generation["status"] = "plan_only"
            return {"status": "succeeded", "output": {"plan_only": True, "planned_files": [op.path for op in ops], "file_count": len(ops)}}

        stage = gen.stage_operations(ws, ops)
        state.generation["staged"] = stage
        for f in stage.get("files", []):
            if isinstance(f, dict):
                _event(state.task_id, "generation.file.staged", {"path": f.get("path"), "operation": f.get("operation")})
        applied = gen.apply_staged_operations(ws, stage, approved_overwrite_paths=overwrite_paths)
        state.generation["applied"] = applied.get("applied", [])
        state.generation["status"] = "files_applied"
        for rec in state.generation["applied"]:
            if isinstance(rec, dict):
                _event(state.task_id, "generation.file.written", {"path": rec.get("path"), "operation": rec.get("operation")})
        gen.cleanup_stage(stage, ws)
        return {"status": "succeeded", "output": {"applied_count": applied.get("count", 0), "files": [r.get("path") for r in state.generation["applied"] if isinstance(r, dict)]}}

    if sid == "validate_project":
        if state.generation.get("plan_only"):
            state.generation["validation_results"] = [{"name": "plan_only", "status": "skipped", "reason": "Plan-only mode; no files applied."}]
            return {"status": "succeeded", "output": {"skipped": True, "reason": "plan_only"}}
        commands = []
        raw_cmds = state.generation.get("validation_plan")
        if isinstance(raw_cmds, list) and raw_cmds:
            for c in raw_cmds:
                if not isinstance(c, dict):
                    continue
                argv = c.get("argv") if isinstance(c.get("argv"), list) else []
                if not argv:
                    continue
                commands.append(gen.ValidationCommand(
                    name=str(c.get("name") or "command"),
                    argv=[str(a) for a in argv],
                    cwd=str(c.get("cwd") or "."),
                    timeout_seconds=int(c.get("timeout_seconds") or gen.DEFAULT_COMMAND_TIMEOUT_SECONDS),
                    required=bool(c.get("required", True)),
                ))
        if not commands:
            commands = gen.default_validation_commands(ws)
        if not commands:
            state.generation["validation_results"] = [{"name": "no_validation", "status": "skipped", "reason": "No applicable validation commands detected."}]
            _event(state.task_id, "validation.completed", {"status": "skipped"})
            return {"status": "succeeded", "output": {"validation_results": state.generation["validation_results"]}}
        command_payload = [cmd.to_dict() for cmd in commands]
        digest = _generation_digest(state, "validate", command_payload)
        validate_scope = _gen_scope(state, "validate", digest)
        if not (_has_grant(state, validate_scope) and _pending_scope_matches(state, validate_scope, digest)):
            _set_pending_approval(
                state, validate_scope, digest, "validate",
                reason="Approval required before executing validation commands in the workspace.",
                commands=[cmd.name for cmd in commands],
            )
            _event(state.task_id, "approval_required", {"scope": validate_scope, "digest": digest, "commands": [cmd.name for cmd in commands]})
            return {"status": "blocked", "error": "approval_required", "approval_scope": validate_scope}
        if not _consume_grant(state, validate_scope):
            return {"status": "failed", "error": "validation approval grant is stale"}
        state.generation.pop("pending_approval", None)
        results = []
        for cmd in commands:
            _event(state.task_id, "validation.started", {"name": cmd.name, "argv": cmd.argv})
            res = gen.run_validation_command(ws, cmd)
            results.append(res)
            _event(state.task_id, "validation.completed", _validation_event_details(res))
            if res.get("status") != "succeeded" and cmd.required:
                break
        safe_results = [gen.redact_validation_result(r) if isinstance(r, dict) else r for r in results]
        state.generation["validation_results"] = safe_results
        ok = all(r.get("status") == "succeeded" for r in results if r.get("status") != "skipped")
        state.generation["status"] = "validated" if ok else "validation_failed"
        return {"status": "succeeded" if ok else "failed", "output": {"validation_results": safe_results}}

    if sid == "repair_project":
        if state.generation.get("plan_only"):
            return {"status": "succeeded", "output": {"skipped": True, "reason": "plan_only"}}
        results = state.generation.get("validation_results") if isinstance(state.generation.get("validation_results"), list) else []
        failed = [r for r in results if isinstance(r, dict) and r.get("status") == "failed"]
        if not failed:
            return {"status": "succeeded", "output": {"message": "No repairs needed"}}
        attempts = int(state.generation.get("repair_attempts") or 0)
        if attempts >= gen.MAX_REPAIR_ATTEMPTS:
            return {"status": "failed", "error": f"Repair attempt limit reached ({gen.MAX_REPAIR_ATTEMPTS})", "output": {"validation_results": results}}

        # The exact repair manifest is produced before approval and persisted in
        # the checkpoint. A grant can never authorize a regenerated manifest.
        repair_manifest = state.generation.get("pending_repair_manifest")
        if not isinstance(repair_manifest, list) or not repair_manifest:
            repair_manifest = None
            if selected_provider() == "ollama":
                source_context = _generation_repair_source_context(state, ws)
                prompt = (
                    "Return ONLY JSON with key 'files' as an array of {path, operation, description, content} for minimal repairs. "
                    "operation must be exactly \"write\" for new or updated files or exactly \"delete\" for removals. "
                    "Only include files that must change. Paths must be relative to workspace. Provide complete file contents, never patches or placeholders. "
                    "Diagnose the actual validation output against the current source below. Follow tracebacks and import chains; for missing imports/modules, "
                    "check the referenced module path, package __init__.py files, symbol names, and declared dependencies. Fix the root cause rather than "
                    "suppressing the error or adding a stub. Preserve existing public behavior and unrelated code. Include every file required for a coherent fix.\n"
                    f"Objective: {state.objective}\n"
                    f"Validation failures: {json.dumps(failed, ensure_ascii=False)[:12000]}\n"
                    f"Current manifest paths: {json.dumps([m.get('path') for m in (state.generation.get('manifest') or []) if isinstance(m, dict)], ensure_ascii=False)}\n"
                    "Current generated workspace source (fresh disk reads, bounded and secret-redacted):\n"
                    f"{source_context}\n"
                )
                obj = await _model_json(state, prompt, "You produce strict JSON minimal repair file operations.")
                if isinstance(obj, dict) and isinstance(obj.get("files"), list):
                    repair_manifest = obj.get("files")
        if not repair_manifest:
            return {"status": "failed", "error": "Unable to generate repair plan; manual review required", "output": {"validation_results": results}}
        try:
            ops = gen.validate_file_operations(ws, repair_manifest)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": f"Invalid repair manifest: {exc}"}
        normalized = [op.to_dict() for op in ops]
        collisions = gen.inspect_collisions(ws, ops)
        overwrite_paths = [item["path"] for item in collisions if item.get("exists")]
        digest = _generation_digest(state, "repair", {
            "manifest": normalized, "overwrite_paths": overwrite_paths, "attempt": attempts + 1,
        })
        stored_digest = state.generation.get("pending_repair_digest")
        if stored_digest is not None and stored_digest != digest:
            return {"status": "failed", "error": "Repair manifest changed after approval", "output": {"validation_results": results}}
        state.generation["pending_repair_manifest"] = normalized
        state.generation["pending_repair_digest"] = digest
        repair_scope = _gen_scope(state, "repair", digest)
        if not (_has_grant(state, repair_scope) and _pending_scope_matches(state, repair_scope, digest)):
            reason = ("Repair will overwrite existing workspace files." if overwrite_paths
                      else "Approval required before applying repair changes.")
            _set_pending_approval(
                state, repair_scope, digest, "repair", reason=reason,
                paths=overwrite_paths, file_count=len(ops),
            )
            _event(state.task_id, "approval_required", {
                "scope": repair_scope, "digest": digest, "paths": overwrite_paths[:50], "repair": True,
            })
            return {
                "status": "blocked", "error": "approval_required", "approval_scope": repair_scope,
                "paths": overwrite_paths[:50],
            }
        if not _consume_grant(state, repair_scope):
            return {"status": "failed", "error": "repair approval grant is stale", "output": {"validation_results": results}}
        state.generation.pop("pending_approval", None)

        state.generation["repair_attempts"] = attempts + 1
        _event(state.task_id, "repair.started", {"attempt": attempts + 1, "digest": digest})
        try:
            stage = gen.stage_operations(ws, ops)
            try:
                applied = gen.apply_staged_operations(ws, stage, approved_overwrite_paths=overwrite_paths)
            finally:
                gen.cleanup_stage(stage, ws)
            state.generation.pop("pending_repair_manifest", None)
            state.generation.pop("pending_repair_digest", None)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": f"Repair apply failed: {exc}"}
        repair_applied = [r.get("path") for r in applied.get("applied", []) if isinstance(r, dict)]
        for rec in applied.get("applied", []):
            if isinstance(rec, dict):
                _event(state.task_id, "generation.file.written", {
                    "path": rec.get("path"), "operation": rec.get("operation"),
                    "repair_attempt": attempts + 1, "source": "repair",
                })
        _event(state.task_id, "repair.applied", {
            "attempt": attempts + 1, "digest": digest,
            "files": repair_applied[:50], "file_count": len(repair_applied),
        })
        # Re-run validations
        cmds = []
        raw_cmds = state.generation.get("validation_plan")
        if isinstance(raw_cmds, list) and raw_cmds:
            for c in raw_cmds:
                if isinstance(c, dict) and isinstance(c.get("argv"), list):
                    cmds.append(gen.ValidationCommand(name=str(c.get("name") or "command"), argv=[str(a) for a in c.get("argv")], cwd=str(c.get("cwd") or "."), timeout_seconds=int(c.get("timeout_seconds") or gen.DEFAULT_COMMAND_TIMEOUT_SECONDS), required=bool(c.get("required", True))))
        if not cmds:
            cmds = gen.default_validation_commands(ws)
        safe_results2 = []
        for cmd in cmds:
            _event(state.task_id, "validation.started", {"name": cmd.name, "argv": cmd.argv, "repair_attempt": attempts + 1})
            res = gen.redact_validation_result(gen.run_validation_command(ws, cmd))
            safe_results2.append(res)
            details = _validation_event_details(res)
            details["repair_attempt"] = attempts + 1
            _event(state.task_id, "validation.completed", details)
            if res.get("status") != "succeeded" and cmd.required:
                break
        state.generation["validation_results"] = safe_results2
        ok = all(r.get("status") == "succeeded" for r in safe_results2 if r.get("status") != "skipped")
        state.generation["status"] = "validated" if ok else "validation_failed"
        _event(state.task_id, "repair.completed", {
            "attempt": attempts + 1, "status": "succeeded" if ok else "failed",
            "files": repair_applied[:50], "file_count": len(repair_applied),
            "validation_status": [r.get("status") for r in safe_results2],
            "exit_codes": [r.get("exit_code") for r in safe_results2],
        })
        return {"status": "succeeded" if ok else "failed", "output": {"applied": applied.get("applied", []), "validation_results": safe_results2, "attempt": attempts + 1}}

    if sid == "final_report":
        spec = state.generation.get("spec") if isinstance(state.generation.get("spec"), dict) else {}
        applied = state.generation.get("applied") if isinstance(state.generation.get("applied"), list) else []
        validations = state.generation.get("validation_results") if isinstance(state.generation.get("validation_results"), list) else []
        report = {
            "workspace": state.workspace,
            "project_name": spec.get("project_name"),
            "stack": spec.get("stack"),
            "files": [a.get("path") for a in applied if isinstance(a, dict)],
            "applied_operations": applied,
            "validations": validations,
            "repair_attempts": state.generation.get("repair_attempts", 0),
            "status": state.generation.get("status"),
        }
        state.generation["report"] = report
        _event(state.task_id, "generation.completed", {"status": report["status"], "file_count": len(report["files"])})
        return {"status": "succeeded", "output": report}

    return {"status": "failed", "error": f"Unknown generation step: {sid}"}


async def node_execute_step(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    _check_cancelled(state.task_id)
    state.graph_steps += 1
    state.wall_clock_ms = _elapsed_ms(state)
    if state.wall_clock_ms > MAX_WALL_CLOCK_MS:
        _mark_terminal(state, ExecutionStatus.TIMED_OUT, "Task timed out", f"Exceeded maximum wall-clock time ({MAX_WALL_CLOCK_MS}ms)")
        _persist_task_state(state)
        return state_to_checkpoint(state)

    manager = _get_manager(state.task_id)
    tools_by_name = _tool_index(manager)
    step = next((s for s in state.plan if s.step_id == state.current_step_id), None)
    if step is None:
        _mark_terminal(state, ExecutionStatus.FAILED, "Execution state lost current step", "No current step selected")
        _persist_task_state(state)
        return state_to_checkpoint(state)

    state.current_step = f"executing:{step.step_id}"
    _event(state.task_id, "step_started", {"step_id": step.step_id, "description": step.description})

    if state.generation.get("enabled") and step.step_id in {
        "analyze_requirements", "plan_project", "generate_files", "validate_project", "repair_project", "final_report",
    }:
        try:
            out = await _exec_generation_step(state, step)
            # Normalize for evaluation.
            if isinstance(out, dict):
                if "output" in out and "result" not in out:
                    out["result"] = out["output"]
                if out.get("status") == "succeeded" and not out.get("model_output"):
                    out["model_output"] = _text(out.get("result") or out.get("output") or "Generation step completed", 4000)
                if out.get("status") == "blocked":
                    state.persistent_state["last_action_output"] = out
                    step.status = "waiting_approval"
                    state.execution_status = ExecutionStatus.WAITING_FOR_APPROVAL.value
                    state.failure_reason = str(out.get("reason") or out.get("error") or "Approval required")
                    _event(state.task_id, "approval_required", {"step_id": step.step_id, "scope": out.get("approval_scope")})
                    _persist_task_state(state)
                    return state_to_checkpoint(state)
            state.persistent_state["last_action_output"] = out
            if isinstance(out, dict):
                state.persistent_state["last_model_output"] = {
                    "execution_mode": "generation_runtime",
                    "provider": selected_provider(),
                    "model_output": out.get("model_output") or _text(out.get("result") or out.get("output") or "", 4000),
                }
        except WorkflowCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("generation step failed for %s", state.task_id)
            state.persistent_state["last_action_output"] = {"status": "failed", "error": f"Generation step failed: {type(exc).__name__}"}
        _persist_task_state(state)
        return state_to_checkpoint(state)

    # If there are MCP tools and provider is local, do not pretend autonomous execution.
    if manager is not None and manager.configs() and selected_provider() == "local" and _step_needs_tool(step, tools_by_name):
        _mark_terminal(state, ExecutionStatus.FAILED, "Configured provider cannot execute MCP tools autonomously", "MCP tools require CRANE_MODEL_PROVIDER=ollama with a tool-capable local model")
        step.status = "failed"
        state.failed_steps = [s.step_id for s in state.plan if s.status in {"failed", "skipped"}]
        _persist_task_state(state)
        return state_to_checkpoint(state)

    try:
        tool = _select_tool_for_step(state, step, tools_by_name)
        has_mcp = bool(manager is not None and manager.configs())
        use_model_tool_loop = bool(has_mcp and selected_provider() == "ollama")
        if tool and has_mcp and not use_model_tool_loop:
            output = await _execute_tool_direct(state, step, manager, tool)
            state.persistent_state["last_action_output"] = output
            if output.get("status") == "blocked" or output.get("error") == "approval_required":
                step.status = "waiting_approval"
                state.execution_status = ExecutionStatus.WAITING_FOR_APPROVAL.value
                state.failure_reason = "Approval required before executing a consequential tool action"
                _event(state.task_id, "approval_required", {"step_id": step.step_id, "tool": tool.get("name")})
                _persist_task_state(state)
                return state_to_checkpoint(state)
        elif use_model_tool_loop:
            model_tools = manager.langchain_tools()
            if not model_tools:
                raise ProviderError("mcp_connect_failed", "No MCP tools were available for the configured provider")
            out = await _invoke_model_for_step(state, step, manager, model_tools)
            state.persistent_state["last_action_output"] = out
            state.persistent_state["last_model_output"] = out
            _event(state.task_id, "provider_result", {"step_id": step.step_id, "provider": out.get("provider"), "model": out.get("model")})
            _merge_tool_call_records(state, step.step_id, out, manager)
            # If model output reports blocked/approval, surface it.
            if isinstance(out, dict) and out.get("error") == "approval_required":
                state.execution_status = ExecutionStatus.WAITING_FOR_APPROVAL.value
                state.failure_reason = "Approval required before continuing"
                step.status = "waiting_approval"
                _persist_task_state(state)
                return state_to_checkpoint(state)
        else:
            out = await _invoke_model_for_step(state, step, manager, [])
            state.persistent_state["last_action_output"] = out
            state.persistent_state["last_model_output"] = out
            _event(state.task_id, "provider_result", {"step_id": step.step_id, "provider": out.get("provider"), "model": out.get("model")})
    except ProviderError as exc:
        state.persistent_state["last_action_output"] = {"status": "failed", "error": exc.message, "code": exc.code}
        state.persistent_state["last_error_code"] = exc.code
        state.last_error = exc.message
        _event(state.task_id, "provider_failed", {"code": exc.code, "step_id": step.step_id})
    except WorkflowCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("step execution failed for %s", state.task_id)
        state.persistent_state["last_action_output"] = {"status": "failed", "error": "Step execution failed unexpectedly"}
        state.last_error = "Step execution failed unexpectedly"

    _persist_task_state(state)
    return state_to_checkpoint(state)


def _evaluation_from_output(state: WorkflowState, step: PlanStep, output: Any) -> EvaluationResult:
    # Structured evaluation with deterministic checks first.
    if isinstance(output, dict) and output.get("status") == "blocked":
        return EvaluationResult(step.step_id, EvaluationOutcome.NEEDS_APPROVAL.value, "Action requires approval", [str(output.get("error") or "approval_required")], "wait_for_approval")
    if isinstance(output, dict) and output.get("status") == "failed":
        err = str(output.get("error") or output.get("message") or "tool failed")
        lower = err.lower()
        if "approval" in lower:
            return EvaluationResult(step.step_id, EvaluationOutcome.NEEDS_APPROVAL.value, err, [err], "wait_for_approval")
        if "timed out" in lower or "timeout" in lower or "temporar" in lower or "connection" in lower:
            return EvaluationResult(step.step_id, EvaluationOutcome.FAILED.value, err, [err], "retry")
        if "invalid" in lower or "schema" in lower or "argument" in lower:
            return EvaluationResult(step.step_id, EvaluationOutcome.FAILED.value, err, [err], "repair_args")
        if "not found" in lower and "tool" in lower:
            return EvaluationResult(step.step_id, EvaluationOutcome.NEEDS_REPLANNING.value, err, [err], "replan")
        return EvaluationResult(step.step_id, EvaluationOutcome.FAILED.value, err, [err], "recover")

    # Model output path
    if isinstance(output, dict) and ("model_output" in output or "execution_mode" in output):
        model_output = str(output.get("model_output") or "").strip()
        tool_calls = output.get("tool_calls") if isinstance(output.get("tool_calls"), list) else []
        exec_mode = str(output.get("execution_mode") or "")
        if exec_mode == "deterministic_local" and _step_needs_tool(step, _tool_index(_get_manager(state.task_id))):
            return EvaluationResult(step.step_id, EvaluationOutcome.FAILED.value, "Deterministic local mode cannot autonomously execute MCP tools", [model_output or "local mode"], "fail")
        current_outputs = [item for item in state.tool_outputs if item.get("step_id") == step.step_id]
        failed_outputs = [
            item for item in current_outputs
            if str(item.get("status") or "").lower() in {"failed", "error", "blocked"}
        ]
        if failed_outputs:
            approval_outputs = [item for item in failed_outputs if str(item.get("status") or "").lower() == "blocked"]
            if approval_outputs:
                evidence = [str(item.get("error") or "approval_required")[:300] for item in approval_outputs]
                return EvaluationResult(step.step_id, EvaluationOutcome.NEEDS_APPROVAL.value, "A tool action requires approval", evidence, "wait_for_approval")
            evidence = [
                str(item.get("error") or _record_result_text(item.get("result")))[:300]
                for item in failed_outputs
            ]
            outcome = EvaluationOutcome.FAILED if len(failed_outputs) == len(current_outputs) else EvaluationOutcome.PARTIALLY_COMPLETED
            return EvaluationResult(step.step_id, outcome.value, "One or more tool calls did not complete", evidence, "recover")
        if tool_calls or model_output or current_outputs:
            if _step_needs_tool(step, _tool_index(_get_manager(state.task_id))) and not tool_calls and not current_outputs:
                return EvaluationResult(step.step_id, EvaluationOutcome.PARTIALLY_COMPLETED.value, "Model responded without required tool evidence", [model_output[:200]], "replan")
            return EvaluationResult(step.step_id, EvaluationOutcome.COMPLETED.value, "Model produced a grounded step result", [model_output[:300] or "tool output observed"], "continue")
        if exec_mode == "deterministic_local":
            return EvaluationResult(step.step_id, EvaluationOutcome.COMPLETED.value, "Deterministic local step completed", ["deterministic_local"], "continue")
        return EvaluationResult(step.step_id, EvaluationOutcome.FAILED.value, "Model returned empty output", [], "recover")

    # Tool output path
    if isinstance(output, dict):
        status = str(output.get("status") or "")
        text = _text(output.get("result"), 2000)
        if status == "succeeded" and text:
            # deterministic check: for run_python fibonacci 20, verify content
            if step.step_id == "execute_code" or any(t == "run_python" for t in step.required_tools):
                if "fibonacci" in state.objective.lower() and "20" in state.objective.lower():
                    ok = "6765" in text and "0" in text
                    if ok:
                        return EvaluationResult(step.step_id, EvaluationOutcome.COMPLETED.value, "Code executed and produced expected Fibonacci output", [text[:300]], "continue")
                    return EvaluationResult(step.step_id, EvaluationOutcome.PARTIALLY_COMPLETED.value, "Code executed but output did not match expected Fibonacci sequence", [text[:300]], "recover")
            if any(t == "web_search" for t in step.required_tools):
                if "http" in text.lower() or "result" in text.lower() or "title" in text.lower():
                    return EvaluationResult(step.step_id, EvaluationOutcome.COMPLETED.value, "Search returned actionable results", [text[:300]], "continue")
                return EvaluationResult(step.step_id, EvaluationOutcome.PARTIALLY_COMPLETED.value, "Search returned no clearly actionable results", [text[:300]], "recover")
            if any(t == "fetch_url" for t in step.required_tools):
                if len(text.strip()) > 40:
                    return EvaluationResult(step.step_id, EvaluationOutcome.COMPLETED.value, "URL content retrieved", [text[:300]], "continue")
                return EvaluationResult(step.step_id, EvaluationOutcome.PARTIALLY_COMPLETED.value, "Retrieved content was too small or empty", [text[:300]], "recover")
            return EvaluationResult(step.step_id, EvaluationOutcome.COMPLETED.value, "Tool produced output", [text[:300]], "continue")
        if status == "succeeded":
            return EvaluationResult(step.step_id, EvaluationOutcome.PARTIALLY_COMPLETED.value, "Tool succeeded but produced no useful output", [], "recover")
    return EvaluationResult(step.step_id, EvaluationOutcome.FAILED.value, "No actionable output observed", [], "recover")


async def node_observe_evaluate(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    _check_cancelled(state.task_id)
    state.graph_steps += 1
    state.current_step = "evaluating"
    state.wall_clock_ms = _elapsed_ms(state)

    step = next((s for s in state.plan if s.step_id == state.current_step_id), None)
    if step is None:
        _mark_terminal(state, ExecutionStatus.FAILED, "Missing step during evaluation", "No current step to evaluate")
        _persist_task_state(state)
        return state_to_checkpoint(state)

    output = state.persistent_state.get("last_action_output")
    # Compatibility: capture provider-observed tool calls not already persisted as MCP records.
    _merge_tool_call_records(state, step.step_id, output, _get_manager(state.task_id))

    evaluation = _evaluation_from_output(state, step, output)
    state.evaluation_results.append(evaluation)
    if len(state.evaluation_results) > 200:
        state.evaluation_results = state.evaluation_results[-200:]

    if evaluation.outcome == EvaluationOutcome.COMPLETED.value:
        step.status = "completed"
    elif evaluation.outcome == EvaluationOutcome.NEEDS_APPROVAL.value:
        step.status = "waiting_approval"
        state.execution_status = ExecutionStatus.WAITING_FOR_APPROVAL.value
    elif evaluation.outcome == EvaluationOutcome.NEEDS_MORE_INFORMATION.value:
        step.status = "waiting_input"
        state.execution_status = ExecutionStatus.WAITING_FOR_USER_INPUT.value
    elif evaluation.outcome in {EvaluationOutcome.FAILED.value, EvaluationOutcome.NEEDS_REPLANNING.value, EvaluationOutcome.PARTIALLY_COMPLETED.value}:
        # leave running/pending decision to recovery
        pass

    _plan_status_sync(state)
    _event(state.task_id, "step_evaluated", {"step_id": step.step_id, "outcome": evaluation.outcome, "summary": evaluation.summary})
    _persist_task_state(state)
    return state_to_checkpoint(state)


async def node_recover(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    _check_cancelled(state.task_id)
    state.graph_steps += 1
    state.current_step = "recovering"
    state.wall_clock_ms = _elapsed_ms(state)

    step = next((s for s in state.plan if s.step_id == state.current_step_id), None)
    evaluation = state.evaluation_results[-1] if state.evaluation_results else None
    if step is None or evaluation is None:
        _mark_terminal(state, ExecutionStatus.FAILED, "Recovery could not identify failed step", state.failure_reason or "Missing recovery context")
        _persist_task_state(state)
        return state_to_checkpoint(state)

    outcome = evaluation.outcome
    if outcome == EvaluationOutcome.NEEDS_APPROVAL.value:
        step.status = "waiting_approval"
        _mark_terminal(state, ExecutionStatus.WAITING_FOR_APPROVAL, "Waiting for approval", "Approval required before continuing")
        _event(state.task_id, "waiting_approval", {"step_id": step.step_id})
        _persist_task_state(state)
        return state_to_checkpoint(state)
    if outcome == EvaluationOutcome.NEEDS_MORE_INFORMATION.value:
        step.status = "waiting_input"
        _mark_terminal(state, ExecutionStatus.WAITING_FOR_USER_INPUT, "Waiting for user input", evaluation.summary or "Additional user information is required")
        _event(state.task_id, "waiting_input", {"step_id": step.step_id})
        _persist_task_state(state)
        return state_to_checkpoint(state)

    # bounded retries per step
    step_retries = sum(1 for r in state.recovery_history if r.step_id == step.step_id)
    err = evaluation.summary or "Step failed"
    classification = "non_recoverable"
    action = "fail"
    recovered = False

    generation_validation_failure = (
        bool(state.generation.get("enabled"))
        and step.step_id == "validate_project"
        and outcome == EvaluationOutcome.FAILED.value
    )
    if generation_validation_failure:
        # Re-running the same validation cannot repair generated code. Mark the
        # step failed so the repair step becomes the next runnable step.
        classification = "generation_validation"
        action = "repair_step"
    elif step_retries < MAX_RETRIES_PER_STEP and len(state.tool_calls) < MAX_TOOL_CALLS_PER_TASK and state.graph_steps < MAX_GRAPH_STEPS:
        if evaluation.next_action in {"retry", "repair_args"}:
            classification = "transient_or_repairable"
            action = "retry_step"
            recovered = True
            step.status = "pending"
        elif evaluation.next_action == "replan" and state.plan_revisions < MAX_PLAN_REVISIONS:
            classification = "replan"
            action = "replan"
            recovered = True
            step.status = "pending"
        elif evaluation.next_action == "recover":
            classification = "recoverable"
            action = "retry_step"
            recovered = True
            step.status = "pending"

    rec = RecoveryRecord(step_id=step.step_id, error=err, classification=classification, action=action, recovered=recovered)
    state.recovery_history.append(rec)
    if len(state.recovery_history) > 100:
        state.recovery_history = state.recovery_history[-100:]
    state.retry_count += 1
    _event(state.task_id, "recovery_attempt", rec.to_dict())

    if not recovered:
        step.status = "failed"
        _plan_status_sync(state)
        # If there are remaining pending steps, we can continue with partial completion; else fail.
        if any(s.status == "pending" for s in state.plan):
            state.execution_status = ExecutionStatus.PARTIALLY_COMPLETED.value
            state.current_step = "selecting_next_step"
        else:
            _mark_terminal(state, ExecutionStatus.PARTIALLY_COMPLETED if state.completed_steps else ExecutionStatus.FAILED, "Task could not be fully completed", err)
    else:
        if action == "replan":
            state.plan_revisions += 1
            state.current_step = "planning"
            # simple replan: keep completed steps, rebuild remaining from default planner
            manager = _get_manager(state.task_id)
            tools_by_name = _tool_index(manager)
            new_tail = _default_generation_plan(state) if _is_generation_task(state) else _default_plan(state.objective, tools_by_name)
            # avoid duplicate step IDs
            existing = {s.step_id for s in state.plan}
            rebuilt: List[PlanStep] = [s for s in state.plan if s.status == "completed"]
            for s in new_tail:
                sid = s.step_id
                n = 2
                while sid in existing:
                    sid = f"{s.step_id}_r{n}"
                    n += 1
                s.step_id = sid
                existing.add(sid)
                rebuilt.append(s)
            ok, val_err = _validate_plan(state, rebuilt, tools_by_name)
            if ok and len(rebuilt) <= MAX_EXECUTION_STEPS:
                _set_plan(state, rebuilt)
                _event(state.task_id, "replanned", {"revision": state.plan_revisions, "steps": [s.step_id for s in rebuilt]})
            else:
                step.status = "failed"
                _plan_status_sync(state)
                _mark_terminal(state, ExecutionStatus.PARTIALLY_COMPLETED if state.completed_steps else ExecutionStatus.FAILED, "Replanning failed validation", val_err or err)
        else:
            state.current_step = "selecting_next_step"

    _persist_task_state(state)
    return state_to_checkpoint(state)


async def node_verify(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = _as_state(raw)
    _check_cancelled(state.task_id)
    state.graph_steps += 1
    state.current_step = "verifying_completion"
    state.wall_clock_ms = _elapsed_ms(state)
    _plan_status_sync(state)

    completed = [s for s in state.plan if s.status == "completed"]
    failed = [s for s in state.plan if s.status in {"failed", "skipped"}]
    pending = [s for s in state.plan if s.status == "pending"]
    waiting_approval = [s for s in state.plan if s.status == "waiting_approval"]
    waiting_input = [s for s in state.plan if s.status == "waiting_input"]

    if state.execution_status == ExecutionStatus.WAITING_FOR_APPROVAL.value or waiting_approval:
        _mark_terminal(state, ExecutionStatus.WAITING_FOR_APPROVAL, "Waiting for approval", state.failure_reason or "Approval required before continuing")
    elif state.execution_status == ExecutionStatus.WAITING_FOR_USER_INPUT.value or waiting_input:
        _mark_terminal(state, ExecutionStatus.WAITING_FOR_USER_INPUT, "Waiting for user input", state.failure_reason or "Additional user input is required")
    elif state.cancelled:
        _mark_terminal(state, ExecutionStatus.CANCELLED, "Task cancelled", "Task cancelled by supervisor request")
    elif pending:
        _mark_terminal(state, ExecutionStatus.PARTIALLY_COMPLETED, "Task ended with incomplete steps", "Not all planned steps completed")
    elif failed and completed:
        _mark_terminal(state, ExecutionStatus.PARTIALLY_COMPLETED, "Task partially completed", state.failure_reason or "Some steps failed")
    elif failed and not completed:
        _mark_terminal(state, ExecutionStatus.FAILED, "Task failed", state.failure_reason or "All steps failed")
    else:
        # verify objective with evidence
        has_evidence = bool(state.tool_outputs) or bool(state.evaluation_results)
        generation_evidence_error = None
        if _is_generation_task(state):
            generation_evidence_error = _generation_evidence_error(state)
            if generation_evidence_error:
                has_evidence = False
        if generation_evidence_error:
            _mark_terminal(state, ExecutionStatus.FAILED, "Generation could not be verified", generation_evidence_error)
        elif not has_evidence and selected_provider() == "local" and any(s.required_tools for s in state.plan):
            _mark_terminal(state, ExecutionStatus.FAILED, "No verified evidence was produced", "Local mode could not execute required tools")
        else:
            _mark_terminal(state, ExecutionStatus.FULLY_COMPLETED, "Objective completed and verified", None)

    final = _final_response(state)
    state.final_result = final
    # Preserve compatibility for existing tests/consumers: top-level result includes model_output and tool_calls
    model_output = final.get("model_output")
    # Prefer last model output text when available
    last_model = state.persistent_state.get("last_model_output")
    if isinstance(last_model, dict) and str(last_model.get("model_output") or "").strip():
        model_output = str(last_model.get("model_output")).strip()
        final["model_output"] = model_output
    state.result = {
        "execution_mode": (last_model or {}).get("execution_mode") if isinstance(last_model, dict) else (state.persistent_state.get("execution") or {}).get("name"),
        "provider": (last_model or {}).get("provider") if isinstance(last_model, dict) else selected_provider(),
        "model": (last_model or {}).get("model") if isinstance(last_model, dict) else state.persistent_state.get("execution", {}).get("model"),
        "model_output": model_output,
        "tool_calls": [tc.to_dict() for tc in state.tool_calls],
        "execution_status": state.execution_status,
        "plan": [s.to_dict() for s in state.plan],
        "completed_steps": [s.to_dict() for s in state.plan if s.status == "completed"],
        "failed_steps": [s.to_dict() for s in state.plan if s.status in {"failed", "skipped"}],
        "remaining_steps": [s.to_dict() for s in state.plan if s.status not in {"completed", "failed", "skipped"}],
        "verification": [e.to_dict() for e in state.evaluation_results],
        "tool_outputs": state.tool_outputs[-20:],
        "recovery_history": [r.to_dict() for r in state.recovery_history],
        "summary": state.result_summary,
    }
    if state.failure_reason:
        state.result["failure_reason"] = state.failure_reason
    err_code = state.persistent_state.get("last_error_code")
    if not err_code and isinstance(state.persistent_state.get("last_action_output"), dict):
        err_code = state.persistent_state["last_action_output"].get("code")
    if err_code and "code" not in state.result:
        state.result["code"] = err_code
    if isinstance(last_model, dict):
        for k in ["notice", "normalized_input", "input_bytes", "context_used", "execution_mode", "provider", "model"]:
            if k in last_model and k not in state.result:
                state.result[k] = last_model[k]
        # Preserve explicit model_output=None for deterministic local compatibility.
        if last_model.get("execution_mode") == "deterministic_local":
            state.result["model_output"] = last_model.get("model_output")
    # Back-compat: deterministic local should report execution_mode clearly and not claim LLM output.
    if state.result.get("execution_mode") == "deterministic_local" and state.result.get("model_output") is None:
        pass
    if state.generation.get("enabled"):
        report = state.generation.get("report") or {
            "workspace": state.workspace,
            "status": state.generation.get("status"),
            "files": [a.get("path") for a in (state.generation.get("applied") or []) if isinstance(a, dict)],
            "validations": state.generation.get("validation_results") or [],
            "repair_attempts": state.generation.get("repair_attempts", 0),
        }
        state.result["generation"] = report
        state.result["workspace"] = state.workspace
        # Provide a clear model_output for CLI display.
        if not str(state.result.get("model_output") or "").strip() or state.result.get("model_output") == final.get("model_output"):
            lines = [
                f"Generation status: {state.execution_status}",
                f"Workspace: {state.workspace}",
                f"Files: {len(report.get('files') or [])}",
            ]
            vals = report.get("validations") or []
            if vals:
                ok = sum(1 for v in vals if isinstance(v, dict) and v.get("status") == "succeeded")
                lines.append(f"Validations: {ok}/{len(vals)} succeeded")
            if report.get("repair_attempts"):
                lines.append(f"Repair attempts: {report.get('repair_attempts')}")
            state.result["model_output"] = "\n".join(lines)
            final["model_output"] = state.result["model_output"]
    state.current_step = "verified"
    _event(state.task_id, "verification_completed", {"execution_status": state.execution_status})
    _persist_task_state(state)
    return state_to_checkpoint(state)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _route_after_plan(raw: Dict[str, Any]) -> str:
    state = _as_state(raw)
    if state.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
        return "end"
    if not state.plan:
        return "end"
    return "select_next"


def _route_after_select(raw: Dict[str, Any]) -> str:
    state = _as_state(raw)
    if state.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
        return "verify"
    step = next((s for s in state.plan if s.step_id == state.current_step_id), None)
    if step is None or step.status != "running":
        return "verify"
    return "execute_step"


def _route_after_execute(raw: Dict[str, Any]) -> str:
    state = _as_state(raw)
    if state.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
        return "verify"
    if state.execution_status == ExecutionStatus.WAITING_FOR_APPROVAL.value:
        return "verify"
    return "observe_evaluate"


def _route_after_evaluate(raw: Dict[str, Any]) -> str:
    state = _as_state(raw)
    if state.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
        return "verify"
    evaluation = state.evaluation_results[-1] if state.evaluation_results else None
    if evaluation is None:
        return "verify"
    if evaluation.outcome == EvaluationOutcome.COMPLETED.value:
        return "select_next"
    if evaluation.outcome in {EvaluationOutcome.NEEDS_APPROVAL.value, EvaluationOutcome.NEEDS_MORE_INFORMATION.value}:
        return "recover"
    return "recover"


def _route_after_recover(raw: Dict[str, Any]) -> str:
    state = _as_state(raw)
    if state.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
        return "verify"
    if state.current_step == "planning":
        return "plan"
    if state.execution_status in {ExecutionStatus.WAITING_FOR_APPROVAL.value, ExecutionStatus.WAITING_FOR_USER_INPUT.value}:
        return "verify"
    # if recovered, go select next; if not, select next may verify/partial
    return "select_next"


# ---------------------------------------------------------------------------
# Graph build / execution
# ---------------------------------------------------------------------------


def _build_graph():
    graph = StateGraph(dict)
    graph.add_node("normalize", node_normalize)
    graph.add_node("plan", node_plan)
    graph.add_node("select_next", node_select_next)
    graph.add_node("execute_step", node_execute_step)
    graph.add_node("observe_evaluate", node_observe_evaluate)
    graph.add_node("recover", node_recover)
    graph.add_node("verify", node_verify)

    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "plan")
    graph.add_conditional_edges("plan", _route_after_plan, {"select_next": "select_next", "end": END})
    graph.add_conditional_edges("select_next", _route_after_select, {"execute_step": "execute_step", "verify": "verify"})
    graph.add_conditional_edges("execute_step", _route_after_execute, {"observe_evaluate": "observe_evaluate", "verify": "verify"})
    graph.add_conditional_edges("observe_evaluate", _route_after_evaluate, {"select_next": "select_next", "recover": "recover"})
    graph.add_conditional_edges("recover", _route_after_recover, {"plan": "plan", "select_next": "select_next", "verify": "verify"})
    graph.add_edge("verify", END)
    return graph.compile()


_COMPILED_GRAPH = None
_GRAPH_LOCK = threading.Lock()


def _graph():
    global _COMPILED_GRAPH
    with _GRAPH_LOCK:
        if _COMPILED_GRAPH is None:
            _COMPILED_GRAPH = _build_graph()
        return _COMPILED_GRAPH


async def _build_manager(task_id: str, mcp_servers: Optional[List[Dict[str, Any]]], granted_approvals: Optional[List[str]]) -> Optional[MCPManager]:
    if not mcp_servers:
        return None
    manager = configure_manager(task_id=task_id, configs=mcp_servers, granted_fingerprints=granted_approvals or [])
    connect_results = await connect_all(manager)
    if not any(connect_results.values()):
        logger.warning("no MCP servers connected: %s", connect_results)
    return manager


def _build_initial_state(
    user_input: str,
    task_id: str,
    resume: bool,
    checkpoint: Optional[Dict[str, Any]],
    conversation_id: Optional[str],
    conversation_messages: Optional[List[Dict[str, Any]]],
    workspace: Optional[str] = None,
) -> WorkflowState:
    if resume and isinstance(checkpoint, dict):
        state = state_from_checkpoint(checkpoint)
        state.task_id = task_id or state.task_id
        state.user_input = user_input or state.user_input
        state.objective = state.objective or _normalize_objective(state.user_input)
        state.conversation_id = conversation_id or state.conversation_id
        if workspace:
            original = os.path.normcase(os.path.abspath(str(state.workspace))) if state.workspace else ""
            supplied = os.path.normcase(os.path.abspath(str(workspace)))
            if original and original != supplied:
                raise ValueError("resume cannot change the authorized workspace")
            state.workspace = workspace
    else:
        state = WorkflowState(task_id=task_id, user_input=user_input, objective=_normalize_objective(user_input), conversation_id=conversation_id, workspace=(workspace or ""))
    state.python_runtime_pid = os.getpid()
    runtime_context = build_runtime_context(
        user_input=state.user_input,
        conversation_id=state.conversation_id,
        conversation_messages=conversation_messages or [],
        mcp_tools=[],
    )
    if state.workspace:
        runtime_context["workspace"] = {"path": state.workspace, "authorized": True}
    state.runtime_context = runtime_context
    state.persistent_state["conversation_messages"] = list(conversation_messages or [])[-MAX_CONTEXT_MESSAGES:]
    return state


def _result_payload(state: WorkflowState) -> Dict[str, Any]:
    result = dict(state.result or {})
    result.setdefault("execution_status", state.execution_status)
    result.setdefault("summary", state.result_summary)
    result.setdefault("plan", [s.to_dict() for s in state.plan])
    result.setdefault("tool_calls", [tc.to_dict() for tc in state.tool_calls])
    result.setdefault("tool_outputs", state.tool_outputs[-20:])
    result.setdefault("verification", [e.to_dict() for e in state.evaluation_results])
    result.setdefault("recovery_history", [r.to_dict() for r in state.recovery_history])
    result.setdefault("model_output", (state.final_result or {}).get("model_output") or state.result_summary or "")
    if state.failure_reason and "failure_reason" not in result:
        result["failure_reason"] = state.failure_reason
    return result


def _terminal_protocol_status(state: WorkflowState) -> str:
    if state.execution_status == ExecutionStatus.CANCELLED.value or state.state == TaskState.CANCELLED:
        return "cancelled"
    if state.execution_status == ExecutionStatus.FULLY_COMPLETED.value or state.state == TaskState.SUCCEEDED:
        return "succeeded"
    return "failed"


async def execute_task(
    user_input: str,
    task_id: str = "",
    resume: bool = False,
    checkpoint: Optional[Dict[str, Any]] = None,
    mcp_servers: Optional[List[Dict[str, Any]]] = None,
    granted_approvals: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    conversation_id: Optional[str] = None,
    conversation_messages: Optional[List[Dict[str, Any]]] = None,
    python_runtime_pid: Optional[int] = None,
    resume_checkpoint: Optional[Dict[str, Any]] = None,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a task through the LangGraph System 2 workflow.

    Returns a protocol-compatible terminal result dict.
    """
    checkpoint = resume_checkpoint if resume_checkpoint is not None else checkpoint
    resume = bool(resume or checkpoint is not None)
    cancel_event = threading.Event()
    _register_cancellation(task_id, cancel_event)
    manager: Optional[MCPManager] = None
    state = _build_initial_state(user_input, task_id, resume, checkpoint, conversation_id, conversation_messages, workspace)
    if python_runtime_pid is not None:
        state.python_runtime_pid = python_runtime_pid
    env_restore: Dict[str, Optional[str]] = {}
    try:
        if env:
            for key, value in env.items():
                name = str(key)
                if not name or value is None:
                    continue
                env_restore[name] = os.environ.get(name)
                os.environ[name] = str(value)
        # Connect MCP before planning so tool discovery is real.
        manager = await _build_manager(task_id, mcp_servers, granted_approvals)
        tools = _available_tools(manager)
        state.runtime_context = build_runtime_context(
            user_input=state.user_input,
            conversation_id=state.conversation_id,
            conversation_messages=conversation_messages or [],
            mcp_tools=tools,
        )
        if state.workspace:
            state.runtime_context["workspace"] = {"path": state.workspace, "authorized": True}
        _set_manager(task_id, manager)

        # Fail fast for impossible provider/tool combinations.
        if manager and manager.configs() and selected_provider() == "local":
            state.state = TaskState.FAILED
            state.execution_status = ExecutionStatus.FAILED.value
            state.failure_reason = "MCP tools require CRANE_MODEL_PROVIDER=ollama with a tool-capable local model"
            state.result_summary = "Configured provider cannot execute MCP tools autonomously"
            state.current_step = "provider_incompatible"
            state.result = {
                "execution_mode": "deterministic_local",
                "provider": "local",
                "model_output": "MCP tools are configured but the deterministic local provider cannot execute them autonomously. Configure CRANE_MODEL_PROVIDER=ollama with a tool-capable model.",
                "execution_status": state.execution_status,
                "summary": state.result_summary,
                "failure_reason": state.failure_reason,
                "tool_calls": [],
                "tool_outputs": [],
                "verification": [],
                "plan": [],
            }
            _persist_task_state(state)
            return {
                "task_id": task_id,
                "status": "failed",
                "result": state.result,
                "result_summary": state.result_summary,
                "failure_reason": state.failure_reason,
                "graph_steps": state.graph_steps,
                "wall_clock_ms": _elapsed_ms(state),
                "current_step": state.current_step,
                "checkpoint": state_to_checkpoint(state),
                "execution_status": state.execution_status,
            }

        final_raw = await _graph().ainvoke(state_to_checkpoint(state), config={"recursion_limit": MAX_GRAPH_STEPS * 4})
        state = _as_state(final_raw)
        _pop_manager(task_id)
        if cancel_event.is_set() and state.state != TaskState.CANCELLED:
            _mark_terminal(state, ExecutionStatus.CANCELLED, "Task cancelled", "Task cancelled by supervisor request")
            state.final_result = _final_response(state)
            state.result = _result_payload(state)
        if state.state not in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
            # Should not happen, but never leave ambiguous terminal state.
            verified = await node_verify(state_to_checkpoint(state))
            state = _as_state(verified)
        if not state.final_result:
            state.final_result = _final_response(state)
        if not state.result:
            state.result = _result_payload(state)
        _persist_task_state(state)
    except WorkflowCancelled:
        state = _as_state(state_to_checkpoint(state))
        _pop_manager(task_id)
        _mark_terminal(state, ExecutionStatus.CANCELLED, "Task cancelled", "Task cancelled by supervisor request")
        state.final_result = _final_response(state)
        state.result = _result_payload(state)
        _persist_task_state(state)
    except ProviderError as exc:
        _pop_manager(task_id)
        state.state = TaskState.FAILED
        state.execution_status = ExecutionStatus.FAILED.value
        state.failure_reason = exc.message
        state.result_summary = "Model provider error"
        state.result = {
            "execution_mode": selected_provider(),
            "provider": selected_provider(),
            "model_output": exc.message,
            "execution_status": state.execution_status,
            "summary": state.result_summary,
            "failure_reason": state.failure_reason,
            "tool_calls": [tc.to_dict() for tc in state.tool_calls],
            "tool_outputs": state.tool_outputs[-20:],
            "verification": [e.to_dict() for e in state.evaluation_results],
            "recovery_history": [r.to_dict() for r in state.recovery_history],
            "plan": [s.to_dict() for s in state.plan],
        }
        _persist_task_state(state)
    except Exception as exc:  # noqa: BLE001
        logger.exception("workflow execution failed for %s", task_id)
        _pop_manager(task_id)
        state.state = TaskState.FAILED
        state.execution_status = ExecutionStatus.FAILED.value
        state.failure_reason = f"Workflow execution failed: {type(exc).__name__}"
        state.result_summary = "Workflow execution failed"
        state.result = {
            "execution_status": state.execution_status,
            "summary": state.result_summary,
            "failure_reason": state.failure_reason,
            "model_output": state.failure_reason,
            "tool_calls": [tc.to_dict() for tc in state.tool_calls],
            "tool_outputs": state.tool_outputs[-20:],
            "verification": [e.to_dict() for e in state.evaluation_results],
            "recovery_history": [r.to_dict() for r in state.recovery_history],
            "plan": [s.to_dict() for s in state.plan],
        }
        _persist_task_state(state)
    finally:
        for name, previous in env_restore.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        _unregister_cancellation(task_id)
        _pop_manager(task_id)
        if manager is not None:
            with contextlib.suppress(Exception):
                await shutdown_all(manager)

    status = _terminal_protocol_status(state)
    result = _result_payload(state)
    return {
        "task_id": task_id,
        "status": status,
        "result": result,
        "result_summary": state.result_summary,
        "failure_reason": state.failure_reason,
        "graph_steps": state.graph_steps,
        "wall_clock_ms": state.wall_clock_ms or _elapsed_ms(state),
        "current_step": state.current_step,
        "checkpoint": state_to_checkpoint(state),
        "execution_status": state.execution_status,
    }
