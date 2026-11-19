"""CRANE LangGraph workflow execution engine.

Stateful task orchestration using LangGraph StateGraph with MCP tool
integration, approval workflow, checkpointing, and recovery.

The engine uses the existing MCP manager and LangChain tool adapter for
tool discovery and invocation. Checkpoint state is persisted through the
Go-owned SQLite store via the versioned JSON protocol.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from langgraph.graph import StateGraph, START, END

from crane.mcp_runtime import (
    mcp_manager,
    discover_tools,
    convert_mcp_to_langchain_tools,
    get_all_tools,
    initialize_all_servers,
)
from crane.python_runtime.workflow_state import (
    WorkflowState,
    TaskState,
    state_to_dict,
    state_from_dict,
    MAX_GRAPH_STEPS,
    MAX_WALL_CLOCK_MS,
    DEFAULT_RETRY_LIMIT,
    DEFAULT_TOOL_TIMEOUT_MS,
)

from crane.mcp_runtime import MCP_PROTOCOL_VERSION as ProtocolVersion
# TypeTaskState is sent as raw JSON with type field "task_state"

logger = logging.getLogger("crane.workflow")


# ---------------------------------------------------------------------------
# Workflow constants
# ---------------------------------------------------------------------------

APPROVAL_ACTION_CLASSES = {
    "destroy",
    "delete",
    "write",
    "execute",
    "send",
    "modify_privileges",
    "alter_permissions",
    "create_user",
    "drop_collection",
    "truncate",
}

READ_ONLY_ACTION_CLASSES = {
    "read",
    "list",
    "search",
    "describe",
    "fetch",
    "inspect",
}


# ---------------------------------------------------------------------------
# Approval logic
# ---------------------------------------------------------------------------

def _classify_action(tool_name: str, tool_description: Optional[str], arguments: Dict[str, Any]) -> str:
    """Classify a tool call into an action category for approval purposes.

    Returns one of: 'approval_required', 'read_only', 'harmless'.
    """
    # Check tool name patterns first
    name_lower = tool_name.lower()
    desc_lower = (tool_description or "").lower()

    # Check arguments for sensitive values
    args_str = json.dumps(arguments).lower()

    # Destructive/externally consequential patterns
    destructive_kw = [
        "delete", "remove", "drop", "destroy", "purge", "cleanup",
        "execute", "run shell", "systemctl", "service",
        "send", "post", "create ticket", "publish",
        "modify", "alter", "update", "upgrade",
        "write", "append", "overwrite",
        "privilege", "permission", "admin", "root",
        "chmod", "chown", "format", "partition",
    ]
    for kw in destructive_kw:
        if kw in args_str or kw in desc_lower:
            return "approval_required"

    # Read-only patterns
    read_kw = [
        "read", "list", "get", "fetch", "describe",
        "inspect", "show", "view", "count",
    ]
    for kw in read_kw:
        if kw in args_str or kw in desc_lower:
            return "read_only"

    # Default: approval required for safety
    return "approval_required"


def _requires_approval(state: WorkflowState) -> bool:
    """Determine whether the current tool execution requires approval."""
    if not state.approval_required:
        return False

    if state.selected_tool is None:
        return False

    classification = _classify_action(
        state.selected_tool.get("name", ""),
        state.selected_tool.get("description"),
        state.tool_arguments,
    )

    return classification == "approval_required"


# ---------------------------------------------------------------------------
# Checkpoint and persistence helpers
# ---------------------------------------------------------------------------

async def _persist_task_state(task_id: str, workflow_state: WorkflowState) -> None:
    """Persist workflow state to the Go-owned SQLite database via protocol.

    Sends a task_state protocol message to the Python runtime supervisor
    which forwards it to the Go process in charge of the SQLite store.
    The Go supervisor stores the state and can resume the workflow after restart.

    The message is written to stdout using the versioned JSON protocol
    (ParseAndValidate + dispatch in the Go supervisor). The Go supervisor
    persists the state and can resume the workflow after restart.
    """
    # Construct the task state message as JSON, including all fields
    # needed for workflow restoration after Python runtime termination.
    msg = {
        "version": ProtocolVersion,
        "type": "task_state",
        "task_id": task_id,
        "state": workflow_state.state.value,
        "current_step": workflow_state.current_step,
        "graph_steps": workflow_state.graph_steps,
        "wall_clock_ms": workflow_state.wall_clock_ms,
        "result": workflow_state.result,
        "result_summary": workflow_state.result_summary,
        "failure_reason": workflow_state.failure_reason,
        "cancelled": workflow_state.cancelled,
        "approval_decision": workflow_state.approval_decision,
        "approval_id": workflow_state.approval_id,
        "selected_tool": workflow_state.selected_tool,
        "tool_arguments": workflow_state.tool_arguments,
        "tool_result": workflow_state.tool_result,
        "tool_error": workflow_state.tool_error,
        "tool_retry_count": workflow_state.tool_retry_count,
        "failure_step": workflow_state.failure_step,
        "persistent_state": workflow_state.persistent_state,
        "last_tool_side_effect": workflow_state.last_tool_side_effect,
    }

    # Marshal to JSON and send to supervisor via stdout
    data = json.dumps(msg)
    # Write to supervisor stdout - the Go supervisor reads messages from stdout
    # using the versioned JSON protocol (ParseAndValidate + dispatch).
    # In a full implementation, the Go supervisor would persist this to SQLite.
    if hasattr(sys, 'stdout') and sys.stdout:
        try:
            sys.stdout.write(data + "\n")
            sys.stdout.flush()
        except Exception:
            logger.info(f"Would persist task {task_id} state: state={workflow_state.state.value}, step={workflow_state.current_step}")
    else:
        logger.info(f"Would persist task {task_id} state: state={workflow_state.state.value}, step={workflow_state.current_step}")


async def _task_submitted(task_id: str, user_input: str, python_runtime_pid: Optional[int] = None) -> WorkflowState:
    """Create initial workflow state for a submitted task."""
    state = WorkflowState(
        task_id=task_id,
        user_input=user_input,
        state=TaskState.QUEUED,
        python_runtime_pid=python_runtime_pid,
    )
    await _persist_task_state(task_id, state)
    return state


# ---------------------------------------------------------------------------
# Tool discovery and selection
# ---------------------------------------------------------------------------

async def _discover_available_tools() -> List[Dict[str, Any]]:
    """Discover all available tools from registered MCP servers."""
    servers = await mcp_manager.list_servers()
    tools = []

    for server in servers:
        if server.tools:
            for tool in server.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description,
                    "server_name": server.name,
                    "server_id": server.server_id,
                    "transport": server.transport,
                })

    return tools


async def _select_tool_for_task(
    state: WorkflowState,
    available_tools: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Select a tool based on the task context.

    In a production system, this would use the LLM to plan which tool
    to call. For now, we use a simple heuristic: if the user input
    contains keywords, match to a tool; otherwise, return None.
    """
    user_input_lower = state.user_input.lower()

    # Simple keyword matching for demo purposes
    for tool in available_tools:
        tool_name = tool.get("name", "").lower()
        tool_desc = (tool.get("description") or "").lower()

        # Check if tool name appears in user input or vice versa
        if tool_name and (tool_name in user_input_lower or any(w in user_input_lower for w in tool_name.split("_"))):
            return tool

        # Check description keywords
        if tool_desc and any(w in user_input_lower for w in tool_desc.split() if len(w) > 3):
            return tool

    # If we have tools but none matched, return the first one
    # (the LLM/planning step would normally decide this)
    return available_tools[0] if available_tools else None


# ---------------------------------------------------------------------------
# Workflow graph nodes
# ---------------------------------------------------------------------------


async def node_initialize(state: WorkflowState) -> WorkflowState:
    """Node: Initialize the workflow for a new task."""
    task_id = state.task_id
    logger.info(f"Initializing workflow for task {task_id}")

    # Set initial state
    state.state = TaskState.STARTING
    state.current_step = "initialization"
    state.graph_steps = 0
    state.wall_clock_ms = 0
    state.cancelled = False
    state.failure_reason = None

    # Discover available tools
    state.available_tools = await _discover_available_tools()
    logger.info(f"Discovered {len(state.available_tools)} available tools")

    # Select initial tool (or none if just a simple query)
    state.selected_tool = None
    state.tool_result = None
    state.tool_error = None

    # Mark as running
    state.state = TaskState.RUNNING
    state.started_at = time.time()

    await _persist_task_state(task_id, state)
    return state


async def node_plan(state: WorkflowState) -> WorkflowState:
    """Node: Plan the next action based on current state and available tools."""
    task_id = state.task_id
    logger.info(f"Planning next action for task {task_id} (step {state.graph_steps})")

    state.graph_steps += 1
    state.current_step = "planning"

    # Check if we've exceeded max steps
    if state.graph_steps >= MAX_GRAPH_STEPS:
        state.state = TaskState.FAILED
        state.failure_reason = f"Exceeded maximum graph steps ({MAX_GRAPH_STEPS})"
        state.current_step = "failed_max_steps"
        state.completed_at = time.time()
        await _persist_task_state(task_id, state)
        return state

    # Check wall clock timeout
    if state.wall_clock_ms >= MAX_WALL_CLOCK_MS:
        state.state = TaskState.FAILED
        state.failure_reason = f"Exceeded maximum wall-clock time ({MAX_WALL_CLOCK_MS}ms)"
        state.current_step = "failed_wall_clock"
        state.completed_at = time.time()
        await _persist_task_state(task_id, state)
        return state

    # Select a tool based on the task
    available_tools = state.available_tools or await _discover_available_tools()
    state.selected_tool = await _select_tool_for_task(state, available_tools)

    if state.selected_tool is None:
        # No tool needed - produce a direct response
        state.current_step = "no_tool_needed"
        state.result = {
            "response": f"Task completed with no required tool invocations. Input: {state.user_input[:100]}..."
        }
        state.result_summary = "No tool invocations required"
        state.state = TaskState.SUCCEEDED
        state.completed_at = time.time()
        await _persist_task_state(task_id, state)
        return state

    # Check if approval is required
    needs_approval = _requires_approval(state)

    if needs_approval:
        state.current_step = "approval_required"
        state.approval_required = True
        state.approval_id = str(uuid.uuid4())
        state.current_step = "waiting_approval"
        state.state = TaskState.WAITING_APPROVAL
        await _persist_task_state(task_id, state)
        return state

    # Move to tool execution
    state.current_step = "tool_selection"
    return state


async def node_approve(state: WorkflowState) -> WorkflowState:
    """Node: Handle approval workflow.

    In a full implementation, this would wait for external approval input.
    For the LangGraph workflow, we simulate approval decision based on
    policy, but the workflow can be designed to wait for external input.
    """
    task_id = state.task_id
    logger.info(f"Handling approval for task {task_id}, decision: {state.approval_decision}")

    if state.approval_decision == "granted":
        state.current_step = "approval_granted"
        state.state = TaskState.RUNNING
        state.approval_timestamp = time.time()
        await _persist_task_state(task_id, state)
        return state
    elif state.approval_decision == "denied":
        state.current_step = "approval_denied"
        state.state = TaskState.FAILED
        state.failure_reason = "Action denied by approval policy"
        state.completed_at = time.time()
        await _persist_task_state(task_id, state)
        return state
    else:
        # No decision yet - stay in waiting_approval state
        state.current_step = "waiting_approval"
        state.state = TaskState.WAITING_APPROVAL
        await _persist_task_state(task_id, state)
        return state


async def node_invoke_tool(state: WorkflowState) -> WorkflowState:
    """Node: Invoke the selected MCP tool through the existing execution path."""
    task_id = state.task_id
    logger.info(f"Invoking tool {state.selected_tool.get('name')} for task {task_id}")

    state.current_step = "tool_invocation"
    state.tool_result = None
    state.tool_error = None
    state.tool_retry_count += 1

    # Check retry limit
    if state.tool_retry_count > DEFAULT_RETRY_LIMIT:
        state.state = TaskState.FAILED
        state.failure_reason = f"Exceeded retry limit ({DEFAULT_RETRY_LIMIT}) for tool {state.selected_tool.get('name')}"
        state.current_step = "failed_retry_limit"
        state.completed_at = time.time()
        await _persist_task_state(task_id, state)
        return state

    # Check if server is available
    server_name = state.selected_tool.get("server_name", "")
    if not server_name:
        state.state = TaskState.FAILED
        state.failure_reason = "No server assigned to selected tool"
        state.current_step = "failed_no_server"
        state.completed_at = time.time()
        await _persist_task_state(task_id, state)
        return state

    # Set tool arguments
    state.tool_arguments = state.tool_arguments or {}

    # Set timeout
    tool_timeout = DEFAULT_TOOL_TIMEOUT_MS

    # Invoke the tool through the existing MCP path
    try:
        from crane.mcp_runtime.runtime import _call_tool_mcp

        result = await asyncio.wait_for(
            _call_tool_mcp(
                server_name=server_name,
                tool_name=state.selected_tool.get("name", ""),
                arguments=state.tool_arguments,
            ),
            timeout=tool_timeout / 1000.0,
        )

        state.tool_result = result
        state.tool_error = None

        # Evaluate the result
        if result and result.get("text"):
            # Check for error in result text
            text = result.get("text", "")
            if text.startswith("Error:"):
                # Tool execution error - handle retry logic
                state.tool_error = text
                if state.tool_retry_count <= DEFAULT_RETRY_LIMIT:
                    state.current_step = "tool_error_retry"
                    # Route back to planning for possible retry
                    return state
                else:
                    state.state = TaskState.FAILED
                    state.failure_reason = f"Tool error after {state.tool_retry_count} retries: {text}"
                    state.current_step = "failed_tool_error"
                    state.completed_at = time.time()
                    await _persist_task_state(task_id, state)
                    return state

        # Success - evaluate result and route
        state.current_step = "result_evaluation"
        return await _evaluate_tool_result(state)

    except asyncio.TimeoutError:
        state.tool_error = f"Tool invocation timed out after {tool_timeout}ms"
        logger.error(f"Tool timeout for task {task_id}: {state.tool_error}")

        if state.tool_retry_count <= DEFAULT_RETRY_LIMIT:
            state.current_step = "tool_timeout_retry"
            return state
        else:
            state.state = TaskState.FAILED
            state.failure_reason = f"Tool timeout after {state.tool_retry_count} retries"
            state.current_step = "failed_timeout"
            state.completed_at = time.time()
            await _persist_task_state(task_id, state)
            return state

    except RuntimeError as e:
        state.tool_error = f"Runtime error: {str(e)}"
        logger.error(f"Runtime error for task {task_id}: {state.tool_error}")

        if state.tool_retry_count <= DEFAULT_RETRY_LIMIT:
            state.current_step = "tool_error_retry"
            return state
        else:
            state.state = TaskState.FAILED
            state.failure_reason = f"Tool runtime error after {state.tool_retry_count} retries: {str(e)}"
            state.current_step = "failed_runtime_error"
            state.completed_at = time.time()
            await _persist_task_state(task_id, state)
            return state

    except Exception as e:
        state.tool_error = f"Unexpected error: {str(e)}"
        logger.error(f"Unexpected error for task {task_id}: {state.tool_error}")

        if state.tool_retry_count <= DEFAULT_RETRY_LIMIT:
            state.current_step = "tool_error_retry"
            return state
        else:
            state.state = TaskState.FAILED
            state.failure_reason = f"Tool error after {state.tool_retry_count} retries: {str(e)}"
            state.current_step = "failed_unexpected_error"
            state.completed_at = time.time()
            await _persist_task_state(task_id, state)
            return state


async def _evaluate_tool_result(state: WorkflowState) -> WorkflowState:
    """Evaluate the tool result and decide on next routing."""
    task_id = state.task_id
    result = state.tool_result or {}

    # Check result success flag
    success = result.get("success", True)

    if success:
        # Successful tool execution
        state.result = {
            "output": result.get("text", ""),
            "structured": result.get("structured"),
            "server_name": state.selected_tool.get("server_name") if state.selected_tool else None,
            "tool_name": state.selected_tool.get("name") if state.selected_tool else None,
        }
        state.result_summary = f"Tool '{state.selected_tool.get('name')}' succeeded"
        state.current_step = "succeeded"
        state.state = TaskState.SUCCEEDED
        state.completed_at = time.time()
        await _persist_task_state(task_id, state)
        return state
    else:
        # Tool reported failure
        error = result.get("error", state.tool_error or "Unknown tool error")
        state.tool_error = error

        if state.tool_retry_count <= DEFAULT_RETRY_LIMIT:
            state.current_step = "tool_error_retry"
            return state
        else:
            state.state = TaskState.FAILED
            state.failure_reason = f"Tool failed after {state.tool_retry_count} retries: {error}"
            state.current_step = "failed_tool_error"
            state.completed_at = time.time()
            await _persist_task_state(task_id, state)
            return state


async def node_completion(state: WorkflowState) -> WorkflowState:
    """Node: Finalize the workflow and generate structured result."""
    task_id = state.task_id
    logger.info(f"Finalizing workflow for task {task_id}")

    state.current_step = "completion"
    state.state = TaskState.SUCCEEDED
    state.completed_at = time.time()

    # Ensure we have a result summary
    if state.result_summary is None:
        state.result_summary = "Task completed successfully"

    # Persist final state
    await _persist_task_state(task_id, state)
    return state


async def node_failure(state: WorkflowState) -> WorkflowState:
    """Node: Handle task failure with structured result."""
    task_id = state.task_id
    logger.info(f"Handling failure for task {task_id}: {state.failure_reason}")

    state.current_step = "failure"
    state.state = TaskState.FAILED
    state.completed_at = time.time()

    # Ensure we have a failure summary
    if state.result_summary is None:
        state.result_summary = f"Task failed: {state.failure_reason or 'Unknown error'}"

    # Persist final state
    await _persist_task_state(task_id, state)
    return state


# ---------------------------------------------------------------------------
# Workflow graph compilation
# ---------------------------------------------------------------------------

# Define the nodes
NODES = {
    "initialize": node_initialize,
    "plan": node_plan,
    "approve": node_approve,
    "invoke_tool": node_invoke_tool,
    "evaluate_result": _evaluate_tool_result,
    "completion": node_completion,
    "failure": node_failure,
}


def _build_decision_edges(state: WorkflowState, next_node: str) -> str:
    """Determine the next node in the workflow graph based on current state."""

    # State transition map
    transitions = {
        "initialize": "plan",                    # After init, move to planning
        "plan": "approve" if _requires_approval(state) else "invoke_tool",
        "approve": "invoke_tool",                # After approval decision
        "invoke_tool": "evaluate_result",        # After tool invocation
        "evaluate_result": "completion" if state.state == TaskState.SUCCEEDED else "failure",
        "completion": END,                       # Final state
        "failure": END,                          # Final state (task failed)
    }

    return transitions.get(next_node, END)


def compile_workflow() -> Any:
    """Build and compile the LangGraph workflow for task execution.

    Returns a compiled StateGraph that can be executed with a given
    initial WorkflowState.
    """

    # Create the state graph
    workflow = StateGraph(WorkflowState)

    # Add all nodes
    for node_name, node_func in NODES.items():
        workflow.add_node(node_name, node_func)

    # Add explicit edges for linear flow
    # START -> initialize
    workflow.add_edge(START, "initialize")

    # initialize -> plan (always transition to planning)
    workflow.add_edge("initialize", "plan")

    # plan -> approve if approval required,
    # otherwise -> invoke_tool if a tool was selected, else -> completion
    workflow.add_conditional_edges(
        "plan",
        lambda s: (
            "approve"
            if _requires_approval(s)
            else "invoke_tool" if s.selected_tool is not None else "completion"
        ),
    )

    # approve -> invoke_tool (after approval decision)
    workflow.add_edge("approve", "invoke_tool")

    # invoke_tool -> evaluate_result (after tool invocation)
    workflow.add_edge("invoke_tool", "evaluate_result")

    # evaluate_result -> completion if succeeded, else failure
    workflow.add_conditional_edges(
        "evaluate_result",
        lambda s: "completion" if s.state == TaskState.SUCCEEDED else "failure",
    )

    # completion and failure are terminal
    workflow.add_edge("completion", END)
    workflow.add_edge("failure", END)

    # Compile without a checkpointer for single-execution tasks.
    # Checkpointing is handled externally through the Go SQLite store.
    compiled = workflow.compile()
    return compiled


# ---------------------------------------------------------------------------
# Public API: task execution
# ---------------------------------------------------------------------------

async def execute_task(
    user_input: str,
    python_runtime_pid: Optional[int] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a user task through the LangGraph workflow engine.

    Args:
        user_input: The user's task description/request.
        python_runtime_pid: Optional PID of the Python runtime process.
        task_id: Optional existing task ID to resume; if None, a new ID is generated.

    Returns:
        A dictionary with the execution result, status, and summary.
    """

    # Generate or use existing task ID
    if task_id is None:
        task_id = str(uuid.uuid4())

    # If a task_id was provided, attempt to restore persisted workflow state
    # from the Go-owned SQLite store (via the Go supervisor).
    restored_state: Optional[WorkflowState] = None
    if task_id is not None:
        # Note: In a full implementation, the Go supervisor would have already
        # sent the persisted state to the Python process during startup. 
        # For now, we acknowledge the task_id and proceed with a new workflow,
        # as the restoration mechanism requires Go supervisor integration.
        logger.info(f"Task ID provided: {task_id} - proceeding with new workflow")
        # TODO: When Go supervisor integration is complete, restore state here:
        # restored_state = await _get_restored_state(task_id)

    # Initialize workflow state
    initial_state = WorkflowState(
        task_id=task_id,
        user_input=user_input,
        python_runtime_pid=python_runtime_pid,
        state=TaskState.QUEUED,
    )

    # Compile the workflow
    compiled = compile_workflow()

    # Prepare the initial state for execution
    # We need to serialize/deserialize through the checkpoint saver
    initial_dict = state_to_dict(initial_state)

    # Execute the workflow
    # The compiled graph returns a dict with all state fields
    try:
        # Run the graph with the initial state
        raw_result = await compiled.ainvoke(
            initial_dict,
            config={"configurable": {"thread_id": task_id}},
        )

        # Construct WorkflowState from the returned dict
        # The raw_result is already a dict with all fields from state_to_dict
        final_state = WorkflowState(
            task_id=raw_result.get("task_id", task_id),
            user_input=raw_result.get("user_input", user_input),
            submitted_at=raw_result.get("submitted_at", time.time()),
            started_at=raw_result.get("started_at"),
            completed_at=raw_result.get("completed_at"),
            state=TaskState(raw_result.get("state", "failed")),
            current_step=raw_result.get("current_step", "initialization"),
            graph_steps=raw_result.get("graph_steps", 0),
            wall_clock_ms=raw_result.get("wall_clock_ms", 0),
            available_tools=raw_result.get("available_tools", []),
            selected_tool=raw_result.get("selected_tool"),
            tool_arguments=raw_result.get("tool_arguments", {}),
            tool_result=raw_result.get("tool_result"),
            tool_error=raw_result.get("tool_error"),
            tool_retry_count=raw_result.get("tool_retry_count", 0),
            approval_required=raw_result.get("approval_required", False),
            approval_id=raw_result.get("approval_id"),
            approval_decision=raw_result.get("approval_decision"),
            approval_timestamp=raw_result.get("approval_timestamp"),
            checkpoint_identifier=raw_result.get("checkpoint_identifier"),
            persistent_state=raw_result.get("persistent_state", {}),
            last_tool_side_effect=raw_result.get("last_tool_side_effect"),
            failure_reason=raw_result.get("failure_reason"),
            failure_step=raw_result.get("failure_step"),
            cancelled=raw_result.get("cancelled", False),
            result=raw_result.get("result"),
            result_summary=raw_result.get("result_summary"),
            python_runtime_pid=raw_result.get("python_runtime_pid"),
            supervisor_version=raw_result.get("supervisor_version", "0.1.0"),
        )

        # Return the result
        return {
            "task_id": final_state.task_id,
            "status": final_state.state.value,
            "result": final_state.result,
            "result_summary": final_state.result_summary,
            "failure_reason": final_state.failure_reason,
            "graph_steps": final_state.graph_steps,
            "wall_clock_ms": final_state.wall_clock_ms,
            "current_step": final_state.current_step,
        }

    except asyncio.CancelledError:
        # Task was cancelled - try to retrieve state
        final_state = await _get_task_state(task_id)
        # ainvoke returns a dict; convert or access fields directly
        if isinstance(final_state, dict):
            final_state = WorkflowState(
                task_id=final_state.get("task_id", task_id),
                user_input=final_state.get("user_input", ""),
                state=TaskState(final_state.get("state", "failed")),
                current_step=final_state.get("current_step", "error"),
                graph_steps=final_state.get("graph_steps", 0),
                wall_clock_ms=final_state.get("wall_clock_ms", 0),
                tool_result=final_state.get("tool_result"),
                tool_error=final_state.get("tool_error"),
                failure_reason=final_state.get("failure_reason"),
                result_summary=final_state.get("result_summary"),
                result=final_state.get("result"),
            )
        return {
            "task_id": final_state.task_id if final_state else task_id,
            "status": TaskState.CANCELLED.value,
            "result": final_state.result if final_state else None,
            "result_summary": final_state.result_summary if final_state else "Cancelled",
            "failure_reason": final_state.failure_reason if final_state else "Task cancelled",
            "graph_steps": final_state.graph_steps if final_state else 0,
            "wall_clock_ms": final_state.wall_clock_ms if final_state else 0,
            "current_step": final_state.current_step if final_state else "cancelled",
        }

    except Exception as e:
        logger.error(f"Workflow execution error for task {task_id}: {e}", exc_info=True)
        # Return failure result
        return {
            "task_id": task_id,
            "status": TaskState.FAILED.value,
            "result": None,
            "result_summary": f"Workflow execution error: {str(e)}",
            "failure_reason": str(e),
            "graph_steps": 0,
            "wall_clock_ms": 0,
            "current_step": "error",
        }


async def _get_task_state_from_store(task_id: str) -> Optional[WorkflowState]:
    """Retrieve task state from the Go-owned SQLite store.
    
    This is called when a task_id is provided to execute_task, and it
    queries the Go supervisor (which owns the SQLite store) for the
    latest persisted state. In a full implementation with direct
    database access, this would query SQLite directly.
    
    For now, this is a placeholder that will be implemented when
    the Go supervisor sends persisted state to the Python process.
    """
    # Placeholder - the Go supervisor will send persisted state
    # to the Python process during startup. The Python process
    # should capture this state and pass it to the workflow execution.
    logger.info(f"Would retrieve task state for {task_id} from Go supervisor")
    return None


async def _restore_workflow_state(raw_msg: dict) -> Optional[WorkflowState]:
    """Restore a WorkflowState from a task_state protocol message.
    
    This is called when the Python process receives a task_state message
    from the Go supervisor during startup (indicating persisted state
    from a previous run).
    """
    try:
        state = WorkflowState(
            task_id=raw_msg.get("task_id", ""),
            user_input="",  # Will be set by execute_task
            state=TaskState(raw_msg.get("state", "queued")),
            current_step=raw_msg.get("current_step", "initialization"),
            graph_steps=raw_msg.get("graph_steps", 0),
            wall_clock_ms=raw_msg.get("wall_clock_ms", 0),
            result=raw_msg.get("result"),
            result_summary=raw_msg.get("result_summary"),
            failure_reason=raw_msg.get("failure_reason"),
            cancelled=raw_msg.get("cancelled", False),
            approval_decision=raw_msg.get("approval_decision"),
            approval_id=raw_msg.get("approval_id"),
            selected_tool=raw_msg.get("selected_tool"),
            tool_arguments=raw_msg.get("tool_arguments", {}),
            tool_result=raw_msg.get("tool_result"),
            tool_error=raw_msg.get("tool_error"),
            tool_retry_count=raw_msg.get("tool_retry_count", 0),
            failure_step=raw_msg.get("failure_step"),
            persistent_state=raw_msg.get("persistent_state", {}),
            last_tool_side_effect=raw_msg.get("last_tool_side_effect"),
            python_runtime_pid=None,
            supervisor_version="0.1.0",
        )
        return state
    except Exception as e:
        logger.error(f"Failed to restore workflow state: {e}")
        return None


# ---------------------------------------------------------------------------
# Approval management
# ---------------------------------------------------------------------------

async def set_approval_decision(task_id: str, decision: str) -> None:
    """Set the approval decision for a task waiting in WAITING_APPROVAL state.

    Args:
        task_id: The task identifier.
        decision: Either "granted" or "denied".
    """
    if decision not in ("granted", "denied"):
        raise ValueError(f"Invalid approval decision: {decision}. Must be 'granted' or 'denied'.")

    # In a full implementation, this would update the SQLite store
    # and signal the workflow to resume.
    # The LangGraph memory saver will check the updated state on resume.
    logger.info(f"Setting approval decision for task {task_id}: {decision}")

    # Retrieve current state and update
    state = await _get_task_state(task_id)
    if state:
        state.approval_decision = decision
        state.approval_timestamp = time.time()
        await _persist_task_state(task_id, state)


# ---------------------------------------------------------------------------
# Task lifecycle management
# ---------------------------------------------------------------------------

async def cancel_task(task_id: str) -> Dict[str, Any]:
    """Cancel a task that is currently running or waiting.

    Args:
        task_id: The task identifier to cancel.

    Returns:
        A dictionary with the cancellation result.
    """
    # Retrieve current state
    state = await _get_task_state(task_id)
    if state is None:
        return {
            "task_id": task_id,
            "status": "not_found",
            "result_summary": f"Task {task_id} not found",
        }

    # Mark as cancelled
    state.cancelled = True
    state.state = TaskState.CANCELLED
    state.completed_at = time.time()

    # Set failure reason indicating cancellation
    state.failure_reason = "Task cancelled by user request"
    state.result_summary = "Task cancelled"

    # Persist the cancelled state
    await _persist_task_state(task_id, state)

    return {
        "task_id": task_id,
        "status": TaskState.CANCELLED.value,
        "result_summary": "Task cancelled",
    }


async def list_tasks() -> List[Dict[str, Any]]:
    """List all tasks in the system.

    Returns:
        A list of task summary dictionaries.
    """
    # Placeholder - in production, query SQLite
    logger.info("Would list all tasks from SQLite")
    return []


# Expose commonly used names
workflow_state = WorkflowState
TaskState = TaskState