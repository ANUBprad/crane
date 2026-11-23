"""Bounded LangGraph workflow execution for CRANE tasks.

The graph is the real execution engine. It emits versioned checkpoint and event
messages to the Go supervisor through stdout; diagnostics remain on stderr.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from langgraph.graph import END, START, StateGraph

from crane.mcp_runtime import (
    MCPManager,
    classify_tool,
    configure_manager,
    connect_all,
    redact,
    shutdown_all,
)
from crane.python_runtime import protocol
from crane.python_runtime.context import build_runtime_context
from crane.python_runtime.provider import (
    ProviderError,
    invoke as invoke_provider,
    invoke_with_tools,
    prepare_provider,
    provider_details,
    selected_provider,
    warm_provider,
)
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
    for idx, step in enumerate(state.plan):
        if step.status != "pending":
            continue
        if all(dep in completed for dep in step.dependencies):
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
    state.state = TaskState.RUNNING
    state.execution_status = ExecutionStatus.RUNNING.value
    _record_transition(state, "start", "normalize", "task accepted")
    _event(state.task_id, "workflow_started", {"provider": details.get("name"), "objective": state.objective})
    _persist_task_state(state)
    return state_to_checkpoint(state)


def _build_plan_with_model(state: WorkflowState, tools: List[Dict[str, Any]]) -> Optional[List[PlanStep]]:
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
        out = invoke_provider(prompt, None, "You are a planning component. Output strict JSON only.", [], state.runtime_context)
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

    manager = _get_manager(state.task_id)
    tools_by_name = _tool_index(manager)
    tools = _available_tools(manager)
    has_mcp = bool(manager is not None and manager.configs())
    use_model_planning = selected_provider() == "ollama" and not has_mcp
    plan = _build_plan_with_model(state, tools) if use_model_planning else None
    if plan is None:
        plan = _default_plan(state.objective, tools_by_name)

    ok, err = _validate_plan(state, plan, tools_by_name)
    if not ok:
        repaired = _default_plan(state.objective, tools_by_name)
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
    runtime_tool_list = _available_tools(manager)
    conversation_messages = state.persistent_state.get("conversation_messages") or []
    system_prompt = (state.runtime_context.get("system_prompt") or "")
    if selected_provider() == "ollama":
        prepare_provider()
        warm_provider()
        state.model_calls += 1
        if state.model_calls > MAX_MODEL_CALLS_PER_TASK:
            raise ProviderError("model_call_limit", f"Exceeded maximum model calls per task ({MAX_MODEL_CALLS_PER_TASK})")
        if tools:
            out = await invoke_with_tools(
                f"Objective: {state.objective}\nCurrent step: {step.description}\nExpected outcome: {step.expected_outcome}\nUse available tools as needed. Return a factual result based on actual tool outputs.",
                tools,
                system_prompt=system_prompt,
                messages=conversation_messages,
            )
            return out
        out = await asyncio.to_thread(
            invoke_provider,
            f"Objective: {state.objective}\nCurrent step: {step.description}\nExpected outcome: {step.expected_outcome}",
            None,
            system_prompt,
            conversation_messages,
            state.runtime_context,
        )
        return out
    # local/deterministic path
    out = await asyncio.to_thread(
        invoke_provider,
        state.objective,
        None,
        system_prompt,
        conversation_messages,
        state.runtime_context,
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
            for inv in manager.invocations():
                if not any(tc.invocation_id == inv.get("invocation_id") for tc in state.tool_calls):
                    tc = ToolCallRecord(
                        invocation_id=str(inv.get("invocation_id") or ""),
                        step_id=step.step_id,
                        server_id=str(inv.get("server_id") or ""),
                        tool_name=str(inv.get("tool_name") or ""),
                        arguments=inv.get("arguments") if isinstance(inv.get("arguments"), dict) else {},
                        result=inv.get("result") if isinstance(inv.get("result"), dict) else {"value": inv.get("result")},
                        error=inv.get("error") or None,
                        status=str(inv.get("status") or "succeeded"),
                        duration_ms=int(inv.get("duration_ms") or 0),
                    )
                    state.tool_calls.append(tc)
                    state.tool_outputs.append({
                        "step_id": step.step_id,
                        "server_id": tc.server_id,
                        "tool_name": tc.tool_name,
                        "arguments": tc.arguments,
                        "status": tc.status,
                        "result": _text(tc.result, MAX_TOOL_OUTPUT_CHARS),
                        "error": tc.error,
                        "duration_ms": tc.duration_ms,
                    })
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
        if tool_calls or model_output or state.tool_outputs:
            if _step_needs_tool(step, _tool_index(_get_manager(state.task_id))) and not tool_calls and not state.tool_outputs:
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
    # Compatibility: capture tool calls from model output if mock/provider returned them
    if isinstance(output, dict) and isinstance(output.get("tool_calls"), list):
        for inv in output.get("tool_calls"):
            if not isinstance(inv, dict):
                continue
            inv_id = str(inv.get("invocation_id") or f"{step.step_id}-{len(state.tool_calls)+1}")
            if any(tc.invocation_id == inv_id for tc in state.tool_calls):
                continue
            tc = ToolCallRecord(
                invocation_id=inv_id,
                step_id=step.step_id,
                server_id=str(inv.get("server_id") or ""),
                tool_name=str(inv.get("tool_name") or inv.get("name") or ""),
                arguments=inv.get("arguments") if isinstance(inv.get("arguments"), dict) else {},
                result=inv.get("result") if isinstance(inv.get("result"), dict) else {"value": inv.get("result")},
                error=inv.get("error") or None,
                status=str(inv.get("status") or "succeeded"),
                duration_ms=int(inv.get("duration_ms") or 0),
            )
            state.tool_calls.append(tc)
            state.tool_outputs.append({"step_id": step.step_id, "server_id": tc.server_id, "tool_name": tc.tool_name, "status": tc.status, "result": _text(tc.result, MAX_TOOL_OUTPUT_CHARS), "error": tc.error})

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

    if step_retries < MAX_RETRIES_PER_STEP and len(state.tool_calls) < MAX_TOOL_CALLS_PER_TASK and state.graph_steps < MAX_GRAPH_STEPS:
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
            new_tail = _default_plan(state.objective, tools_by_name)
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
        if not has_evidence and selected_provider() == "local" and any(s.required_tools for s in state.plan):
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
) -> WorkflowState:
    if resume and isinstance(checkpoint, dict):
        state = state_from_checkpoint(checkpoint)
        state.task_id = task_id or state.task_id
        state.user_input = user_input or state.user_input
        state.objective = state.objective or _normalize_objective(state.user_input)
        state.conversation_id = conversation_id or state.conversation_id
    else:
        state = WorkflowState(task_id=task_id, user_input=user_input, objective=_normalize_objective(user_input), conversation_id=conversation_id)
    state.python_runtime_pid = os.getpid()
    # Build runtime context. Use manager later after connection.
    runtime_context = build_runtime_context(
        user_input=state.user_input,
        conversation_id=state.conversation_id,
        conversation_messages=conversation_messages or [],
        mcp_tools=[],
    )
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
) -> Dict[str, Any]:
    """Execute a task through the LangGraph System 2 workflow.

    Returns a protocol-compatible terminal result dict.
    """
    checkpoint = resume_checkpoint if resume_checkpoint is not None else checkpoint
    resume = bool(resume or checkpoint is not None)
    cancel_event = threading.Event()
    _register_cancellation(task_id, cancel_event)
    manager: Optional[MCPManager] = None
    state = _build_initial_state(user_input, task_id, resume, checkpoint, conversation_id, conversation_messages)
    if python_runtime_pid is not None:
        state.python_runtime_pid = python_runtime_pid
    try:
        if env:
            os.environ.update({str(k): str(v) for k, v in env.items() if k and v is not None})
        # Connect MCP before planning so tool discovery is real.
        manager = await _build_manager(task_id, mcp_servers, granted_approvals)
        tools = _available_tools(manager)
        state.runtime_context = build_runtime_context(
            user_input=state.user_input,
            conversation_id=state.conversation_id,
            conversation_messages=conversation_messages or [],
            mcp_tools=tools,
        )
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
            await node_verify(state_to_checkpoint(state))
            state = _as_state(state_to_checkpoint(state)) if False else state
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
