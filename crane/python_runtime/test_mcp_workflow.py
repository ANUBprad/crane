"""Real MCP workflow tests without a hosted model or fake tool execution."""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import crane.mcp_runtime as mcp_runtime
from crane.mcp_runtime import MCPManager, MCPServerConfig, action_fingerprint
from crane.python_runtime import protocol, workflow_engine
from crane.python_runtime.workflow_state import PlanStep, WorkflowState

ROOT = Path(__file__).resolve().parents[2]
TEST_SERVER = ROOT / "crane" / "mcp_runtime" / "test_server.py"
OLLAMA_ENV = {"CRANE_MODEL_PROVIDER": "ollama"}


def stdio_config(server_id: str = "workflow-test") -> MCPServerConfig:
    return MCPServerConfig.from_dict({
        "server_id": server_id,
        "name": "Workflow Test Server",
        "transport": "stdio",
        "command": "python",
        "args": [str(TEST_SERVER)],
        "enabled": True,
        "connect_timeout_seconds": 10,
        "invocation_timeout_seconds": 20,
    })


class MCPWorkflowTest(unittest.TestCase):
    def test_stdio_tool_discovery_policy_and_real_invocation(self) -> None:
        async def scenario() -> None:
            manager = MCPManager()
            config = stdio_config()
            manager.add_config(config)
            try:
                tools_by_server = await mcp_runtime.connect_all(manager)
                self.assertEqual(tools_by_server[config.server_id], True)
                self.assertEqual(
                    sorted(tool.name for tool in manager.server_tools(config.server_id).values()),
                    [
                        "delete_test_record",
                        "get_invocation_counts",
                        "get_test_echo",
                        "get_tool_probe_status",
                        "tool_probe",
                    ],
                )
                tools = manager.langchain_tools()
                self.assertEqual(sorted(tool.name for tool in tools), [
                    "workflow_test_delete_test_record",
                    "workflow_test_get_invocation_counts",
                    "workflow_test_get_test_echo",
                    "workflow_test_get_tool_probe_status",
                    "workflow_test_tool_probe",
                ])

                echo_tool = next(tool for tool in tools if tool.name.endswith("get_test_echo"))
                output = await echo_tool.ainvoke({"value": "langchain-mcp"})
                self.assertIn("langchain-mcp", str(output))

                delete_tool = next(tool for tool in tools if tool.name.endswith("delete_test_record"))
                with self.assertRaises(Exception) as blocked:
                    await delete_tool.ainvoke({"record_id": "r1"})
                self.assertIn("approval", str(blocked.exception).lower())
                record = manager.invocations()[-1]
                self.assertEqual(record["status"], "blocked")
                self.assertEqual(record["policy_decision"], "approval_required")
            finally:
                await mcp_runtime.shutdown_all(manager)

        asyncio.run(scenario())

    def test_http_tool_discovery_and_real_invocation(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        url = f"http://127.0.0.1:{port}/mcp"
        stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        server = subprocess.Popen(
            [sys.executable, str(TEST_SERVER), "--http", "--port", str(port)],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
        )
        try:
            deadline = time.time() + 15
            ready = False
            while time.time() < deadline:
                with contextlib.suppress(OSError):
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        ready = True
                        break
                time.sleep(0.2)
            if not ready:
                stderr.seek(0)
                self.fail(f"HTTP MCP server did not start: {stderr.read()}")

            async def scenario() -> None:
                manager = MCPManager()
                config = MCPServerConfig.from_dict({
                    "server_id": "workflow-http", "name": "Workflow HTTP",
                    "transport": "streamable_http", "url": url, "enabled": True,
                    "connect_timeout_seconds": 10, "invocation_timeout_seconds": 20,
                })
                manager.add_config(config)
                try:
                    self.assertTrue((await mcp_runtime.connect_all(manager))[config.server_id])
                    tools = manager.langchain_tools()
                    echo_tool = next(tool for tool in tools if tool.name.endswith("get_test_echo"))
                    output = await echo_tool.ainvoke({"value": "http-workflow"})
                    self.assertIn("http-workflow", str(output))
                finally:
                    await mcp_runtime.shutdown_all(manager)

            asyncio.run(scenario())
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            stderr.close()

    def _run_tool_task(
        self,
        task_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        fingerprints: list,
    ) -> tuple[Dict[str, Any], list]:
        tool_results: list = []

        def write_stdout(message: Dict[str, Any]) -> None:
            decoded = protocol.decode(protocol.encode(message))
            if decoded["type"] == protocol.TYPE_TOOL_RESULT:
                tool_results.append(decoded)

        async def fake_tool_model(user_input: str, tools: list, env=None, system_prompt: str = "", messages=None, **kwargs: Any) -> Dict[str, Any]:
            tool = next(item for item in tools if item.name.endswith(tool_name))
            try:
                output = await tool.ainvoke(arguments)
                model_output = str(output)
            except Exception:
                model_output = "MCP tool call blocked or failed"
            return {
                "execution_mode": "test_tool_model",
                "provider": "test",
                "model": "none",
                "model_output": model_output,
                "notice": None,
            }

        async def forbidden_fallback(user_input: str, env=None, system_prompt: str = "", messages=None, context=None) -> Dict[str, Any]:
            self.fail("non-tool provider path executed with MCP tools configured")

        with mock.patch.dict(os.environ, OLLAMA_ENV), \
             mock.patch.object(protocol, "write_stdout", side_effect=write_stdout), \
             mock.patch.object(workflow_engine, "prepare_provider"), \
             mock.patch.object(workflow_engine, "warm_provider"), \
             mock.patch.object(workflow_engine, "invoke_with_tools", side_effect=fake_tool_model), \
             mock.patch.object(workflow_engine, "invoke_provider", side_effect=forbidden_fallback):
            outcome = asyncio.run(workflow_engine.execute_task(
                f"use {tool_name}", task_id=task_id, mcp_servers=[stdio_config().to_dict()],
                granted_approvals=fingerprints,
            ))
        return outcome, tool_results

    def test_task_submission_context_reaches_real_tool_and_is_persisted(self) -> None:
        outcome, tool_results = self._run_tool_task(
            "mcp-task-echo", "get_test_echo", {"value": "e2e"}, [],
        )
        self.assertEqual(outcome["status"], "succeeded")
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_results[0]["tool_name"], "get_test_echo")
        self.assertEqual(tool_results[0]["status"], "succeeded")
        self.assertNotIn("CRANE_MCP", protocol_json(tool_results[0]))

    def test_consequential_tool_is_blocked_without_durable_grant(self) -> None:
        outcome, tool_results = self._run_tool_task(
            "mcp-task-blocked", "delete_test_record", {"record_id": "r1"}, [],
        )
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["result"]["execution_status"], "waiting_for_approval")
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_results[0]["status"], "blocked")
        self.assertEqual(tool_results[0]["policy_decision"], "approval_required")

    def test_durable_grant_allows_exact_consequential_tool(self) -> None:
        arguments = {"record_id": "r-granted"}
        fingerprint = action_fingerprint("mcp-task-granted", "workflow-test", "delete_test_record", arguments)
        outcome, tool_results = self._run_tool_task(
            "mcp-task-granted", "delete_test_record", arguments, [fingerprint],
        )
        self.assertEqual(outcome["status"], "succeeded")
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_results[0]["status"], "succeeded")
        self.assertIn("r-granted", protocol_json(tool_results[0]))


class _InvocationManager:
    def __init__(self, invocations: list) -> None:
        self._invocations = invocations

    def invocations(self) -> list:
        return self._invocations


class ToolEvidenceTests(unittest.TestCase):
    @staticmethod
    def _state_with_step() -> tuple[WorkflowState, PlanStep]:
        step = PlanStep(
            step_id="respond",
            description="Use the probe tool and report its result.",
            expected_outcome="The probe tool result is reported.",
        )
        state = WorkflowState(task_id="evidence-task", user_input="probe", objective="probe")
        state.plan = [step]
        state.current_step_id = step.step_id
        return state, step

    def test_provider_observation_does_not_duplicate_authoritative_mcp_record(self) -> None:
        result_text = '{"probe":"TOOL_CALL_OK","invoked":true}'
        authoritative = {
            "invocation_id": "inv-1",
            "server_id": "probe",
            "tool_name": "tool_probe",
            "arguments": {"value": "TOOL_CALL_OK"},
            "status": "succeeded",
            "result": {"text": result_text, "structured": None},
            "error": None,
            "duration_ms": 5,
        }
        provider_output = {
            "execution_mode": "langchain_ollama_mcp",
            "provider": "ollama",
            "model_output": "The probe succeeded.",
            "tool_calls": [{
                "name": "probe_tool_probe",
                "tool_call_id": "call-1",
                "arguments": {"value": "TOOL_CALL_OK"},
                "result": result_text,
                "status": "succeeded",
            }],
        }
        state, step = self._state_with_step()
        workflow_engine._merge_tool_call_records(
            state, step.step_id, provider_output, _InvocationManager([authoritative])
        )

        self.assertEqual(len(state.tool_calls), 1)
        self.assertEqual(len(state.tool_outputs), 1)
        self.assertEqual(state.tool_calls[0].invocation_id, "inv-1")
        self.assertEqual(state.tool_calls[0].server_id, "probe")
        self.assertEqual(state.tool_calls[0].status, "succeeded")

    def test_failed_and_blocked_observations_are_not_evaluated_as_success(self) -> None:
        state, step = self._state_with_step()
        failed_output = {
            "execution_mode": "langchain_ollama_mcp",
            "provider": "ollama",
            "model_output": "I tried to call the tool.",
            "tool_calls": [{
                "name": "missing_tool",
                "tool_call_id": "call-1",
                "arguments": {},
                "result": "MCP tool 'missing_tool' is not available",
                "status": "failed",
                "error": "MCP tool 'missing_tool' is not available",
            }],
        }
        workflow_engine._merge_tool_call_records(state, step.step_id, failed_output, _InvocationManager([]))
        failed_evaluation = workflow_engine._evaluation_from_output(state, step, failed_output)

        self.assertEqual(state.tool_calls[0].status, "failed")
        self.assertEqual(failed_evaluation.outcome, "failed")
        self.assertIn("did not complete", failed_evaluation.summary)

        blocked_state, blocked_step = self._state_with_step()
        blocked_output = {
            "execution_mode": "langchain_ollama_mcp",
            "provider": "ollama",
            "model_output": "Approval is required.",
            "tool_calls": [{
                "name": "probe_delete_test_record",
                "tool_call_id": "call-2",
                "arguments": {"record_id": "r1"},
                "result": "MCP tool failed: This tool requires explicit approval",
                "status": "blocked",
                "error": "approval_required",
            }],
        }
        workflow_engine._merge_tool_call_records(
            blocked_state, blocked_step.step_id, blocked_output, _InvocationManager([])
        )
        blocked_evaluation = workflow_engine._evaluation_from_output(blocked_state, blocked_step, blocked_output)

        self.assertEqual(blocked_state.tool_calls[0].status, "blocked")
        self.assertEqual(blocked_evaluation.outcome, "needs_approval")


def protocol_json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
