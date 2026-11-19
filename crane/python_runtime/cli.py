"""CRANE Python agent runtime CLI entry point.

Used directly as an executable script and as the child process
that the Go supervisor launches and communicates with.
"""
import sys
from crane.python_runtime.runtime import Runtime, build_health_response


def main() -> None:
    """CLI entry point for the CRANE Python agent runtime."""
    runtime = Runtime()

    # If --health is passed, emit health check and exit
    if len(sys.argv) > 1 and sys.argv[1] == "--health":
        print(build_health_response(runtime.version, runtime.python_version))
        sys.exit(0)

    # Otherwise, run the full runtime lifecycle
    runtime.run()


if __name__ == "__main__":
    main()