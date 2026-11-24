import json
import threading
import unittest
from unittest import mock

from crane.python_runtime import protocol, protocol_runtime, workflow_engine


class ProtocolRuntimeCancellationTests(unittest.TestCase):
    def test_cancellation_result_is_sent_before_model_work_finishes(self) -> None:
        task_id = "t-protocol-cancel"
        started = threading.Event()
        release = threading.Event()
        written = []

        async def slow_task(*args, **kwargs):
            workflow_engine._register_cancellation(task_id, threading.Event())
            started.set()
            release.wait(2)
            return {
                "task_id": task_id,
                "status": "succeeded",
                "result": {"execution_status": "fully_completed"},
            }

        def fake_write(message):
            written.append(message)
            if message.get("type") == protocol.TYPE_TASK_RESULT and message.get("status") == "cancelled":
                release.set()

        submission = protocol.encode({
            "type": protocol.TYPE_TASK_SUBMISSION,
            "task_id": task_id,
            "input": "long running work",
            "resume": False,
            "checkpoint": None,
            "mcp_servers": [],
            "granted_approvals": [],
            "conversation_messages": [],
        })
        cancellation = protocol.encode({
            "type": protocol.TYPE_TASK_CANCELLATION,
            "task_id": task_id,
            "reason": "supervisor_request",
        })

        def stdin_lines():
            yield submission + "\n"
            self.assertTrue(started.wait(2))
            yield cancellation + "\n"

        with mock.patch("sys.stdin", stdin_lines()), \
             mock.patch.object(protocol, "write_stdout", side_effect=fake_write), \
             mock.patch.object(protocol_runtime, "execute_task", side_effect=slow_task):
            self.assertEqual(protocol_runtime.ProtocolRuntime().run(), 0)

        results = [m for m in written if m.get("type") == protocol.TYPE_TASK_RESULT]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertEqual(results[0]["execution_status"], "cancelled")
        events = [m for m in written if m.get("type") == protocol.TYPE_TASK_EVENT]
        self.assertTrue(any(m.get("event_type") == "cancellation_queued" for m in events))


class ProtocolRuntimeWorkspaceTests(unittest.TestCase):
    def test_task_submission_workspace_reaches_execute_task(self) -> None:
        task_id = "t-protocol-workspace"
        workspace = r"C:\workspace\crane"
        captured = {}

        async def capture_task(_input, **kwargs):
            captured.update(kwargs)
            return {
                "task_id": task_id,
                "status": "succeeded",
                "result": {"execution_status": "fully_completed"},
                "result_summary": "ok",
                "graph_steps": 1,
                "wall_clock_ms": 0,
                "current_step": "verified",
            }

        submission = protocol.encode({
            "type": protocol.TYPE_TASK_SUBMISSION,
            "task_id": task_id,
            "input": "Create a project with tests and repair failures",
            "resume": False,
            "checkpoint": None,
            "mcp_servers": [],
            "granted_approvals": [],
            "conversation_messages": [],
            "workspace": workspace,
        })

        with mock.patch("sys.stdin", iter([submission + "\n"])), \
             mock.patch.object(protocol, "write_stdout"), \
             mock.patch.object(protocol_runtime, "execute_task", side_effect=capture_task):
            self.assertEqual(protocol_runtime.ProtocolRuntime().run(), 0)

        self.assertEqual(captured.get("workspace"), workspace)


if __name__ == "__main__":
    unittest.main()
