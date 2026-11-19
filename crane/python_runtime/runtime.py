"""CRANE Python agent runtime core.

Handles agent execution, signal lifecycle, and protocol responses.
This module is imported by the CLI entry point and by the Go supervisor
for integration testing and runtime communication.
"""
import json
import signal
import sys
import time
from typing import Any, Dict, Optional


__all__ = [
    "Runtime",
    "handle_signal",
    "build_health_response",
    "parse_message",
    "validate_message",
]


class Runtime:
    """Manages the CRANE agent runtime lifecycle.

    Responsible for:
    - Signal handling (SIGINT, SIGTERM)
    - Health check responses
    - Message parsing and validation
    - Graceful shutdown coordination
    """

    def __init__(self, version: str = "0.1.0", python_version: Optional[str] = None) -> None:
        self.version = version
        self.python_version = python_version or f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        self._shutdown = False
        self._signal_handled = False

    def handle_signal(self, signum: int, frame: Any) -> None:
        """Handle SIGINT/SIGTERM with graceful shutdown semantics.

        Args:
            signum: The signal number.
            frame: The current stack frame.
        """
        self._signal_handled = True
        self._shutdown = True

    def run(self) -> None:
        """Main runtime event loop.

        Waits for shutdown signals. In the full product this loop
        drives LangGraph workflow execution, MCP tool calls, and
        LLM provider interactions.
        """
        # Set up signal handlers
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

        # Block until a signal is received
        while not self._shutdown:
            time.sleep(0.5)

        # Graceful shutdown path
        self._do_shutdown()

    def _do_shutdown(self) -> None:
        """Perform graceful shutdown bookkeeping.

        Persists checkpoint references, releases resources,
        and ensures no child processes are left orphaned.
        """
        # TODO: persist checkpoints, clean up MCP connections
        pass

    def health_check(self) -> Dict[str, Any]:
        """Return a structured health-check response.

        Returns:
            A dictionary conforming to the crane-agent/0.1.0 protocol.
        """
        return {
            "protocol": "crane-agent/0.1.0",
            "status": "healthy",
            "version": self.version,
            "python_version": self.python_version,
        }

    @staticmethod
    def parse_message(raw: str) -> Optional[Dict[str, Any]]:
        """Parse a JSON message from raw string.

        Args:
            raw: The raw message string.

        Returns:
            Parsed dictionary, or None if the message is not valid JSON.
        """
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def validate_message(msg: Dict[str, Any]) -> bool:
        """Validate that a message has the expected structure.

        Required fields for a valid crane-agent protocol message:
        - "type": message type identifier

        Args:
            msg: The parsed message dictionary.

        Returns:
            True if the message is structurally valid.
        """
        if not isinstance(msg, dict):
            return False
        if "type" not in msg:
            return False
        return True

    def shutdown(self) -> None:
        """Request shutdown of the runtime."""
        self._shutdown = True

    def run_with_handling(self) -> int:
        """Run the runtime with structured error handling.

        Returns:
            Exit code (0 for success, non-zero for failure).
        """
        try:
            self.run()
            return 0
        except KeyboardInterrupt:
            self._shutdown = True
            return 1
        except Exception as exc:  # noqa: BLE001
            # TODO: log structured error
            print(f"Runtime error: {exc}", file=sys.stderr)
            return 1


def build_health_response(version: str = "0.1.0", python_version: Optional[str] = None) -> str:
    """Build a JSON health-check response.

    Args:
        version: The runtime version string.
        python_version: The Python version string. Defaults to sys.version.

    Returns:
        A JSON-encoded string conforming to the crane-agent/0.1.0 protocol.
    """
    python_ver = python_version or f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    response = {
        "protocol": "crane-agent/0.1.0",
        "status": "healthy",
        "version": version,
        "python_version": python_ver,
    }
    return json.dumps(response, ensure_ascii=False)


def parse_message_from_raw(raw: str) -> Optional[Dict[str, Any]]:
    """Parse and validate a JSON message in one step.

    Args:
        raw: Raw string potentially containing JSON.

    Returns:
        Parsed dictionary if valid and passes validation, else None.
    """
    parsed = Runtime.parse_message(raw)
    if parsed is None:
        return None
    if not Runtime.validate_message(parsed):
        return None
    return parsed