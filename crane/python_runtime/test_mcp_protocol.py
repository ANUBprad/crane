"""Regression tests for the long-lived MCP protocol daemon dispatch."""
from __future__ import annotations

import asyncio
import unittest

import crane.mcp_runtime as mcp_runtime
from crane.python_runtime.mcp_protocol import MCPProtocolRuntime


class MCPProtocolDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        # Isolate global manager state between tests.
        mcp_runtime.configure_manager(task_id="test", configs=[], granted_fingerprints=[])

    def tearDown(self) -> None:
        try:
            asyncio.run(mcp_runtime.shutdown_all())
        except Exception:
            pass
        mcp_runtime.configure_manager(task_id="test", configs=[], granted_fingerprints=[])

    def test_configure_then_list_servers_uses_current_manager(self) -> None:
        async def scenario() -> None:
            runtime = MCPProtocolRuntime()
            server = {
                "server_id": "srv-1",
                "name": "Server One",
                "transport": "stdio",
                "command": "python",
                "args": ["server.py"],
                "enabled": True,
            }
            configured = await runtime._dispatch({
                "operation": "configure",
                "payload": {
                    "servers": [server],
                    "granted_approvals": [],
                    "task_id": "mcp-daemon",
                },
            })
            self.assertEqual(len(configured["servers"]), 1)
            self.assertEqual(configured["servers"][0]["config"]["server_id"], "srv-1")

            # Regression: dispatch must read the manager replaced by configure_manager.
            listed = await runtime._dispatch({
                "operation": "list_servers",
                "payload": {},
            })
            self.assertEqual(len(listed["servers"]), 1)
            self.assertEqual(listed["servers"][0]["config"]["server_id"], "srv-1")

            added = await runtime._dispatch({
                "operation": "add_server",
                "payload": {
                    "config": {
                        "server_id": "srv-2",
                        "name": "Server Two",
                        "transport": "stdio",
                        "command": "python",
                        "args": ["server.py"],
                        "enabled": True,
                    }
                },
            })
            self.assertEqual(added["server"]["server_id"], "srv-2")

            listed2 = await runtime._dispatch({
                "operation": "list_servers",
                "payload": {},
            })
            ids = sorted(s["config"]["server_id"] for s in listed2["servers"])
            self.assertEqual(ids, ["srv-1", "srv-2"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
