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
from typing import Any, Dict, List
from unittest.mock import patch
from urllib.error import URLError

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


class ProviderConfigurationTests(unittest.TestCase):
    def test_defaults_are_local_and_offline(self) -> None:
        self.assertEqual(provider.selected_provider({}), "local")
        config = provider.load_ollama_config({})
        self.assertEqual(config.model, provider.DEFAULT_MODEL)
        self.assertEqual(config.base_url, provider.DEFAULT_BASE_URL)
        self.assertEqual(config.connect_timeout_seconds, 5.0)
        self.assertEqual(config.invoke_timeout_seconds, 45.0)
        self.assertFalse(config.allow_remote)

    def test_explicit_environment_is_validated(self) -> None:
        config = provider.load_ollama_config({
            "CRANE_MODEL": "custom:tag",
            "OLLAMA_HOST": "http://127.0.0.1:1234/",
            "CRANE_OLLAMA_CONNECT_TIMEOUT_SECONDS": "1.5",
            "CRANE_OLLAMA_INVOKE_TIMEOUT_SECONDS": "30",
        })
        self.assertEqual(config.model, "custom:tag")
        self.assertEqual(config.base_url, "http://127.0.0.1:1234")
        self.assertEqual(config.connect_timeout_seconds, 1.5)
        self.assertEqual(config.invoke_timeout_seconds, 30.0)

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
        self.assertEqual(chat.kwargs["client_kwargs"]["timeout"], 45.0)

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
