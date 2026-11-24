"""Real Ollama end-to-end test for the MCP tool-calling lifecycle.

This test is opt-in because it requires a locally running Ollama server and the
configured model. Run it with:

    CRANE_RUN_OLLAMA_TOOL_E2E=1 python -m unittest crane.python_runtime.test_ollama_tool_integration -v
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from crane.python_runtime import provider, workflow_engine

ROOT = Path(__file__).resolve().parents[2]
TEST_SERVER = ROOT / "crane" / "mcp_runtime" / "test_server.py"
MODEL = os.environ.get("CRANE_E2E_MODEL", "llama3.2:3b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
INSTRUCTION = "You must call tool_probe with value TOOL_CALL_OK. Do not respond in prose."


@unittest.skipUnless(
    os.environ.get("CRANE_RUN_OLLAMA_TOOL_E2E") == "1",
    "set CRANE_RUN_OLLAMA_TOOL_E2E=1 to run the real Ollama tool-call test",
)
class OllamaToolIntegrationTest(unittest.TestCase):
    def test_real_ollama_executes_tool_probe_and_returns_follow_up(self) -> None:
        config = provider.load_ollama_config({
            "CRANE_MODEL_PROVIDER": "ollama",
            "CRANE_MODEL": MODEL,
            "OLLAMA_HOST": OLLAMA_HOST,
        })
        provider.preflight_ollama(config)

        with tempfile.TemporaryDirectory(prefix="crane-ollama-tool-") as temp_dir:
            status_path = Path(temp_dir) / "tool-probe-status.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                outcome = asyncio.run(workflow_engine.execute_task(
                    INSTRUCTION,
                    task_id="ollama-tool-probe-e2e",
                    env={
                        "CRANE_MODEL_PROVIDER": "ollama",
                        "CRANE_MODEL": MODEL,
                        "OLLAMA_HOST": OLLAMA_HOST,
                    },
                    mcp_servers=[{
                        "server_id": "probe",
                        "name": "Tool Probe",
                        "transport": "stdio",
                        "command": "python",
                        "args": [str(TEST_SERVER)],
                        "env": {"CRANE_MCP_TOOL_PROBE_STATUS_FILE": str(status_path)},
                        "enabled": True,
                        "connect_timeout_seconds": 10,
                        "invocation_timeout_seconds": 30,
                    }],
                ))

            frames: List[Dict[str, Any]] = [
                json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()
            ]
            discovery = [
                frame for frame in frames
                if frame.get("type") == "task_event"
                and frame.get("event_type") == "mcp_server_discovery"
            ]
            self.assertTrue(discovery, "MCP tool discovery did not emit an event")
            self.assertGreaterEqual(int(discovery[0]["payload"].get("tool_count") or 0), 5)

            self.assertTrue(status_path.exists(), "tool_probe did not write its execution status file")
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(status.get("invoked"), status)
            values = status.get("values") or []
            self.assertTrue(values, status)
            self.assertEqual(set(values), {"TOOL_CALL_OK"}, status)

            self.assertEqual(outcome["status"], "succeeded", outcome)
            result = outcome["result"]
            self.assertEqual(result["execution_status"], "fully_completed", result)
            model_output = str(result.get("model_output") or "").strip()
            self.assertTrue(model_output, result)
            self.assertIn("TOOL_CALL_OK", model_output, result)

            probe_calls = [
                call for call in result.get("tool_calls") or []
                if call.get("tool_name") == "tool_probe"
            ]
            self.assertTrue(probe_calls, result)
            invocation_ids = [str(call.get("invocation_id") or "") for call in probe_calls]
            self.assertEqual(len(invocation_ids), len(set(invocation_ids)), probe_calls)
            self.assertTrue(any(call.get("server_id") == "probe" for call in probe_calls), probe_calls)
            self.assertTrue(any(call.get("status") == "succeeded" for call in probe_calls), probe_calls)
            self.assertTrue(
                any(call.get("arguments") == {"value": "TOOL_CALL_OK"} for call in probe_calls),
                probe_calls,
            )


if __name__ == "__main__":
    unittest.main()
