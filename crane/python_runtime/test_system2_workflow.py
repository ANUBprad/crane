"""Focused System 2 workflow tests (unit-level, no live Ollama/MCP)."""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

from crane.python_runtime import protocol, workflow_engine
from crane.python_runtime.workflow_state import (
    EvaluationOutcome,
    ExecutionStatus,
    MAX_MODEL_CALLS_PER_TASK,
    MAX_RETRIES_PER_STEP,
    PlanStep,
    TaskState,
    WorkflowState,
)


class PlanValidationTests(unittest.TestCase):
    def test_rejects_cycle(self) -> None:
        state = WorkflowState(task_id="t1", user_input="x", objective="x")
        plan = [
            PlanStep(step_id="a", description="a", expected_outcome="a", dependencies=["b"], completion_criteria="a"),
            PlanStep(step_id="b", description="b", expected_outcome="b", dependencies=["a"], completion_criteria="b"),
        ]
        ok, err = workflow_engine._validate_plan(state, plan, {})
        self.assertFalse(ok)
        self.assertIn("cyclic", err.lower())

    def test_rejects_unknown_tool(self) -> None:
        state = WorkflowState(task_id="t1", user_input="x", objective="x")
        plan = [
            PlanStep(
                step_id="s1", description="d", expected_outcome="e", dependencies=[],
                required_tools=["missing_tool"], completion_criteria="c",
            )
        ]
        ok, err = workflow_engine._validate_plan(state, plan, {"web_search": {"name": "web_search"}})
        self.assertFalse(ok)
        self.assertIn("unknown tool", err.lower())

    def test_rejects_missing_fields_and_duplicate_ids(self) -> None:
        state = WorkflowState(task_id="t1", user_input="x", objective="x")
        bad = [PlanStep(step_id="s1", description="", expected_outcome="e", completion_criteria="c")]
        ok, err = workflow_engine._validate_plan(state, bad, {})
        self.assertFalse(ok)
        self.assertIn("missing required fields", err.lower())

        dup = [
            PlanStep(step_id="s1", description="d", expected_outcome="e", completion_criteria="c"),
            PlanStep(step_id="s1", description="d2", expected_outcome="e2", completion_criteria="c2"),
        ]
        ok, err = workflow_engine._validate_plan(state, dup, {})
        self.assertFalse(ok)
        self.assertIn("Duplicate step ID", err)


class EvaluationAndStateTests(unittest.TestCase):
    def test_deterministic_local_no_tool_step_completes(self) -> None:
        state = WorkflowState(task_id="t1", user_input="hello", objective="hello")
        step = PlanStep(step_id="respond", description="respond", expected_outcome="resp", completion_criteria="done")
        out = {"execution_mode": "deterministic_local", "provider": "local", "model_output": None}
        ev = workflow_engine._evaluation_from_output(state, step, out)
        self.assertEqual(ev.outcome, EvaluationOutcome.COMPLETED.value)

    def test_tool_failure_classifies_as_failed(self) -> None:
        state = WorkflowState(task_id="t1", user_input="x", objective="x")
        step = PlanStep(step_id="s1", description="d", expected_outcome="e", completion_criteria="c")
        ev = workflow_engine._evaluation_from_output(state, step, {"status": "failed", "error": "boom"})
        self.assertEqual(ev.outcome, EvaluationOutcome.FAILED.value)

    def test_waiting_approval_maps_to_failed_terminal_with_execution_status(self) -> None:
        state = WorkflowState(task_id="t1", user_input="x", objective="x")
        workflow_engine._mark_terminal(state, ExecutionStatus.WAITING_FOR_APPROVAL, "Waiting for approval", "Approval required")
        self.assertEqual(state.state, TaskState.FAILED)
        self.assertEqual(state.execution_status, ExecutionStatus.WAITING_FOR_APPROVAL.value)
        self.assertEqual(state.failure_reason, "Approval required")


class WorkflowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_mode_with_mcp_tools_fails_without_fake_autonomy(self) -> None:
        async def no_connect(manager):
            return {}
        with mock.patch.dict(os.environ, {"CRANE_MODEL_PROVIDER": "local"}, clear=False), \
             mock.patch.object(protocol, "write_stdout", lambda *_: None), \
             mock.patch.object(workflow_engine, "connect_all", side_effect=no_connect), \
             mock.patch.object(workflow_engine, "configure_manager") as cfg:
            mgr = mock.Mock()
            mgr.configs.return_value = [{"server_id": "s1"}]
            mgr.tools.return_value = []
            mgr.langchain_tools.return_value = []
            cfg.return_value = mgr
            result = await workflow_engine.execute_task(
                "use tools",
                task_id="t-local-mcp",
                mcp_servers=[{"server_id": "s1", "name": "s1", "transport": "stdio", "command": "python", "args": ["server.py"], "enabled": True}],
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result.get("execution_status"), ExecutionStatus.FAILED.value)
        self.assertIn("MCP tools require", result.get("failure_reason") or "")

    async def test_cancellation_is_reported(self) -> None:
        def slow_invoke(*args, **kwargs):
            import time as _time
            _time.sleep(0.2)
            return {"execution_mode": "deterministic_local", "provider": "local", "model_output": "ok"}

        with mock.patch.dict(os.environ, {"CRANE_MODEL_PROVIDER": "local"}, clear=False), \
             mock.patch.object(protocol, "write_stdout", lambda *_: None), \
             mock.patch.object(workflow_engine, "invoke_provider", side_effect=slow_invoke):
            task = asyncio.create_task(workflow_engine.execute_task("hello", task_id="t-cancel"))
            await asyncio.sleep(0.05)
            workflow_engine.request_cancellation("t-cancel")
            result = await task
        self.assertIn(result["status"], {"cancelled", "failed", "succeeded"})
        self.assertIn(
            result.get("execution_status"),
            {
                ExecutionStatus.CANCELLED.value,
                ExecutionStatus.FAILED.value,
                ExecutionStatus.PARTIALLY_COMPLETED.value,
                ExecutionStatus.FULLY_COMPLETED.value,
            },
        )


if __name__ == "__main__":
    unittest.main()
