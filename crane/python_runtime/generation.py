"""Workspace-authorized project generation for CRANE.

This module is runtime-owned. The model may propose structured file operations,
but only this module validates paths, stages changes, applies approved writes,
and runs controlled validation commands inside the authorized workspace.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_FILES = 200
MAX_COMMAND_OUTPUT = 32 * 1024
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120
MAX_COMMAND_TIMEOUT_SECONDS = 300
MAX_REPAIR_ATTEMPTS = 3

PROTECTED_NAMES = {
    ".git",
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".ssh",
    "id_rsa",
    "id_ed25519",
    ".npmrc",
    ".pypirc",
    ".crane-staging",
}
PROTECTED_PREFIXES = (".git/", ".git\\", ".ssh/", ".ssh\\")
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class GenerationError(ValueError):
    pass


@dataclass
class FileOperation:
    path: str
    content: str
    operation: str = "write"  # write|delete
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "content": self.content,
            "operation": self.operation,
            "description": self.description,
        }


@dataclass
class ValidationCommand:
    name: str
    argv: List[str]
    cwd: str = "."
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS
    required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "required": self.required,
        }


def _is_windows() -> bool:
    return os.name == "nt"


def _norm_case(path: str) -> str:
    p = os.path.normcase(os.path.normpath(path))
    return p.lower() if _is_windows() else p


def canonicalize_workspace(workspace: str) -> Path:
    if not isinstance(workspace, str) or not workspace.strip():
        raise GenerationError("Workspace path is required.")
    raw = Path(workspace).expanduser()
    try:
        resolved = raw.resolve(strict=False)
    except Exception as exc:  # noqa: BLE001
        raise GenerationError(f"Invalid workspace path: {exc}") from exc
    if not resolved.is_absolute():
        raise GenerationError("Workspace path must resolve to an absolute path.")
    # Require existing directory for safety - creation is explicit user action.
    if not resolved.exists():
        raise GenerationError(f"Workspace does not exist: {resolved}")
    if not resolved.is_dir():
        raise GenerationError(f"Workspace is not a directory: {resolved}")
    # Resolve symlinks/junctions in the workspace itself.
    real = resolved.resolve(strict=True)
    return real


def _check_reserved_name(name: str) -> None:
    base = name.split(".")[0].upper()
    if base in WINDOWS_RESERVED:
        raise GenerationError(f"Reserved Windows filename is not allowed: {name}")


def _is_protected_rel(rel_posix: str) -> bool:
    rel = rel_posix.replace("\\", "/").lstrip("/")
    lower = rel.lower()
    parts = [p for p in lower.split("/") if p]
    if not parts:
        return True
    for part in parts:
        _check_reserved_name(part)
        if part in PROTECTED_NAMES:
            return True
    for prefix in PROTECTED_PREFIXES:
        if lower == prefix[:-1] or lower.startswith(prefix):
            return True
    # Protect common secret files
    if re.search(r"(^|/)(\.?env(\.|$)|.*secret.*|.*credential.*|.*\.pem$|.*\.key$|.*\.pfx$)", lower):
        return True
    return False


def validate_relative_path(workspace: Path, rel_path: str) -> Path:
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise GenerationError("File path must be a non-empty string.")
    if "\x00" in rel_path:
        raise GenerationError("File path contains invalid characters.")
    # Reject absolute paths and drive-prefixed paths.
    p = Path(rel_path)
    if p.is_absolute():
        raise GenerationError(f"Absolute paths are not allowed: {rel_path}")
    if re.match(r"^[A-Za-z]:", rel_path):
        raise GenerationError(f"Drive-prefixed paths are not allowed: {rel_path}")
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        raise GenerationError(f"Absolute paths are not allowed: {rel_path}")

    candidate = (workspace / p).resolve(strict=False)
    ws_real = workspace.resolve(strict=True)
    try:
        rel = candidate.relative_to(ws_real)
    except Exception as exc:  # noqa: BLE001
        raise GenerationError(f"Path escapes workspace: {rel_path}") from exc

    rel_posix = rel.as_posix()
    if rel_posix in {"", "."}:
        raise GenerationError("File path must point to a file inside workspace.")
    if any(part == ".." for part in p.parts):
        raise GenerationError(f"Path traversal is not allowed: {rel_path}")

    # Check every existing parent for symlink/junction escape.
    cur = ws_real
    for part in rel.parts:
        cur = cur / part
        if cur.exists():
            try:
                real_cur = cur.resolve(strict=True)
            except Exception as exc:  # noqa: BLE001
                raise GenerationError(f"Cannot resolve path safely: {rel_path}") from exc
            try:
                real_cur.relative_to(ws_real)
            except Exception as exc:  # noqa: BLE001
                raise GenerationError(f"Path escapes workspace via link/reparse point: {rel_path}") from exc
            # On Windows, reject junctions/symlinks inside workspace for write targets.
            if cur.is_symlink():
                raise GenerationError(f"Symlinked paths are not allowed: {rel_path}")
            if _is_windows() and hasattr(os, "stat"):
                try:
                    st = cur.lstat()
                    attrs = getattr(st, "st_file_attributes", 0)
                    # FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
                    if attrs & 0x0400:
                        raise GenerationError(f"Reparse point/junction paths are not allowed: {rel_path}")
                except GenerationError:
                    raise
                except Exception:
                    pass

    if _is_protected_rel(rel_posix):
        raise GenerationError(f"Protected or sensitive path is not allowed: {rel_path}")
    return candidate


def validate_file_operations(workspace: Path, operations: List[Dict[str, Any]]) -> List[FileOperation]:
    if not isinstance(operations, list) or not operations:
        raise GenerationError("At least one file operation is required.")
    if len(operations) > MAX_FILES:
        raise GenerationError(f"Too many file operations: {len(operations)} > {MAX_FILES}")

    validated: List[FileOperation] = []
    seen: set[str] = set()
    total_bytes = 0
    for idx, raw in enumerate(operations):
        if not isinstance(raw, dict):
            raise GenerationError(f"File operation {idx} must be an object.")
        op = str(raw.get("operation") or "write").lower().strip()
        if op not in {"write", "delete"}:
            raise GenerationError(f"Unsupported operation for {raw.get('path')!r}: {op}")
        rel_path = raw.get("path")
        if not isinstance(rel_path, str):
            raise GenerationError(f"File operation {idx} path must be a string.")
        target = validate_relative_path(workspace, rel_path)
        rel_norm = _norm_case(str(target.relative_to(workspace.resolve(strict=True))))
        if rel_norm in seen:
            raise GenerationError(f"Duplicate file path in operations: {rel_path}")
        seen.add(rel_norm)

        content = raw.get("content", "")
        if op == "delete":
            content = ""
        if not isinstance(content, str):
            raise GenerationError(f"File content must be a string: {rel_path}")
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > MAX_FILE_BYTES:
            raise GenerationError(f"File exceeds size limit ({MAX_FILE_BYTES} bytes): {rel_path}")
        total_bytes += content_bytes
        if total_bytes > MAX_TOTAL_BYTES:
            raise GenerationError(f"Total generated content exceeds limit ({MAX_TOTAL_BYTES} bytes).")
        desc = raw.get("description")
        validated.append(FileOperation(
            path=target.relative_to(workspace.resolve(strict=True)).as_posix(),
            content=content,
            operation=op,
            description=str(desc)[:300] if isinstance(desc, str) else "",
        ))
    return validated


def inspect_collisions(workspace: Path, ops: List[FileOperation]) -> List[Dict[str, Any]]:
    ws = workspace.resolve(strict=True)
    collisions = []
    for op in ops:
        target = (ws / op.path).resolve(strict=False)
        exists = target.exists()
        is_file = target.is_file() if exists else False
        is_dir = target.is_dir() if exists else False
        collisions.append({
            "path": op.path,
            "operation": op.operation,
            "exists": exists,
            "is_file": is_file,
            "is_dir": is_dir,
            "requires_approval": bool(exists or op.operation == "delete"),
        })
    return collisions


def stage_operations(workspace: Path, ops: List[FileOperation]) -> Dict[str, Any]:
    ws = workspace.resolve(strict=True)
    stage_root = ws / ".crane-staging"
    stage_root.mkdir(parents=True, exist_ok=True)
    stamp = str(int(time.time() * 1000))
    stage_dir = stage_root / stamp
    stage_dir.mkdir(parents=True, exist_ok=False)
    staged = []
    for op in ops:
        target = validate_relative_path(ws, op.path)
        rel = target.relative_to(ws).as_posix()
        record: Dict[str, Any] = {"path": rel, "operation": op.operation, "description": op.description}
        if op.operation == "write":
            staged_path = stage_dir / rel
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            staged_path.write_text(op.content, encoding="utf-8")
            record["staged_path"] = str(staged_path)
            record["bytes"] = len(op.content.encode("utf-8"))
        else:
            record["staged_path"] = None
            record["bytes"] = 0
        staged.append(record)
    return {
        "stage_dir": str(stage_dir),
        "created_at": time.time(),
        "files": staged,
    }


def summarize_changes(workspace: Path, ops: List[FileOperation]) -> Dict[str, Any]:
    ws = workspace.resolve(strict=True)
    summary = {"creates": [], "overwrites": [], "deletes": [], "protected_blocked": []}
    for op in ops:
        target = (ws / op.path).resolve(strict=False)
        exists = target.exists()
        if op.operation == "delete":
            summary["deletes"].append(op.path)
        elif exists:
            summary["overwrites"].append(op.path)
        else:
            summary["creates"].append(op.path)
    return summary


def apply_staged_operations(workspace: Path, stage: Dict[str, Any], approved_overwrites: bool = False) -> Dict[str, Any]:
    ws = workspace.resolve(strict=True)
    stage_dir = Path(str(stage.get("stage_dir") or ""))
    if not stage_dir.exists() or not stage_dir.is_dir():
        raise GenerationError("Staging directory is missing.")
    # Ensure stage dir is inside workspace staging root.
    stage_root = (ws / ".crane-staging").resolve(strict=True)
    try:
        stage_dir.resolve(strict=True).relative_to(stage_root)
    except Exception as exc:  # noqa: BLE001
        raise GenerationError("Invalid staging directory.") from exc

    applied = []
    files = stage.get("files") if isinstance(stage.get("files"), list) else []
    for rec in files:
        if not isinstance(rec, dict):
            continue
        rel = str(rec.get("path") or "")
        op = str(rec.get("operation") or "write")
        target = validate_relative_path(ws, rel)
        exists = target.exists()
        if exists and not approved_overwrites:
            raise GenerationError(f"Overwrite approval required: {rel}")
        if op == "delete":
            if exists and target.is_file():
                target.unlink()
                applied.append({"path": rel, "operation": "delete", "status": "deleted"})
            elif exists:
                raise GenerationError(f"Refusing to delete non-file path: {rel}")
            else:
                applied.append({"path": rel, "operation": "delete", "status": "missing"})
            continue
        staged_path = Path(str(rec.get("staged_path") or ""))
        if not staged_path.exists():
            raise GenerationError(f"Staged file missing: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace via temp file in same directory.
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".crane-tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(staged_path.read_bytes())
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, str(target))
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
            except Exception:
                pass
        applied.append({"path": rel, "operation": "write", "status": "written", "bytes": rec.get("bytes", 0)})
    return {"applied": applied, "count": len(applied)}


def cleanup_stage(stage: Dict[str, Any]) -> None:
    stage_dir = Path(str(stage.get("stage_dir") or ""))
    if stage_dir.exists() and stage_dir.is_dir() and stage_dir.name.isdigit():
        try:
            shutil.rmtree(stage_dir, ignore_errors=True)
        except Exception:
            pass


def allowed_validation_command(argv: List[str]) -> bool:
    if not argv or not isinstance(argv, list):
        return False
    cmd = os.path.basename(str(argv[0])).lower()
    allowed = {
        "python", "python3", "py",
        "pytest",
        "node", "npm", "npx",
        "go", "gofmt",
        "git",  # read-only status/diff checks; execution layer can further restrict
        "cargo",
        "dotnet",
    }
    return cmd in allowed


def run_validation_command(workspace: Path, command: ValidationCommand) -> Dict[str, Any]:
    ws = workspace.resolve(strict=True)
    argv = [str(a) for a in command.argv if str(a)]
    if not argv:
        return {"name": command.name, "status": "failed", "error": "empty command"}
    if not allowed_validation_command(argv):
        return {"name": command.name, "status": "failed", "error": f"command not allowed: {argv[0]}"}
    # Prevent shell execution; use argument array only.
    cwd = validate_relative_path(ws, command.cwd) if command.cwd not in {"", "."} else ws
    if cwd.exists() and cwd.is_file():
        return {"name": command.name, "status": "failed", "error": "cwd must be a directory"}
    cwd.mkdir(parents=True, exist_ok=True)

    timeout = max(1, min(int(command.timeout_seconds or DEFAULT_COMMAND_TIMEOUT_SECONDS), MAX_COMMAND_TIMEOUT_SECONDS))
    env = os.environ.copy()
    # Avoid leaking common secret env vars to generated project checks where possible? Keep minimal; don't blanket remove PATH.
    for k in list(env.keys()):
        if re.search(r"(TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY)", k, re.IGNORECASE):
            env.pop(k, None)

    start = time.time()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            shell=False,
        )
        stdout = (proc.stdout or "")[:MAX_COMMAND_OUTPUT]
        stderr = (proc.stderr or "")[:MAX_COMMAND_OUTPUT]
        return {
            "name": command.name,
            "argv": argv,
            "status": "succeeded" if proc.returncode == 0 else "failed",
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": int((time.time() - start) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return {
            "name": command.name,
            "argv": argv,
            "status": "failed",
            "error": f"timeout after {timeout}s",
            "stdout": stdout[:MAX_COMMAND_OUTPUT],
            "stderr": stderr[:MAX_COMMAND_OUTPUT],
            "duration_ms": int((time.time() - start) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": command.name,
            "argv": argv,
            "status": "failed",
            "error": str(exc),
            "duration_ms": int((time.time() - start) * 1000),
        }


def default_validation_commands(workspace: Path) -> List[ValidationCommand]:
    ws = workspace.resolve(strict=True)
    cmds: List[ValidationCommand] = []
    if (ws / "pyproject.toml").exists() or (ws / "requirements.txt").exists() or any(ws.rglob("*.py")):
        # Syntax check only; no install.
        py_files = [p.relative_to(ws).as_posix() for p in ws.rglob("*.py") if ".crane-staging" not in p.parts][:50]
        if py_files:
            cmds.append(ValidationCommand(name="python_syntax", argv=[os.environ.get("PYTHON", "python"), "-m", "py_compile", *py_files], timeout_seconds=120))
        if (ws / "tests").exists() or any(ws.rglob("test_*.py")):
            cmds.append(ValidationCommand(name="pytest", argv=[os.environ.get("PYTHON", "python"), "-m", "pytest", "-q"], timeout_seconds=180, required=False))
    if (ws / "package.json").exists():
        cmds.append(ValidationCommand(name="npm_test", argv=["npm", "test", "--", "--runInBand"], timeout_seconds=180, required=False))
    if (ws / "go.mod").exists():
        cmds.append(ValidationCommand(name="go_test", argv=["go", "test", "./..."], timeout_seconds=180, required=False))
    return cmds
