import unittest

from crane.python_runtime.context import build_runtime_context


class ContextTest(unittest.TestCase):
    def test_runtime_context_includes_identity_and_provider(self) -> None:
        ctx = build_runtime_context(user_input="What is CRANE?", conversation_id="conv-1")
        self.assertEqual(ctx["identity"]["name"], "CRANE")
        self.assertIn("system_prompt", ctx)
        self.assertIn("CRANE", ctx["system_prompt"])
        self.assertEqual(ctx["conversation"]["id"], "conv-1")

    def test_runtime_context_reports_no_tools(self) -> None:
        ctx = build_runtime_context(user_input="hello", mcp_tools=[])
        self.assertIn("No MCP/external tools", ctx["system_prompt"])

    def test_runtime_context_reports_generation_capability(self) -> None:
        ctx = build_runtime_context(user_input="Create a project", mcp_tools=[])
        self.assertTrue(any("project generation" in item.lower() for item in ctx["capabilities"]["available"]))
        self.assertFalse(any("project generation" in item.lower() for item in ctx["capabilities"]["not_implemented"]))


if __name__ == "__main__":
    unittest.main()
