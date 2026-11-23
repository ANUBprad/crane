import json
import unittest

from crane.mcp_runtime.builtin_server import _is_private_host, fetch_url, run_python


class BuiltinServerTest(unittest.TestCase):
    def test_private_host_detection(self) -> None:
        self.assertTrue(_is_private_host("127.0.0.1"))
        self.assertTrue(_is_private_host("localhost"))
        self.assertTrue(_is_private_host("10.0.0.1"))

    def test_fetch_url_rejects_private_host(self) -> None:
        result = json.loads(fetch_url("http://127.0.0.1/test"))
        self.assertIn("error", result)

    def test_run_python_executes_code(self) -> None:
        result = json.loads(run_python("print('ok')", timeout_seconds=10))
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("ok", result["stdout"])
        self.assertFalse(result["timed_out"])


if __name__ == "__main__":
    unittest.main()
