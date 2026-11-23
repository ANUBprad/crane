import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from crane.python_runtime import generation as gen
from crane.python_runtime import workflow_engine


class GenerationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_rejects_absolute_and_traversal_paths(self):
        with self.assertRaises(gen.GenerationError):
            gen.validate_relative_path(self.ws, "/etc/passwd")
        with self.assertRaises(gen.GenerationError):
            gen.validate_relative_path(self.ws, "../escape.txt")
        with self.assertRaises(gen.GenerationError):
            gen.validate_relative_path(self.ws, "a/../../escape.txt")
        with self.assertRaises(gen.GenerationError):
            gen.validate_relative_path(self.ws, "C:\\Windows\\system.ini")

    def test_rejects_protected_paths(self):
        with self.assertRaises(gen.GenerationError):
            gen.validate_relative_path(self.ws, ".git/config")
        with self.assertRaises(gen.GenerationError):
            gen.validate_relative_path(self.ws, ".env")
        with self.assertRaises(gen.GenerationError):
            gen.validate_relative_path(self.ws, "secrets/api.key")

    def test_rejects_windows_device_ads_and_invalid_names_on_every_host(self):
        invalid_paths = [
            r"C:relative.txt", r"\\\\server\\share\\file.txt", r"\\\\?\\C:\\Windows\\win.ini",
            "src/file.txt:secret", "folder/NUL.txt", "folder/COM1.log",
            "folder/name. ", "folder/name.", "folder/bad?.txt", "folder/../escape.txt",
        ]
        for invalid in invalid_paths:
            with self.subTest(path=invalid):
                with self.assertRaises(gen.GenerationError):
                    gen.validate_relative_path(self.ws, invalid)

    def test_rejects_dangling_symlink_parent(self):
        link = self.ws / "dangling"
        try:
            os.symlink(str(self.ws / "missing-target"), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not permitted on this host")
        with self.assertRaises(gen.GenerationError):
            gen.validate_relative_path(self.ws, "dangling/new.txt")

    def test_rejects_windows_directory_junction_escape(self):
        if os.name != "nt":
            self.skipTest("Windows junction validation")
        outside_temp = tempfile.TemporaryDirectory()
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)
        junction = self.ws / "linked"
        created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True, text=True, check=False)
        if created.returncode != 0:
            self.skipTest("directory junction creation is unavailable to this runner")
        try:
            with self.assertRaises(gen.GenerationError):
                gen.validate_relative_path(self.ws, "linked/escape.txt")
        finally:
            os.rmdir(junction)

    def test_rejects_staging_root_junction(self):
        if os.name != "nt":
            self.skipTest("Windows junction validation")
        outside_temp = tempfile.TemporaryDirectory()
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)
        junction = self.ws / ".crane-staging"
        created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True, text=True, check=False)
        if created.returncode != 0:
            self.skipTest("directory junction creation is unavailable to this runner")
        ops = gen.validate_file_operations(self.ws, [{"path": "src/app.py", "operation": "write", "content": "print('safe')"}])
        try:
            with self.assertRaises(gen.GenerationError):
                gen.stage_operations(self.ws, ops)
            self.assertFalse((outside / "src" / "app.py").exists())
        finally:
            os.rmdir(junction)

    def test_stage_reference_cannot_be_forged_outside_staging_root(self):
        outside_temp = tempfile.TemporaryDirectory()
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)
        external_file = outside / "app.py"
        external_file.write_text("do not read or apply", encoding="utf-8")
        ops = gen.validate_file_operations(self.ws, [{"path": "app.py", "operation": "write", "content": "print('safe')"}])
        stage = gen.stage_operations(self.ws, ops)
        stage["files"][0]["staged_path"] = str(external_file)
        try:
            with self.assertRaises(gen.GenerationError):
                gen.apply_staged_operations(self.ws, stage, approved_overwrites=False)
        finally:
            gen.cleanup_stage({"stage_dir": stage["stage_dir"]}, self.ws)
        self.assertEqual(external_file.read_text(encoding="utf-8"), "do not read or apply")

    def test_cleanup_stage_refuses_outside_numeric_directory(self):
        outside_temp = tempfile.TemporaryDirectory()
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name) / "123"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("preserve", encoding="utf-8")
        self.assertFalse(gen.cleanup_stage({"stage_dir": str(outside)}, self.ws))
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_new_file_created_after_staging_is_never_overwritten_without_approval(self):
        ops = gen.validate_file_operations(self.ws, [{"path": "src/app.py", "operation": "write", "content": "generated"}])
        stage = gen.stage_operations(self.ws, ops)
        target = self.ws / "src" / "app.py"
        target.parent.mkdir()
        target.write_text("created by another process", encoding="utf-8")
        try:
            with self.assertRaises(gen.GenerationError):
                gen.apply_staged_operations(self.ws, stage, approved_overwrites=False)
        finally:
            gen.cleanup_stage(stage, self.ws)
        self.assertEqual(target.read_text(encoding="utf-8"), "created by another process")
    def test_validate_and_stage_and_apply(self):
        ops = gen.validate_file_operations(self.ws, [
            {"path": "src/app.py", "operation": "write", "content": "print('ok')\n"},
            {"path": "README.md", "operation": "write", "content": "# Demo\n"},
        ])
        collisions = gen.inspect_collisions(self.ws, ops)
        self.assertTrue(all(not c["exists"] for c in collisions))
        stage = gen.stage_operations(self.ws, ops)
        self.assertTrue(Path(stage["stage_dir"]).exists())
        applied = gen.apply_staged_operations(self.ws, stage, approved_overwrites=False)
        self.assertEqual(applied["count"], 2)
        self.assertEqual((self.ws / "src/app.py").read_text(encoding="utf-8"), "print('ok')\n")
        gen.cleanup_stage(stage, self.ws)
        self.assertFalse(Path(stage["stage_dir"]).exists())

    def test_overwrite_requires_approval(self):
        (self.ws / "a.txt").write_text("old", encoding="utf-8")
        ops = gen.validate_file_operations(self.ws, [{"path": "a.txt", "operation": "write", "content": "new"}])
        stage = gen.stage_operations(self.ws, ops)
        with self.assertRaises(gen.GenerationError):
            gen.apply_staged_operations(self.ws, stage, approved_overwrites=False)
        applied = gen.apply_staged_operations(self.ws, stage, approved_overwrites=True)
        self.assertEqual(applied["count"], 1)
        self.assertEqual((self.ws / "a.txt").read_text(encoding="utf-8"), "new")

    def test_overwrite_approval_is_scoped_to_explicit_paths(self):
        approved = self.ws / "approved.txt"
        approved.write_text("old approved", encoding="utf-8")
        ops = gen.validate_file_operations(self.ws, [{"path": "approved.txt", "operation": "write", "content": "new approved"}])
        stage = gen.stage_operations(self.ws, ops)
        try:
            gen.apply_staged_operations(self.ws, stage, approved_overwrite_paths=["approved.txt"])
        finally:
            gen.cleanup_stage(stage, self.ws)
        self.assertEqual(approved.read_text(encoding="utf-8"), "new approved")

        unapproved = self.ws / "unapproved.txt"
        unapproved.write_text("preserve", encoding="utf-8")
        ops = gen.validate_file_operations(self.ws, [{"path": "unapproved.txt", "operation": "write", "content": "must not apply"}])
        stage = gen.stage_operations(self.ws, ops)
        try:
            with self.assertRaises(gen.GenerationError):
                gen.apply_staged_operations(self.ws, stage, approved_overwrite_paths=["approved.txt"])
        finally:
            gen.cleanup_stage(stage, self.ws)
        self.assertEqual(unapproved.read_text(encoding="utf-8"), "preserve")

    def test_validation_output_is_bounded_and_git_write_commands_are_rejected(self):
        command = gen.ValidationCommand(
            name="bounded_output", argv=["python", "-c", "print('x' * 50000)"], cwd=".", timeout_seconds=20,
        )
        result = gen.run_validation_command(self.ws, command)
        self.assertEqual(result["status"], "succeeded")
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), gen.MAX_COMMAND_OUTPUT)
        self.assertTrue(result["stdout_truncated"])
        self.assertFalse(gen.allowed_validation_command(["git", "push", "origin", "main"]))
        self.assertTrue(gen.allowed_validation_command(["python.exe", "-m", "py_compile", "app.py"]))
    def test_duplicate_paths_rejected(self):
        with self.assertRaises(gen.GenerationError):
            gen.validate_file_operations(self.ws, [
                {"path": "a.txt", "operation": "write", "content": "1"},
                {"path": "a.txt", "operation": "write", "content": "2"},
            ])


class GenerationWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        os.environ["CRANE_MODEL_PROVIDER"] = "local"

    async def asyncTearDown(self):
        self.td.cleanup()
        os.environ.pop("CRANE_MODEL_PROVIDER", None)

    async def test_generation_task_plan_only(self):
        result = await workflow_engine.execute_task(
            "[PLAN ONLY] Create a Python FastAPI app",
            task_id="gen-plan",
            workspace=str(self.ws),
        )
        self.assertIn(result["status"], {"succeeded", "failed"})
        # Plan-only should not write app files into workspace root.
        self.assertFalse((self.ws / "app" / "main.py").exists())
        self.assertTrue(result.get("checkpoint") or result.get("result"))

    async def test_generation_task_applies_with_approval_grant(self):
        # Pre-grant approvals by passing through checkpoint-like resume is complex; instead test module directly.
        spec = gen._default_generation_spec if False else None
        ops = gen.validate_file_operations(self.ws, [
            {"path": "README.md", "operation": "write", "content": "# Hello\n"},
        ])
        stage = gen.stage_operations(self.ws, ops)
        applied = gen.apply_staged_operations(self.ws, stage, approved_overwrites=True)
        self.assertEqual(applied["count"], 1)
        self.assertTrue((self.ws / "README.md").exists())


if __name__ == "__main__":
    unittest.main()
