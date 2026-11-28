import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_normalizes_common_write_aliases_but_rejects_unknown_operations(self):
        ops = gen.validate_file_operations(self.ws, [
            {"path": "main.py", "operation": "create", "content": "print('ok')\n"},
        ])
        self.assertEqual(ops[0].operation, "write")
        with self.assertRaises(gen.GenerationError):
            gen.validate_file_operations(self.ws, [
                {"path": "main.py", "operation": "chmod", "content": ""},
            ])

    def test_rejects_windows_device_ads_and_invalid_names_on_every_host(self):
        invalid_paths = [
            r"C:relative.txt", r"\\server\share\file.txt", r"\\?\C:\Windows\win.ini",
            "src/file.txt:secret", "folder/NUL.txt", "folder/COM1.log",
            "folder/COM0", "folder/LPT0.txt", "folder/CONIN$", "folder/CONOUT$.log",
            "folder/COM¹", "folder/name. ", "folder/name.", "folder/bad?.txt",
            "folder/../escape.txt",
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
        self.assertEqual(
            result["status"],
            "succeeded",
            msg={
                "error": result.get("error"),
                "exit_code": result.get("exit_code"),
                "stdout": result.get("stdout", "")[:500],
                "stderr": result.get("stderr", "")[:500],
            },
        )
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), gen.MAX_COMMAND_OUTPUT)
        self.assertTrue(result["stdout_truncated"])
        self.assertFalse(gen.allowed_validation_command(["git", "push", "origin", "main"]))
        self.assertTrue(gen.allowed_validation_command(["python.exe", "-m", "py_compile", "app.py"]))

    def test_bare_pytest_is_pinned_to_runtime_python_module(self):
        resolved = gen._resolved_validation_argv(["pytest", "-q", "tests.py"])
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved[:3], [sys.executable, "-m", "pytest"])
        self.assertEqual(resolved[3:], ["-q", "tests.py"])
        shim = self.ws / ("pytest.exe" if os.name == "nt" else "pytest")
        shim.write_text("", encoding="utf-8")
        self.assertFalse(gen.allowed_validation_command([str(shim), "-q"]))
        self.assertEqual(gen._validation_environment()["PYTEST_DISABLE_PLUGIN_AUTOLOAD"], "1")

    def test_model_json_extracts_first_balanced_object_ignoring_prose(self):
        response = (
            "```json\n"
            '{"files": [{"path": "main.py", "operation": "write"}]}\n'
            "```\n"
            "Explanation: use {\"files\": []} instead.\n"
            "```python\nprint({'broken': True})\n```\n"
        )
        parsed = workflow_engine._parse_model_json(response)
        self.assertEqual(parsed, {"files": [{"path": "main.py", "operation": "write"}]})

    def test_model_json_rejects_unbalanced_and_non_object_output(self):
        self.assertIsNone(workflow_engine._parse_model_json("no json here"))
        self.assertIsNone(workflow_engine._parse_model_json('{"files": ['))

    def test_validation_executable_is_pinned_and_python_env_is_ignored(self):
        shim_dir = self.ws / "shim"
        shim_dir.mkdir()
        shim = shim_dir / ("python.exe" if os.name == "nt" else "python")
        marker = shim_dir / "executed"
        if os.name == "nt":
            shim.write_text(f"echo hijacked > \"{marker}\"\n", encoding="utf-8")
        else:
            shim.write_text(f"#!/bin/sh\necho hijacked > {marker}\n", encoding="utf-8")
        previous_path = os.environ.get("PATH", "")
        previous_python = os.environ.get("PYTHON")
        os.environ["PATH"] = str(shim_dir) + os.pathsep + previous_path
        os.environ["PYTHON"] = str(shim)
        try:
            command = gen.ValidationCommand(
                name="pinned_python",
                argv=["python", "-c", "import sys; print(sys.executable)"],
                cwd=".",
                timeout_seconds=20,
            )
            result = gen.run_validation_command(self.ws, command)
            self.assertEqual(result["status"], "succeeded", result)
            self.assertEqual(Path(result["stdout"].strip()).resolve(), Path(sys.executable).resolve())
            self.assertFalse(marker.exists())
            self.assertFalse(gen.allowed_validation_command([str(shim), "-c", "pass"]))
            (self.ws / "app.py").write_text("print('ok')\n", encoding="utf-8")
            for cmd in gen.default_validation_commands(self.ws):
                if cmd.name in {"python_syntax", "pytest"}:
                    self.assertEqual(Path(cmd.argv[0]).resolve(), Path(sys.executable).resolve())
        finally:
            os.environ["PATH"] = previous_path
            if previous_python is None:
                os.environ.pop("PYTHON", None)
            else:
                os.environ["PYTHON"] = previous_python

    def test_default_fastapi_manifest_is_sqlite_backed_and_validates(self):
        objective = (
            "Create a small FastAPI application backed by SQLite. Implement a health endpoint "
            "and a basic CRUD endpoint. Include appropriate validation and automated tests."
        )
        state = workflow_engine.WorkflowState(
            task_id="fastapi-default", user_input=objective, objective=objective,
            workspace=str(self.ws),
        )
        spec = workflow_engine._default_generation_spec(state)
        manifest = workflow_engine._default_generation_manifest(state, spec)
        self.assertTrue(any(item["path"] == "tests/test_app.py" for item in manifest))

        ops = gen.validate_file_operations(self.ws, manifest)
        for op in ops:
            target = gen.validate_relative_path(self.ws, op.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(op.content, encoding="utf-8")

        source = (self.ws / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("import sqlite3", source)
        self.assertIn("CREATE TABLE IF NOT EXISTS tasks", source)
        self.assertNotIn("_tasks: dict", source)
        self.assertNotIn("_tasks =", source)
        self.assertIn("{'status': 'ok'}", source)

        results = [
            gen.run_validation_command(self.ws, cmd)
            for cmd in gen.default_validation_commands(self.ws)
        ]
        self.assertTrue(any(r.get("name") == "pytest" for r in results), results)
        for result in results:
            self.assertEqual(result["status"], "succeeded", result)

    def test_validation_environment_is_minimal_and_output_is_redacted(self):
        previous = {key: os.environ.get(key) for key in ("MY_CREDENTIAL", "DATABASE_URL")}
        os.environ["MY_CREDENTIAL"] = "canary-credential"
        os.environ["DATABASE_URL"] = "postgres://canary-host/db"
        try:
            command = gen.ValidationCommand(
                name="scrubbed_env",
                argv=["python", "-c", "import os; print(os.environ.get('MY_CREDENTIAL', '')); print(os.environ.get('DATABASE_URL', '')); print('SECRET_CANARY=canary-secret')"],
                cwd=".",
                timeout_seconds=20,
            )
            result = gen.run_validation_command(self.ws, command)
            self.assertEqual(result["status"], "succeeded", result)
            self.assertNotIn("canary-credential", result["stdout"])
            self.assertNotIn("canary-host", result["stdout"])
            self.assertNotIn("canary-secret", result["stdout"])
            self.assertEqual(os.environ["MY_CREDENTIAL"], "canary-credential")
            self.assertEqual(os.environ["DATABASE_URL"], "postgres://canary-host/db")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_stage_manifest_tampering_is_rejected(self):
        ops = gen.validate_file_operations(self.ws, [{"path": "app.py", "operation": "write", "content": "safe"}])
        stage = gen.stage_operations(self.ws, ops)
        extra = self.ws / ".crane-staging" / Path(stage["stage_dir"]).name / "extra.txt"
        extra.write_text("not approved", encoding="utf-8")
        stage["files"].append({
            "path": "extra.txt", "operation": "write", "staged_path": str(extra), "bytes": 12,
        })
        try:
            with self.assertRaises(gen.GenerationError):
                gen.apply_staged_operations(self.ws, stage)
            self.assertFalse((self.ws / "extra.txt").exists())
            self.assertFalse(gen.cleanup_stage(stage, self.ws))
        finally:
            gen._remove_validated_stage_dir(Path(stage["stage_dir"]))

    def test_validation_timeout_terminates_process_tree(self):
        if os.name != "nt":
            self.skipTest("Windows process-tree termination")
        marker = self.ws / "child-marker.txt"
        child = f"import time; time.sleep(3); open({str(marker)!r}, 'w').write('alive')"
        script = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
        command = gen.ValidationCommand(
            name="tree_timeout", argv=["python", "-c", script], cwd=".", timeout_seconds=1,
        )
        result = gen.run_validation_command(self.ws, command)
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("timeout", result.get("error", ""))
        time.sleep(3.5)
        self.assertFalse(marker.exists(), "timed-out descendant process survived")

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

    async def test_generation_intent_without_workspace_fails_honestly(self):
        with mock.patch("crane.python_runtime.protocol.write_stdout"):
            result = await workflow_engine.execute_task(
                "Create a Python project with tests and repair any failures",
                task_id="gen-missing-workspace",
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result.get("execution_status"), "failed")
        payload = result.get("result") or {}
        self.assertNotEqual(payload.get("execution_status"), "fully_completed")
        self.assertEqual(payload.get("plan"), [])
        reason = result.get("failure_reason") or ""
        self.assertIn("--workspace", reason)
        self.assertIn("No files were created", reason)

    async def test_generation_intent_with_workspace_reaches_generation_plan(self):
        state = workflow_engine.WorkflowState(
            task_id="gen-dispatch", user_input="Create a Python project with tests and repair failures", workspace=str(self.ws),
        )
        with mock.patch("crane.python_runtime.protocol.write_stdout"):
            normalized = await workflow_engine.node_normalize(workflow_engine.state_to_checkpoint(state))
            planned = await workflow_engine.node_plan(normalized)
        planned_state = workflow_engine._as_state(planned)
        step_ids = [step.step_id for step in planned_state.plan]
        for expected in ("analyze_requirements", "generate_files", "validate_project", "repair_project"):
            self.assertIn(expected, step_ids)
        self.assertNotIn("respond", step_ids)
        self.assertTrue(planned_state.generation.get("enabled"))

    async def test_generation_completion_requires_file_and_validation_evidence(self):
        (self.ws / "README.md").write_text("# verified\n", encoding="utf-8")
        applied = [{"path": "README.md", "operation": "write", "status": "written", "bytes": 10}]
        validation = {"name": "pytest", "status": "succeeded", "exit_code": 0, "stdout": "1 passed"}

        def state_with(records, validations):
            state = workflow_engine.WorkflowState(
                task_id="gen-evidence", user_input="Create a Python project with tests", workspace=str(self.ws),
            )
            state.generation["enabled"] = True
            state.generation["applied"] = records
            state.generation["validation_results"] = validations
            state.plan = [workflow_engine.PlanStep(
                step_id="respond", description="respond", expected_outcome="respond", status="completed",
            )]
            state.completed_steps = ["respond"]
            state.evaluation_results = [workflow_engine.EvaluationResult(
                step_id="respond", outcome="completed", summary="model said done",
            )]
            return state

        async def verify(state):
            with mock.patch("crane.python_runtime.protocol.write_stdout"):
                result = await workflow_engine.node_verify(workflow_engine.state_to_checkpoint(state))
            return workflow_engine._as_state(result)

        without_evidence = await verify(state_with([], []))
        self.assertNotEqual(without_evidence.execution_status, workflow_engine.ExecutionStatus.FULLY_COMPLETED.value)
        self.assertIn("workspace", (without_evidence.failure_reason or "").lower())

        without_validation = await verify(state_with(applied, []))
        self.assertNotEqual(without_validation.execution_status, workflow_engine.ExecutionStatus.FULLY_COMPLETED.value)
        self.assertIn("validation", (without_validation.failure_reason or "").lower())

        with_evidence = await verify(state_with(applied, [validation]))
        self.assertEqual(with_evidence.execution_status, workflow_engine.ExecutionStatus.FULLY_COMPLETED.value)

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

    async def test_plan_prompt_states_exact_allowed_operations(self):
        state, step = self._state_and_step("plan_project")
        prompts = []

        async def capture_prompt(current_state, prompt, system=None):
            prompts.append(prompt)
            return {
                "files": [{"path": "main.py", "operation": "write", "content": "print('ok')\n"}],
                "validation_commands": [],
                "notes": [],
            }

        with mock.patch.dict(os.environ, {"CRANE_MODEL_PROVIDER": "ollama"}), \
                mock.patch.object(workflow_engine, "_model_json", new=capture_prompt), \
                mock.patch("crane.python_runtime.protocol.write_stdout"):
            result = await workflow_engine._exec_generation_step(state, step)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(prompts), 1)
        self.assertIn('"write"', prompts[0])
        self.assertIn('"delete"', prompts[0])

    def test_repair_step_is_runnable_after_failed_validation(self):
        state = workflow_engine.WorkflowState(
            task_id="repair-dependency", user_input="test", workspace=str(self.ws),
        )
        state.plan = workflow_engine._default_generation_plan(state)
        for step in state.plan:
            if step.step_id in {"analyze_requirements", "plan_project", "generate_files"}:
                step.status = "completed"
            elif step.step_id == "validate_project":
                step.status = "failed"
            else:
                step.status = "pending"
        workflow_engine._plan_status_sync(state)
        runnable = workflow_engine._next_runnable_step(state)
        self.assertIsNotNone(runnable)
        self.assertEqual(runnable.step_id, "repair_project")

    def test_graph_step_budget_covers_generation_approval_cycles(self):
        self.assertGreaterEqual(workflow_engine.MAX_GRAPH_STEPS, 4 * len(workflow_engine._default_generation_plan(
            workflow_engine.WorkflowState(task_id="budget", user_input="test", workspace=str(self.ws))
        )))

    async def test_failed_generation_validation_routes_to_repair(self):
        state = workflow_engine.WorkflowState(
            task_id="validation-recovery", user_input="test", workspace=str(self.ws),
        )
        state.generation["enabled"] = True
        state.plan = workflow_engine._default_generation_plan(state)
        for step in state.plan:
            if step.step_id in {"analyze_requirements", "plan_project", "generate_files", "validate_project"}:
                step.status = "completed" if step.step_id != "validate_project" else "failed"
            else:
                step.status = "pending"
        workflow_engine._plan_status_sync(state)
        state.current_step_id = "validate_project"
        state.evaluation_results = [workflow_engine.EvaluationResult(
            step_id="validate_project", outcome="failed", summary="pytest failed", next_action="recover",
        )]
        with mock.patch("crane.python_runtime.protocol.write_stdout"):
            result = await workflow_engine.node_recover(workflow_engine.state_to_checkpoint(state))
        recovered = workflow_engine._as_state(result)
        validate = next(s for s in recovered.plan if s.step_id == "validate_project")
        self.assertEqual(validate.status, "failed")
        runnable = workflow_engine._next_runnable_step(recovered)
        self.assertIsNotNone(runnable)
        self.assertEqual(runnable.step_id, "repair_project")

    def test_terminal_failure_clears_pending_approval(self):
        state = workflow_engine.WorkflowState(
            task_id="terminal-approval", user_input="test", workspace=str(self.ws),
        )
        state.execution_status = workflow_engine.ExecutionStatus.WAITING_FOR_APPROVAL.value
        state.generation["pending_approval"] = {
            "scope": "generation:repair:terminal-approval:abc123", "reason": "Approve repair",
        }
        workflow_engine._mark_terminal(
            state, workflow_engine.ExecutionStatus.FAILED, "Repair failed", "graph step limit",
        )
        self.assertEqual(state.execution_status, workflow_engine.ExecutionStatus.FAILED.value)
        self.assertNotIn("pending_approval", state.generation)

    def _state_and_step(self, step_id):
        state = workflow_engine.WorkflowState(
            task_id="approval-task", user_input="Create a project", workspace=str(self.ws),
        )
        step = workflow_engine.PlanStep(
            step_id=step_id, description="test step", expected_outcome="test outcome",
        )
        return state, step

    async def test_generate_files_requires_exact_digest_bound_scope(self):
        state, step = self._state_and_step("generate_files")
        ops = gen.validate_file_operations(self.ws, [
            {"path": "README.md", "operation": "write", "content": "# Hello\n"},
        ])
        state.generation["manifest"] = [op.to_dict() for op in ops]
        wrong_scope = "generation:apply:approval-task:deadbeefdeadbeef"
        state.granted_approvals = [wrong_scope]
        with mock.patch("crane.python_runtime.protocol.write_stdout"):
            blocked = await workflow_engine._exec_generation_step(state, step)
            self.assertEqual(blocked["status"], "blocked", blocked)
            scope = blocked["approval_scope"]
            self.assertNotEqual(scope, wrong_scope)
            self.assertEqual(state.generation["pending_approval"]["scope"], scope)
            self.assertFalse((self.ws / "README.md").exists())

            state.granted_approvals = [scope]
            state.generation["approvals_granted"] = [scope]
            applied = await workflow_engine._exec_generation_step(state, step)
            self.assertEqual(applied["status"], "succeeded", applied)
            self.assertEqual((self.ws / "README.md").read_text(encoding="utf-8"), "# Hello\n")
            self.assertNotIn(scope, state.granted_approvals)

            replayed = await workflow_engine._exec_generation_step(state, step)
            self.assertEqual(replayed["status"], "blocked", replayed)
            self.assertNotEqual(replayed["approval_scope"], scope)

    async def test_validation_requires_exact_command_digest_scope(self):
        state, step = self._state_and_step("validate_project")
        state.generation["validation_plan"] = [{
            "name": "safe_check", "argv": ["python", "-c", "print('validated')"],
            "cwd": ".", "timeout_seconds": 20, "required": True,
        }]
        wrong_scope = "generation:validate:approval-task:deadbeefdeadbeef"
        state.granted_approvals = [wrong_scope]
        with mock.patch("crane.python_runtime.protocol.write_stdout"):
            blocked = await workflow_engine._exec_generation_step(state, step)
            self.assertEqual(blocked["status"], "blocked", blocked)
            scope = blocked["approval_scope"]
            self.assertNotEqual(scope, wrong_scope)
            self.assertEqual(state.generation["pending_approval"]["scope"], scope)

            state.granted_approvals = [scope]
            state.generation["approvals_granted"] = [scope]
            ok = await workflow_engine._exec_generation_step(state, step)
            self.assertEqual(ok["status"], "succeeded", ok)
            self.assertIn("validated", ok["output"]["validation_results"][0]["stdout"])
            self.assertNotIn(scope, state.granted_approvals)

            replayed = await workflow_engine._exec_generation_step(state, step)
            self.assertEqual(replayed["status"], "blocked", replayed)
            self.assertEqual(replayed["approval_scope"], scope)
            self.assertNotIn(scope, state.granted_approvals)

    async def test_validation_failure_event_carries_actual_output(self):
        state, step = self._state_and_step("validate_project")
        state.generation["validation_plan"] = [{
            "name": "failing_check",
            "argv": ["python", "-c", "import sys; print('VALIDATION_FAILURE_DETAIL'); sys.exit(3)"],
            "cwd": ".", "timeout_seconds": 20, "required": True,
        }]
        with mock.patch("crane.python_runtime.protocol.write_stdout") as write:
            blocked = await workflow_engine._exec_generation_step(state, step)
            scope = blocked["approval_scope"]
            state.granted_approvals = [scope]
            state.generation["approvals_granted"] = [scope]
            result = await workflow_engine._exec_generation_step(state, step)

        self.assertEqual(result["status"], "failed", result)
        validation = result["output"]["validation_results"][0]
        self.assertEqual(validation["exit_code"], 3)
        events = [
            call.args[0] for call in write.call_args_list
            if call.args and isinstance(call.args[0], dict)
            and call.args[0].get("event_type") == "validation.completed"
        ]
        self.assertTrue(events)
        self.assertEqual(events[-1]["payload"]["exit_code"], 3)
        self.assertIn("VALIDATION_FAILURE_DETAIL", events[-1]["payload"]["stdout"])

    async def test_repair_prompt_includes_current_source_and_redacts_secrets(self):
        state, step = self._state_and_step("repair_project")
        state.objective = "Repair the generated FastAPI application"
        self.ws.joinpath("app").mkdir()
        files = [
            {
                "path": "app/main.py",
                "operation": "write",
                "content": "from app.helpers import build_app\nAPI_KEY = 'canary-secret-value'\n",
            },
            {
                "path": "app/helpers.py",
                "operation": "write",
                "content": "def build_app():\n    return 'ok'\n",
            },
            {"path": "app/__init__.py", "operation": "write", "content": ""},
        ]
        ops = gen.validate_file_operations(self.ws, files)
        for op in ops:
            target = gen.validate_relative_path(self.ws, op.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(op.content, encoding="utf-8")
        state.generation["manifest"] = [op.to_dict() for op in ops]
        state.generation["applied"] = [
            {"path": op.path, "operation": "write", "status": "written"} for op in ops
        ]
        state.generation["validation_results"] = [{
            "name": "pytest",
            "status": "failed",
            "stderr": "ModuleNotFoundError: No module named 'app.helpers'",
        }]
        prompts = []

        async def capture_prompt(current_state, prompt, system=None):
            prompts.append(prompt)
            return {
                "files": [{
                    "path": "app/main.py",
                    "operation": "write",
                    "description": "Repair the failing import",
                    "content": "from app.helpers import build_app\n",
                }],
            }

        with mock.patch.dict(os.environ, {"CRANE_MODEL_PROVIDER": "ollama"}), \
                mock.patch.object(workflow_engine, "_model_json", new=capture_prompt), \
                mock.patch("crane.python_runtime.protocol.write_stdout"):
            result = await workflow_engine._exec_generation_step(state, step)

        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(len(prompts), 1)
        self.assertIn("ModuleNotFoundError", prompts[0])
        self.assertIn("from app.helpers import build_app", prompts[0])
        self.assertIn("def build_app()", prompts[0])
        self.assertIn("[redacted]", prompts[0])
        self.assertNotIn("canary-secret-value", prompts[0])
        self.assertIn("package __init__.py", prompts[0])

    async def test_repair_requires_exact_manifest_digest_and_never_regenerates_under_grant(self):
        state, step = self._state_and_step("repair_project")
        state.generation["validation_results"] = [{"name": "test", "status": "failed"}]
        ops = gen.validate_file_operations(self.ws, [
            {"path": "app.py", "operation": "write", "content": "fixed = True\n"},
        ])
        normalized = [op.to_dict() for op in ops]
        state.generation["pending_repair_manifest"] = normalized
        with mock.patch("crane.python_runtime.protocol.write_stdout"), \
                mock.patch.object(workflow_engine, "_model_json", new=mock.AsyncMock(side_effect=AssertionError("model called under grant"))):
            blocked = await workflow_engine._exec_generation_step(state, step)
            self.assertEqual(blocked["status"], "blocked", blocked)
            scope = blocked["approval_scope"]
            self.assertIn("generation:repair:approval-task:", scope)
            self.assertEqual(state.generation["pending_repair_digest"], scope.rsplit(":", 1)[1])
            self.assertFalse((self.ws / "app.py").exists())

            state.granted_approvals = [scope]
            state.generation["approvals_granted"] = [scope]
            repaired = await workflow_engine._exec_generation_step(state, step)
            self.assertEqual(repaired["status"], "succeeded", repaired)
            self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"), "fixed = True\n")
            self.assertNotIn(scope, state.granted_approvals)

    async def test_repair_emits_real_apply_and_revalidation_evidence(self):
        state, step = self._state_and_step("repair_project")
        state.generation["validation_results"] = [{
            "name": "pytest", "status": "failed", "exit_code": 1,
            "stdout": "1 failed", "stderr": "AssertionError: expected ok",
        }]
        ops = gen.validate_file_operations(self.ws, [
            {"path": "app.py", "operation": "write", "content": "status = 'ok'\n"},
        ])
        state.generation["pending_repair_manifest"] = [op.to_dict() for op in ops]

        with mock.patch("crane.python_runtime.protocol.write_stdout") as write:
            blocked = await workflow_engine._exec_generation_step(state, step)
            self.assertEqual(blocked["status"], "blocked", blocked)
            scope = blocked["approval_scope"]
            state.granted_approvals = [scope]
            state.generation["approvals_granted"] = [scope]
            repaired = await workflow_engine._exec_generation_step(state, step)

        self.assertEqual(repaired["status"], "succeeded", repaired)
        events = [
            call.args[0] for call in write.call_args_list
            if call.args and isinstance(call.args[0], dict)
            and call.args[0].get("type") == "task_event"
        ]
        written = [e for e in events if e.get("event_type") == "generation.file.written"]
        self.assertTrue(any(e["payload"].get("path") == "app.py" for e in written), events)
        repair_applied = [e for e in events if e.get("event_type") == "repair.applied"]
        self.assertEqual(repair_applied[-1]["payload"]["files"], ["app.py"])
        repair_completed = [e for e in events if e.get("event_type") == "repair.completed"]
        self.assertEqual(repair_completed[-1]["payload"]["status"], "succeeded")
        self.assertEqual(repair_completed[-1]["payload"]["exit_codes"], [0])
        validated = [
            e for e in events if e.get("event_type") == "validation.completed"
            and e["payload"].get("repair_attempt") == 1
        ]
        self.assertTrue(validated, events)
        self.assertEqual(validated[-1]["payload"]["exit_code"], 0)
        self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"), "status = 'ok'\n")

    async def test_tampered_repair_manifest_is_rejected_after_approval(self):
        state, step = self._state_and_step("repair_project")
        state.generation["validation_results"] = [{"name": "test", "status": "failed"}]
        ops = gen.validate_file_operations(self.ws, [
            {"path": "app.py", "operation": "write", "content": "approved = True\n"},
        ])
        state.generation["pending_repair_manifest"] = [op.to_dict() for op in ops]
        with mock.patch("crane.python_runtime.protocol.write_stdout"):
            blocked = await workflow_engine._exec_generation_step(state, step)
            scope = blocked["approval_scope"]
            state.granted_approvals = [scope]
            state.generation["approvals_granted"] = [scope]
            state.generation["pending_repair_manifest"][0]["content"] = "tampered = True\n"
            result = await workflow_engine._exec_generation_step(state, step)
            self.assertEqual(result["status"], "failed", result)
            self.assertIn("changed after approval", result["error"])
            self.assertFalse((self.ws / "app.py").exists())

    async def test_task_env_is_restored_and_resume_workspace_cannot_be_redirected(self):
        original = os.environ.get("CRANE_TEST_ENV_CANARY")
        checkpoint = workflow_engine.state_to_checkpoint(
            workflow_engine.WorkflowState(task_id="resume-task", user_input="test", workspace=str(self.ws))
        )
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        try:
            with mock.patch("crane.python_runtime.protocol.write_stdout"):
                await workflow_engine.execute_task(
                    "echo test", task_id="env-task", env={"CRANE_TEST_ENV_CANARY": "injected"},
                    workspace=str(self.ws),
                )
            self.assertIsNone(os.environ.get("CRANE_TEST_ENV_CANARY"))
            with self.assertRaises(ValueError):
                await workflow_engine.execute_task(
                    "echo test", task_id="resume-task", resume=True,
                    checkpoint=checkpoint, workspace=other.name,
                )
        finally:
            if original is None:
                os.environ.pop("CRANE_TEST_ENV_CANARY", None)
            else:
                os.environ["CRANE_TEST_ENV_CANARY"] = original


if __name__ == "__main__":
    unittest.main()
