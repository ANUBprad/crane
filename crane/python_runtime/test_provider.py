"""Focused tests for the explicit Ollama provider and LangGraph boundary.

Mocked tests validate CRANE's integration contract; they are not proof that a
real Ollama model answered a task.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import types
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch
from urllib.error import URLError

from langchain_core.messages import AIMessage, ToolMessage

from crane.python_runtime import provider, workflow_engine


OLLAMA_ENV = {
    "CRANE_MODEL_PROVIDER": "ollama",
    "CRANE_MODEL": "crane-test:latest",
    "OLLAMA_HOST": "http://127.0.0.1:11434",
}


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeChatOllama:
    calls: List[Dict[str, Any]] = []
    answer = "Mocked Ollama answer"
    failure: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        type(self).calls.append(kwargs)

    def invoke(self, prompt: str) -> FakeResponse:
        if type(self).failure is not None:
            raise type(self).failure
        return FakeResponse(type(self).answer)


class FakeHTTPResponse:
    def __init__(self, payload: Dict[str, Any], status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


def tags_response(*names: str) -> FakeHTTPResponse:
    return FakeHTTPResponse({"models": [{"name": name} for name in names]})


def fake_chat_module(chat_class: Any = FakeChatOllama) -> Dict[str, Any]:
    module = types.ModuleType("langchain_ollama")
    module.ChatOllama = chat_class
    return {"langchain_ollama": module}


def reset_fake_chat() -> None:
    FakeChatOllama.calls = []
    FakeChatOllama.answer = "Mocked Ollama answer"
    FakeChatOllama.failure = None


class FakeTool:
    def __init__(self, name: str, result: Any = None, error: Optional[Exception] = None) -> None:
        self.name = name
        self.result = {"ok": True} if result is None else result
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    async def ainvoke(self, arguments: Dict[str, Any]) -> Any:
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        return self.result


class FakeToolResponse:
    def __init__(
        self,
        content: str = "",
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        additional_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.additional_kwargs = additional_kwargs or {}


class FakeBoundChat:
    def __init__(self, responses: List[FakeToolResponse]) -> None:
        self.responses = list(responses)
        self.invocations: List[tuple[List[Any], Dict[str, Any]]] = []

    def invoke(self, messages: List[Any], **kwargs: Any) -> FakeToolResponse:
        self.invocations.append((list(messages), dict(kwargs)))
        return self.responses.pop(0)


class FakeToolChat:
    def __init__(self, responses: List[FakeToolResponse]) -> None:
        self.bound = FakeBoundChat(responses)
        self.bound_tools: List[Any] = []

    def bind_tools(self, tools: List[Any]) -> FakeBoundChat:
        self.bound_tools = list(tools)
        return self.bound


class ProviderConfigurationTests(unittest.TestCase):
    def test_defaults_are_local_and_offline(self) -> None:
        self.assertEqual(provider.selected_provider({}), "local")
        config = provider.load_ollama_config({})
        self.assertEqual(config.model, provider.DEFAULT_MODEL)
        self.assertEqual(config.base_url, provider.DEFAULT_BASE_URL)
        self.assertEqual(config.connect_timeout_seconds, 5.0)
        self.assertEqual(config.invoke_timeout_seconds, 180.0)
        self.assertEqual(config.num_ctx, 4096)
        self.assertFalse(config.allow_remote)

    def test_explicit_environment_is_validated(self) -> None:
        config = provider.load_ollama_config({
            "CRANE_MODEL": "custom:tag",
            "OLLAMA_HOST": "http://127.0.0.1:1234/",
            "CRANE_OLLAMA_CONNECT_TIMEOUT_SECONDS": "1.5",
            "CRANE_OLLAMA_INVOKE_TIMEOUT_SECONDS": "30",
            "CRANE_OLLAMA_NUM_CTX": "8192",
        })
        self.assertEqual(config.model, "custom:tag")
        self.assertEqual(config.base_url, "http://127.0.0.1:1234")
        self.assertEqual(config.connect_timeout_seconds, 1.5)
        self.assertEqual(config.invoke_timeout_seconds, 30.0)
        self.assertEqual(config.num_ctx, 8192)

    def test_empty_model_is_actionable_configuration_error(self) -> None:
        with self.assertRaises(provider.ProviderError) as raised:
            provider.load_ollama_config({"CRANE_MODEL": ""})
        self.assertEqual(raised.exception.code, "provider_not_configured")

    def test_invalid_timeouts_are_rejected(self) -> None:
        for value in ("0", "-1", "abc", "999"):
            with self.subTest(value=value):
                with self.assertRaises(provider.ProviderError) as raised:
                    provider.load_ollama_config({
                        "CRANE_OLLAMA_INVOKE_TIMEOUT_SECONDS": value,
                    })
                self.assertEqual(raised.exception.code, "provider_invalid_configuration")

    def test_invalid_num_ctx_is_rejected(self) -> None:
        for value in ("0", "-1", "1.5", "abc", str(provider.MAX_NUM_CTX + 1)):
            with self.subTest(value=value):
                with self.assertRaises(provider.ProviderError) as raised:
                    provider.load_ollama_config({"CRANE_OLLAMA_NUM_CTX": value})
                self.assertEqual(raised.exception.code, "provider_invalid_configuration")

    def test_config_details_report_bounded_context_size(self) -> None:
        config = provider.load_ollama_config({"CRANE_OLLAMA_NUM_CTX": "2048"})
        self.assertEqual(config.details()["num_ctx"], 2048)

    def test_remote_endpoint_requires_explicit_authorization(self) -> None:
        with self.assertRaises(provider.ProviderError) as raised:
            provider.load_ollama_config({"OLLAMA_HOST": "https://ollama.example.com"})
        self.assertEqual(raised.exception.code, "ollama_remote_endpoint_not_authorized")
        config = provider.load_ollama_config({
            "OLLAMA_HOST": "https://ollama.example.com",
            "CRANE_ALLOW_REMOTE_OLLAMA": "true",
        })
        self.assertEqual(config.base_url, "https://ollama.example.com")
        self.assertTrue(config.allow_remote)

    def test_credentials_in_base_url_are_rejected(self) -> None:
        with self.assertRaises(provider.ProviderError) as raised:
            provider.load_ollama_config({"OLLAMA_HOST": "http://user:secret@127.0.0.1:11434"})
        self.assertEqual(raised.exception.code, "provider_invalid_configuration")


class ProviderInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_fake_chat()

    def test_provider_initialization_uses_langchain_chat_ollama(self) -> None:
        config = provider.load_ollama_config(OLLAMA_ENV)
        with patch.dict(sys.modules, fake_chat_module()):
            chat = provider._create_chat_model(config)
        self.assertEqual(len(FakeChatOllama.calls), 1)
        self.assertEqual(chat.kwargs["model"], "crane-test:latest")
        self.assertEqual(chat.kwargs["base_url"], "http://127.0.0.1:11434")
        self.assertEqual(chat.kwargs["temperature"], 0)
        self.assertEqual(chat.kwargs["num_ctx"], 4096)
        self.assertEqual(chat.kwargs["client_kwargs"]["timeout"], 180.0)

    def test_json_mode_is_passed_to_chat_ollama_for_structured_calls(self) -> None:
        config = provider.load_ollama_config(OLLAMA_ENV)
        with patch.dict(sys.modules, fake_chat_module()):
            provider._create_chat_model(config, json_mode=True)
        self.assertEqual(FakeChatOllama.calls[0]["format"], "json")

    def test_custom_num_ctx_is_passed_to_chat_ollama(self) -> None:
        config = provider.load_ollama_config({
            **OLLAMA_ENV,
            "CRANE_OLLAMA_NUM_CTX": "2048",
        })
        with patch.dict(sys.modules, fake_chat_module()):
            provider._create_chat_model(config)
        self.assertEqual(FakeChatOllama.calls[0]["num_ctx"], 2048)

    def test_chat_model_import_does_not_leave_transformers_blocked(self) -> None:
        config = provider.load_ollama_config(OLLAMA_ENV)
        with patch.dict(sys.modules, fake_chat_module()):
            sys.modules.pop("transformers", None)
            provider._create_chat_model(config)
            self.assertNotIn("transformers", sys.modules)

    def test_missing_dependency_is_actionable(self) -> None:
        config = provider.load_ollama_config(OLLAMA_ENV)
        with patch.dict(sys.modules, {"langchain_ollama": None}):
            with self.assertRaises(provider.ProviderError) as raised:
                provider._create_chat_model(config)
        self.assertEqual(raised.exception.code, "provider_unavailable")
        self.assertIn(provider.OLLAMA_DEPENDENCY, raised.exception.message)

    def test_ollama_unavailable_does_not_fall_back(self) -> None:
        with patch.dict(os.environ, OLLAMA_ENV, clear=True), \
                patch.object(provider.urllib.request, "urlopen", side_effect=URLError("refused")), \
                patch.dict(sys.modules, fake_chat_module()):
            with self.assertRaises(provider.ProviderError) as raised:
                provider.invoke("private task text")
        self.assertEqual(raised.exception.code, "ollama_unavailable")
        self.assertEqual(FakeChatOllama.calls, [])

    def test_missing_configured_model_is_reported_before_invocation(self) -> None:
        with patch.dict(os.environ, OLLAMA_ENV, clear=True), \
                patch.object(provider.urllib.request, "urlopen", return_value=tags_response("other:latest")), \
                patch.dict(sys.modules, fake_chat_module()):
            with self.assertRaises(provider.ProviderError) as raised:
                provider.invoke("private task text")
        self.assertEqual(raised.exception.code, "model_not_found")
        self.assertIn("ollama pull", raised.exception.message)
        self.assertEqual(FakeChatOllama.calls, [])

    def test_model_invocation_failure_is_actionable(self) -> None:
        reset_fake_chat()
        FakeChatOllama.failure = RuntimeError("model exploded")
        with patch.dict(os.environ, OLLAMA_ENV, clear=True), \
                patch.object(provider.urllib.request, "urlopen", return_value=tags_response("crane-test:latest")), \
                patch.dict(sys.modules, fake_chat_module()):
            with self.assertRaises(provider.ProviderError) as raised:
                provider.invoke("private task text")
        self.assertEqual(raised.exception.code, "provider_invocation_failed")

    def test_chat_ollama_invocation_returns_actual_provider_result_shape(self) -> None:
        reset_fake_chat()
        FakeChatOllama.answer = "  real LangChain response text  "
        with patch.dict(os.environ, OLLAMA_ENV, clear=True), \
                patch.object(provider.urllib.request, "urlopen", return_value=tags_response("crane-test:latest")), \
                patch.dict(sys.modules, fake_chat_module()):
            result = provider.invoke("private task text")
        self.assertEqual(result["execution_mode"], "langchain_ollama")
        self.assertEqual(result["provider"], "ollama")
        self.assertEqual(result["model"], "crane-test:latest")
        self.assertEqual(result["model_output"], "real LangChain response text")
        self.assertIsNone(result["notice"])
        self.assertEqual(len(FakeChatOllama.calls), 1)

    def test_unsupported_provider_has_no_cloud_or_local_fallback(self) -> None:
        env = {"CRANE_MODEL_PROVIDER": "hosted", "CRANE_MODEL": "secret-model"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(provider.urllib.request, "urlopen", side_effect=AssertionError("network called")), \
                patch.dict(sys.modules, fake_chat_module()):
            with self.assertRaises(provider.ProviderError) as raised:
                provider.invoke("private task text")
        self.assertEqual(raised.exception.code, "provider_not_supported")
        self.assertEqual(FakeChatOllama.calls, [])

    def test_deterministic_result_never_claims_model_output(self) -> None:
        result = provider.invoke("private task text", {"CRANE_MODEL_PROVIDER": "local"})
        self.assertEqual(result["execution_mode"], "deterministic_local")
        self.assertEqual(result["provider"], "local")
        self.assertIsNone(result["model_output"])
        self.assertIn("deterministic workflow output only", result["notice"])


class ProviderToolLoopTests(unittest.TestCase):
    def run_loop(
        self,
        responses: List[FakeToolResponse],
        tools: List[FakeTool],
        **kwargs: Any,
    ) -> tuple[Dict[str, Any], FakeToolChat]:
        chat = FakeToolChat(responses)
        with patch.dict(os.environ, OLLAMA_ENV, clear=True), \
                patch.object(provider.urllib.request, "urlopen", return_value=tags_response("crane-test:latest")), \
                patch.object(provider, "_create_chat_model", return_value=chat):
            result = asyncio.run(provider.invoke_with_tools("run tool", tools, **kwargs))
        return result, chat

    def test_successful_call_delivers_arguments_and_follows_up_with_tool_result(self) -> None:
        tool = FakeTool("probe_tool_probe", {"probe": "TOOL_CALL_OK", "value": "TOOL_CALL_OK"})
        responses = [
            FakeToolResponse(tool_calls=[{
                "id": "call-1",
                "name": "probe_tool_probe",
                "args": '{"value": "TOOL_CALL_OK"}',
            }]),
            FakeToolResponse("Tool result processed."),
        ]
        result, chat = self.run_loop(responses, [tool])

        self.assertEqual(tool.calls, [{"value": "TOOL_CALL_OK"}])
        self.assertEqual(result["model_output"], "Tool result processed.")
        self.assertEqual(result["execution_mode"], "langchain_ollama_mcp")
        self.assertEqual(len(result["tool_calls"]), 1)
        observed = result["tool_calls"][0]
        self.assertEqual(observed["status"], "succeeded")
        self.assertEqual(observed["tool_call_id"], "call-1")
        self.assertEqual(observed["arguments"], {"value": "TOOL_CALL_OK"})
        self.assertIn("TOOL_CALL_OK", observed["result"])

        self.assertEqual(len(chat.bound.invocations), 2)
        self.assertTrue(all(kwargs.get("stream") is False for _, kwargs in chat.bound.invocations))
        second_messages = chat.bound.invocations[1][0]
        tool_messages = [message for message in second_messages if isinstance(message, ToolMessage)]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0].tool_call_id, "call-1")
        self.assertIn("TOOL_CALL_OK", str(tool_messages[0].content))
        assistant_messages = [message for message in second_messages if isinstance(message, AIMessage)]
        self.assertTrue(any(message.tool_calls for message in assistant_messages))

    def test_unprefixed_tool_name_resolves_to_unique_registered_tool(self) -> None:
        tool = FakeTool("probe_tool_probe", {"invoked": True})
        responses = [
            FakeToolResponse(tool_calls=[{"id": "raw-1", "name": "tool_probe", "args": {"value": "TOOL_CALL_OK"}}]),
            FakeToolResponse("Done"),
        ]
        result, _ = self.run_loop(responses, [tool])
        self.assertEqual(tool.calls, [{"value": "TOOL_CALL_OK"}])
        self.assertEqual(result["tool_calls"][0]["name"], "tool_probe")
        self.assertEqual(result["tool_calls"][0]["status"], "succeeded")

    def test_additional_kwargs_tool_calls_are_parsed(self) -> None:
        tool = FakeTool("probe_tool_probe", {"ok": True})
        responses = [
            FakeToolResponse(tool_calls=None, additional_kwargs={"tool_calls": [{
                "id": "legacy-1",
                "function": {"name": "probe_tool_probe", "arguments": '{"value": "legacy"}'},
            }]}),
            FakeToolResponse("Done"),
        ]
        result, _ = self.run_loop(responses, [tool])
        self.assertEqual(tool.calls, [{"value": "legacy"}])
        self.assertEqual(result["tool_calls"][0]["tool_call_id"], "legacy-1")

    def test_multiple_tool_calls_are_executed_and_correlated(self) -> None:
        tool = FakeTool("probe_tool_probe", {"ok": True})
        responses = [
            FakeToolResponse(tool_calls=[
                {"id": "multi-1", "name": "probe_tool_probe", "args": {"value": "one"}},
                {"id": "multi-2", "name": "probe_tool_probe", "args": {"value": "two"}},
            ]),
            FakeToolResponse("Both tools completed."),
        ]
        result, chat = self.run_loop(responses, [tool])

        self.assertEqual(tool.calls, [{"value": "one"}, {"value": "two"}])
        self.assertEqual([call["status"] for call in result["tool_calls"]], ["succeeded", "succeeded"])
        tool_messages = [message for message in chat.bound.invocations[1][0] if isinstance(message, ToolMessage)]
        self.assertEqual([message.tool_call_id for message in tool_messages], ["multi-1", "multi-2"])

    def test_unknown_tool_and_invalid_arguments_are_reported_as_failures(self) -> None:
        tool = FakeTool("probe_tool_probe", {"ok": True})
        responses = [
            FakeToolResponse(tool_calls=[
                {"id": "bad-1", "name": "missing_tool", "args": {"value": "x"}},
                {"id": "bad-2", "name": "probe_tool_probe", "args": "[]"},
            ]),
            FakeToolResponse("Recovered from failed tool calls."),
        ]
        result, chat = self.run_loop(responses, [tool])

        self.assertEqual(tool.calls, [])
        self.assertEqual([call["status"] for call in result["tool_calls"]], ["failed", "failed"])
        self.assertEqual(result["tool_calls"][0]["error"], "MCP tool 'missing_tool' is not available")
        self.assertEqual(result["tool_calls"][1]["error"], "tool arguments must be a JSON object")
        tool_messages = [message for message in chat.bound.invocations[1][0] if isinstance(message, ToolMessage)]
        self.assertEqual([message.status for message in tool_messages], ["error", "error"])

    def test_tool_exception_does_not_fabricate_a_result(self) -> None:
        tool = FakeTool("probe_tool_probe", error=RuntimeError("secret transport detail"))
        responses = [
            FakeToolResponse(tool_calls=[{"id": "exc-1", "name": "probe_tool_probe", "args": {}}]),
            FakeToolResponse("Tool failed."),
        ]
        result, chat = self.run_loop(responses, [tool])

        self.assertEqual(len(tool.calls), 1)
        observed = result["tool_calls"][0]
        self.assertEqual(observed["status"], "failed")
        self.assertEqual(observed["result"], "MCP tool call failed")
        self.assertNotIn("secret transport detail", json.dumps(result))
        tool_message = next(message for message in chat.bound.invocations[1][0] if isinstance(message, ToolMessage))
        self.assertEqual(tool_message.status, "error")

    def test_approval_blocked_tool_result_is_not_reported_as_success(self) -> None:
        tool = FakeTool("probe_tool_probe", "MCP tool failed: This tool requires explicit approval for this exact action")
        responses = [
            FakeToolResponse(tool_calls=[{"id": "blocked-1", "name": "probe_tool_probe", "args": {}}]),
            FakeToolResponse("Approval required."),
        ]
        result, chat = self.run_loop(responses, [tool])

        observed = result["tool_calls"][0]
        self.assertEqual(observed["status"], "blocked")
        self.assertEqual(observed["error"], "approval_required")
        tool_message = next(message for message in chat.bound.invocations[1][0] if isinstance(message, ToolMessage))
        self.assertEqual(tool_message.status, "error")

    def test_malformed_tool_call_and_empty_response_are_handled(self) -> None:
        responses = [
            FakeToolResponse(tool_calls=[{}, {"name": ""}]),
            FakeToolResponse("Recovered."),
        ]
        result, _ = self.run_loop(responses, [])

        self.assertEqual(len(result["tool_calls"]), 2)
        self.assertTrue(all(call["status"] == "failed" for call in result["tool_calls"]))
        self.assertEqual(result["tool_calls"][0]["error"], "Malformed tool call response")

        with patch.dict(os.environ, OLLAMA_ENV, clear=True), \
                patch.object(provider.urllib.request, "urlopen", return_value=tags_response("crane-test:latest")), \
                patch.object(provider, "_create_chat_model", return_value=FakeToolChat([FakeToolResponse("")])):
            with self.assertRaises(provider.ProviderError) as raised:
                asyncio.run(provider.invoke_with_tools("run tool", []))
        self.assertEqual(raised.exception.code, "provider_empty_response")

    def test_tool_result_is_bounded(self) -> None:
        tool = FakeTool("probe_tool_probe", "x" * 50_000)
        responses = [
            FakeToolResponse(tool_calls=[{"id": "large-1", "name": "probe_tool_probe", "args": {}}]),
            FakeToolResponse("Done"),
        ]
        result, chat = self.run_loop(responses, [tool], max_tool_output_chars=1000)

        observed_result = result["tool_calls"][0]["result"]
        self.assertLessEqual(len(observed_result), 1020)
        self.assertIn("[truncated]", observed_result)
        tool_message = next(message for message in chat.bound.invocations[1][0] if isinstance(message, ToolMessage))
        self.assertLessEqual(len(str(tool_message.content)), 1020)

    def test_max_tool_iterations_and_call_budget_are_enforced(self) -> None:
        tool = FakeTool("probe_tool_probe", {"ok": True})
        repeated_call = FakeToolResponse(tool_calls=[
            {"id": "loop-1", "name": "probe_tool_probe", "args": {}},
        ])
        with patch.dict(os.environ, OLLAMA_ENV, clear=True), \
                patch.object(provider.urllib.request, "urlopen", return_value=tags_response("crane-test:latest")), \
                patch.object(provider, "_create_chat_model", return_value=FakeToolChat([repeated_call])):
            with self.assertRaises(provider.ProviderError) as rounds_error:
                asyncio.run(provider.invoke_with_tools("run tool", [tool], max_tool_rounds=1))
        self.assertEqual(rounds_error.exception.code, "tool_loop_limit")

        two_calls = FakeToolResponse(tool_calls=[
            {"id": "budget-1", "name": "probe_tool_probe", "args": {}},
            {"id": "budget-2", "name": "probe_tool_probe", "args": {}},
        ])
        with patch.dict(os.environ, OLLAMA_ENV, clear=True), \
                patch.object(provider.urllib.request, "urlopen", return_value=tags_response("crane-test:latest")), \
                patch.object(provider, "_create_chat_model", return_value=FakeToolChat([two_calls])):
            with self.assertRaises(provider.ProviderError) as call_error:
                asyncio.run(provider.invoke_with_tools("run tool", [tool], max_tool_calls=1))
        self.assertEqual(call_error.exception.code, "tool_call_limit")
        self.assertEqual(len(tool.calls), 1)


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_fake_chat()

    @staticmethod
    def run_workflow(env: Dict[str, str]) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        stdout = io.StringIO()
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(stdout):
            outcome = asyncio.run(workflow_engine.execute_task(
                "private task text", task_id="integration-task"
            ))
        frames = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        return outcome, frames

    def test_langgraph_persists_real_provider_output(self) -> None:
        with patch.object(provider.urllib.request, "urlopen", return_value=tags_response("crane-test:latest")), \
                patch.dict(sys.modules, fake_chat_module()):
            outcome, frames = self.run_workflow(OLLAMA_ENV)
        self.assertEqual(outcome["status"], "succeeded")
        self.assertEqual(outcome["result"]["execution_mode"], "langchain_ollama")
        self.assertEqual(outcome["result"]["model_output"], "Mocked Ollama answer")
        persisted = [frame for frame in frames if frame["type"] == "task_state"]
        self.assertTrue(persisted)
        self.assertEqual(persisted[-1]["checkpoint"]["result"]["model_output"], "Mocked Ollama answer")
        events = [frame for frame in frames if frame["type"] == "task_event"]
        self.assertTrue(any(frame["event_type"] == "provider_result" for frame in events))

    def test_langgraph_propagates_provider_failure_without_fallback(self) -> None:
        with patch.object(provider.urllib.request, "urlopen", return_value=tags_response("other:latest")), \
                patch.dict(sys.modules, fake_chat_module()):
            outcome, frames = self.run_workflow(OLLAMA_ENV)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["result"]["code"], "model_not_found")
        self.assertIn("not installed", outcome["failure_reason"])
        self.assertNotIn("deterministic_local", json.dumps(outcome))
        persisted = [frame for frame in frames if frame["type"] == "task_state"]
        self.assertEqual(persisted[-1]["state"], "failed")
        events = [frame for frame in frames if frame["type"] == "task_event"]
        self.assertTrue(any(frame["event_type"] == "provider_failed" for frame in events))
        self.assertEqual(FakeChatOllama.calls, [])

    def test_deterministic_langgraph_result_is_not_reported_as_llm(self) -> None:
        outcome, _ = self.run_workflow({"CRANE_MODEL_PROVIDER": "local"})
        self.assertEqual(outcome["status"], "succeeded")
        self.assertEqual(outcome["result"]["execution_mode"], "deterministic_local")
        self.assertIsNone(outcome["result"]["model_output"])


class DiagnosticsTests(unittest.TestCase):
    def test_diagnostics_report_model_and_provider_state_without_task_data(self) -> None:
        reset_fake_chat()
        private_task = "private task text that must not appear in diagnostics"
        with patch.dict(os.environ, OLLAMA_ENV, clear=True), \
                patch.object(provider.urllib.request, "urlopen", return_value=tags_response("crane-test:latest")), \
                patch.dict(sys.modules, fake_chat_module()):
            report = provider.collect_diagnostics()
        encoded = json.dumps(report)
        self.assertNotIn(private_task, encoded)
        self.assertEqual(report["provider"], "ollama")
        self.assertTrue(report["ollama"]["reachable"])
        self.assertTrue(report["ollama"]["model_available"])
        self.assertTrue(report["ollama"]["provider_initializable"])
        self.assertEqual(report["ollama"]["configured_model"], "crane-test:latest")

    def test_diagnostics_separate_connectivity_from_missing_model(self) -> None:
        with patch.dict(os.environ, OLLAMA_ENV, clear=True), \
                patch.object(provider.urllib.request, "urlopen", return_value=tags_response("other:latest")), \
                patch.dict(sys.modules, fake_chat_module()):
            report = provider.collect_diagnostics()
        self.assertTrue(report["ollama"]["reachable"])
        self.assertFalse(report["ollama"]["model_available"])
        self.assertTrue(any(issue["code"] == "model_not_found" for issue in report["issues"]))


if __name__ == "__main__":
    unittest.main()
