"""CRANE Python agent runtime CLI entry point.

Stdout is reserved for versioned protocol messages in child-process mode.
Diagnostics always go to stderr.
"""
import logging
import sys

from crane.python_runtime.protocol_runtime import ProtocolRuntime
from crane.python_runtime.runtime import Runtime, build_health_response


def main() -> None:
    """CLI entry point for the CRANE Python agent runtime."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    runtime = Runtime()

    if len(sys.argv) > 1 and sys.argv[1] == "--health":
        print(build_health_response(runtime.version, runtime.python_version))
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--protocol":
        raise SystemExit(ProtocolRuntime().run())

    raise SystemExit(ProtocolRuntime().run())


if __name__ == "__main__":
    main()
