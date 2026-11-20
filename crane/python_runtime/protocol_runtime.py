"""Long-lived Python process driven by Go through versioned NDJSON messages."""
from __future__ import annotations

import asyncio
import os
import queue
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, Optional

from crane.python_runtime import protocol
from crane.python_runtime.workflow_engine import execute_task, request_cancellation

_POLL_INTERVAL_SECONDS = 0.05


class ProtocolRuntime:
    """Reads commands from stdin and runs one workflow task at a time."""

    def __init__(self) -> None:
        self.version = "0.1.0"
        self._stop = threading.Event()

    def _read_stdin(self, commands: "queue.Queue[Optional[Dict[str, Any]]]") -> None:
        try:
            for line in sys.stdin:
                if self._stop.is_set():
                    break
                try:
                    commands.put(protocol.decode(line.rstrip("\r\n")))
                except protocol.ProtocolError as exc:
                    commands.put({"type": protocol.TYPE_ERROR_RESPONSE, "code": "bad_request", "message": str(exc)})
        finally:
            commands.put(None)

    @staticmethod
    def _error(code: str, message: str, task_id: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {"type": protocol.TYPE_ERROR_RESPONSE, "code": code, "message": message}
        if task_id:
            payload["task_id"] = task_id
        protocol.write_stdout(payload)

    @staticmethod
    def _send_result(outcome: Dict[str, Any]) -> None:
        protocol.write_stdout({
            "type": protocol.TYPE_TASK_RESULT,
            "task_id": outcome["task_id"],
            "status": outcome["status"],
            "result": outcome.get("result"),
            "result_summary": outcome.get("result_summary"),
            "failure_reason": outcome.get("failure_reason"),
            "graph_steps": outcome.get("graph_steps", 0),
            "wall_clock_ms": outcome.get("wall_clock_ms", 0),
            "current_step": outcome.get("current_step", ""),
            "checkpoint": outcome.get("checkpoint"),
        })

    def run(self) -> int:
        protocol.write_stdout({
            "type": protocol.TYPE_RUNTIME_IDENTITY,
            "runtime_version": self.version,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "pid": os.getpid(),
        })
        commands: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        reader = threading.Thread(target=self._read_stdin, args=(commands,), daemon=True)
        reader.start()
        executor = ThreadPoolExecutor(max_workers=1)
        current_id: Optional[str] = None
        future: Optional[Future] = None
        eof = False

        try:
            while not eof:
                if future is None:
                    message = commands.get()
                else:
                    try:
                        message = commands.get(timeout=_POLL_INTERVAL_SECONDS)
                    except queue.Empty:
                        if future.done():
                            self._send_result(future.result())
                            future = None
                            current_id = None
                        continue
                if message is None:
                    eof = True
                    continue
                if message.get("type") == protocol.TYPE_ERROR_RESPONSE:
                    self._error(str(message.get("code", "bad_request")), str(message.get("message", "invalid protocol message")))
                    continue
                if message.get("type") == protocol.TYPE_HEALTH_CHECK:
                    protocol.write_stdout({
                        "type": protocol.TYPE_SUCCESS_RESPONSE,
                        "protocol": protocol.PROTOCOL,
                        "status": "healthy",
                        "version": self.version,
                        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    })
                    continue
                if message.get("type") == protocol.TYPE_SHUTDOWN_REQUEST:
                    self._stop.set()
                    eof = True
                    continue
                if message.get("type") == protocol.TYPE_TASK_CANCELLATION:
                    try:
                        command = protocol.parse_task_cancellation(message)
                    except protocol.ProtocolError as exc:
                        self._error("bad_request", str(exc))
                        continue
                    if current_id == command["task_id"]:
                        if not request_cancellation(current_id):
                            protocol.write_stdout(protocol.task_event(
                                current_id, "cancellation_queued", {"reason": command["reason"]}
                            ))
                    else:
                        self._error("not_found", "no active task has that identifier", command["task_id"])
                    continue
                if message.get("type") != protocol.TYPE_TASK_SUBMISSION:
                    self._error("unsupported_message", "unsupported protocol message type")
                    continue
                if future is not None and not future.done():
                    self._error("busy", "the Python runtime already has an active task")
                    continue
                try:
                    command = protocol.parse_task_submission(message)
                except protocol.ProtocolError as exc:
                    self._error("bad_request", str(exc))
                    continue
                current_id = command["task_id"]
                future = executor.submit(
                    asyncio.run,
                    execute_task(
                        command["input"],
                        python_runtime_pid=os.getpid(),
                        task_id=current_id,
                        resume_checkpoint=command["checkpoint"] if command["resume"] else None,
                        mcp_servers=command["mcp_servers"],
                        granted_approvals=command["granted_approvals"],
                    ),
                )
            if future is not None:
                self._send_result(future.result())
        finally:
            self._stop.set()
            executor.shutdown(wait=True, cancel_futures=True)
        return 0
