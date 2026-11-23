"""Workspace-authorized project generation for CRANE.

This module is runtime-owned. The model may propose structured file operations,
but only this module validates paths, stages changes, applies approved writes,
and runs controlled validation commands inside the authorized workspace.
"""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
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
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "COM0", "LPT0",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
    "COM¹", "COM²", "COM³",
}
MAX_VALIDATION_ARGV = 64
MAX_VALIDATION_ARG_CHARS = 4096
VALIDATION_ENV_KEYS = (
    "SYSTEMROOT", "SystemRoot", "PATH", "Path", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONUTF8",
)
_REDACTION_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(token|secret|password|api[_-]?key|credential|database_url|credential_file)[A-Z0-9_]*\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;&}\)]+)"),
)

# Executable locations are captured once at runtime startup. A later PATH change
# cannot redirect an allowlisted validation command to a hostile shim.
_TRUSTED_PATH = os.environ.get("PATH", "")
_TRUSTED_EXECUTABLES: Dict[str, Path] = {name: Path(sys.executable).resolve() for name in ("python", "python3", "py")}
for _tool in ("node", "npm", "npx", "go", "gofmt", "cargo", "dotnet"):
    _located = shutil.which(_tool, path=_TRUSTED_PATH)
    if _located and Path(_located).suffix.lower() not in {".cmd", ".bat", ".ps1", ".sh"}:
        try:
            _TRUSTED_EXECUTABLES[_tool] = Path(_located).resolve(strict=True)
        except OSError:
            pass


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
    base = name.split(".", 1)[0].upper()
    if base in WINDOWS_RESERVED or re.fullmatch(r"(?:COM[0-9¹²³]|LPT[0-9])(?:\..*)?", base):
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
    if re.search(r"(^|/)(\.?env(\.|$)|.*secret.*|.*credential.*|.*\.pem$|.*\.key$|.*\.pfx$)", lower):
        return True
    return False


def _is_reparse_point(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & 0x0400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _lstat_path(path: Path, description: str) -> Optional[os.stat_result]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GenerationError(f"Cannot inspect {description} safely: {exc}") from exc
    if _is_reparse_point(info):
        raise GenerationError(f"Symlink or reparse-point path is not allowed: {description}")
    return info


def _safe_directory_tree(root: Path, parts: List[str], create: bool = False) -> Path:
    root_real = root.resolve(strict=True)
    current = root_real
    root_info = _lstat_path(root_real, str(root_real))
    if root_info is None or not stat.S_ISDIR(root_info.st_mode):
        raise GenerationError(f"Workspace/staging root is not a directory: {root_real}")
    for part in parts:
        candidate = current / part
        info = _lstat_path(candidate, str(candidate))
        if info is None and create:
            try:
                candidate.mkdir()
            except FileExistsError:
                pass
            except OSError as exc:
                raise GenerationError(f"Cannot create workspace directory {candidate}: {exc}") from exc
            info = _lstat_path(candidate, str(candidate))
        if info is None:
            raise GenerationError(f"Required directory does not exist: {candidate}")
        if not stat.S_ISDIR(info.st_mode):
            raise GenerationError(f"Path component is not a directory: {candidate}")
        try:
            candidate.resolve(strict=True).relative_to(root_real)
        except (OSError, ValueError) as exc:
            raise GenerationError(f"Directory escapes authorized root: {candidate}") from exc
        current = candidate
    return current


def _workspace_root(workspace: Path) -> Path:
    try:
        root = Path(workspace).resolve(strict=True)
    except OSError as exc:
        raise GenerationError(f"Workspace is unavailable: {exc}") from exc
    info = _lstat_path(root, str(root))
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise GenerationError("Workspace must be an existing directory.")
    return root


def validate_relative_path(workspace: Path, rel_path: str) -> Path:
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise GenerationError("File path must be a non-empty string.")
    if "\x00" in rel_path or any(ord(ch) < 32 for ch in rel_path):
        raise GenerationError("File path contains invalid control characters.")

    # Validate Windows syntax even when tests run on a non-Windows host.
    windows_path = PureWindowsPath(rel_path)
    if windows_path.drive or windows_path.root or windows_path.is_absolute():
        raise GenerationError(f"Absolute, drive-prefixed, or device paths are not allowed: {rel_path}")

    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        raise GenerationError(f"Absolute or UNC paths are not allowed: {rel_path}")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise GenerationError(f"Empty, dot, and traversal path components are not allowed: {rel_path}")

    for part in parts:
        if any(ch in '<>:"|?*' for ch in part):
            raise GenerationError(f"Invalid or alternate-stream filename is not allowed: {part}")
        if part.endswith((" ", ".")):
            raise GenerationError(f"Windows trailing-dot/space filename is not allowed: {part}")
        _check_reserved_name(part)

    rel_posix = "/".join(parts)
    if _is_protected_rel(rel_posix):
        raise GenerationError(f"Protected or sensitive path is not allowed: {rel_path}")

    ws_real = _workspace_root(workspace)
    current = ws_real
    # lstat every component, including dangling links; exists()/resolve() alone
    # follows links and misses dangling symlinks and junctions.
    for index, part in enumerate(parts):
        current = current / part
        info = _lstat_path(current, rel_path)
        if info is None:
            break
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise GenerationError(f"Parent path component is not a directory: {current}")
        try:
            current.resolve(strict=True).relative_to(ws_real)
        except (OSError, ValueError) as exc:
            raise GenerationError(f"Path escapes workspace: {rel_path}") from exc

    candidate = ws_real.joinpath(*parts)
    try:
        candidate.resolve(strict=False).relative_to(ws_real)
    except (OSError, ValueError) as exc:
        raise GenerationError(f"Path escapes workspace: {rel_path}") from exc
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


def _stage_root(workspace: Path, create: bool = False) -> Path:
    ws = _workspace_root(workspace)
    return _safe_directory_tree(ws, [".crane-staging"], create=create)


def _validated_stage_dir(workspace: Path, stage: Dict[str, Any]) -> Tuple[Path, Path]:
    ws = _workspace_root(workspace)
    stage_root = _stage_root(ws, create=False)
    raw_path = stage.get("stage_dir") if isinstance(stage, dict) else None
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise GenerationError("Staging directory is missing.")
    stage_dir = Path(raw_path)
    if not stage_dir.is_absolute():
        raise GenerationError("Staging directory must be an absolute path.")
    if _norm_case(str(stage_dir.parent.absolute())) != _norm_case(str(stage_root.absolute())):
        raise GenerationError("Staging directory must be a direct child of the authorized staging root.")
    if not stage_dir.name.startswith("crane-stage-"):
        raise GenerationError("Invalid staging directory identifier.")
    info = _lstat_path(stage_dir, str(stage_dir))
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise GenerationError("Staging directory is missing or is not a directory.")
    try:
        resolved = stage_dir.resolve(strict=True)
        if _norm_case(str(resolved.parent)) != _norm_case(str(stage_root.resolve(strict=True))):
            raise GenerationError("Staging directory resolves outside the authorized staging root.")
    except OSError as exc:
        raise GenerationError(f"Cannot resolve staging directory: {exc}") from exc
    return stage_root, stage_dir


def _stage_manifest_digest(workspace: Path, stage: Dict[str, Any]) -> str:
    stage_dir = Path(str(stage.get("stage_dir") or "")).resolve(strict=True)
    payload = {
        "workspace": _norm_case(str(workspace.resolve(strict=True))),
        "stage_id": stage_dir.name,
        "files": stage.get("files"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_stage_manifest(workspace: Path, stage: Dict[str, Any]) -> None:
    supplied = stage.get("manifest_digest")
    if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise GenerationError("Staging manifest integrity metadata is missing.")
    if not hmac.compare_digest(supplied, _stage_manifest_digest(workspace, stage)):
        raise GenerationError("Staging manifest integrity check failed.")


def _remove_validated_stage_dir(stage_dir: Path) -> bool:
    try:
        _assert_no_reparse_tree(stage_dir)
        shutil.rmtree(stage_dir)
        return True
    except (GenerationError, OSError):
        return False


def _assert_no_reparse_tree(root: Path) -> None:
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirs) + list(files):
            child = current_path / name
            info = _lstat_path(child, str(child))
            if info is not None and _is_reparse_point(info):
                raise GenerationError(f"Reparse point inside staging directory: {child}")
            if name in dirs and info is not None and not stat.S_ISDIR(info.st_mode):
                raise GenerationError(f"Invalid staging directory entry: {child}")


def inspect_collisions(workspace: Path, ops: List[FileOperation]) -> List[Dict[str, Any]]:
    ws = _workspace_root(workspace)
    collisions = []
    for op in ops:
        target = validate_relative_path(ws, op.path)
        info = _lstat_path(target, op.path)
        exists = info is not None
        collisions.append({
            "path": op.path,
            "operation": op.operation,
            "exists": exists,
            "is_file": bool(info and stat.S_ISREG(info.st_mode)),
            "is_dir": bool(info and stat.S_ISDIR(info.st_mode)),
            "requires_approval": bool(exists or op.operation == "delete"),
        })
    return collisions


def stage_operations(workspace: Path, ops: List[FileOperation]) -> Dict[str, Any]:
    ws = _workspace_root(workspace)
    stage_root = _stage_root(ws, create=True)
    stage_dir = Path(tempfile.mkdtemp(prefix="crane-stage-", dir=str(stage_root)))
    staged = []
    try:
        for op in ops:
            target = validate_relative_path(ws, op.path)
            rel_parts = op.path.replace("\\", "/").split("/")
            record: Dict[str, Any] = {
                "path": target.relative_to(ws).as_posix(),
                "operation": op.operation,
                "description": op.description,
            }
            if op.operation == "write":
                staged_parent = _safe_directory_tree(stage_dir, rel_parts[:-1], create=True)
                staged_path = staged_parent / rel_parts[-1]
                fd = os.open(staged_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(op.content.encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
                record["staged_path"] = str(staged_path)
                record["bytes"] = len(op.content.encode("utf-8"))
            else:
                record["staged_path"] = None
                record["bytes"] = 0
            staged.append(record)
    except Exception:
        _remove_validated_stage_dir(stage_dir)
        raise
    stage = {"stage_dir": str(stage_dir), "created_at": time.time(), "files": staged}
    stage["manifest_digest"] = _stage_manifest_digest(ws, stage)
    return stage


def summarize_changes(workspace: Path, ops: List[FileOperation]) -> Dict[str, Any]:
    ws = _workspace_root(workspace)
    summary = {"creates": [], "overwrites": [], "deletes": [], "protected_blocked": []}
    for op in ops:
        target = validate_relative_path(ws, op.path)
        info = _lstat_path(target, op.path)
        if op.operation == "delete":
            summary["deletes"].append(op.path)
        elif info is not None:
            summary["overwrites"].append(op.path)
        else:
            summary["creates"].append(op.path)
    return summary


def apply_staged_operations(
    workspace: Path,
    stage: Dict[str, Any],
    approved_overwrites: bool = False,
    approved_overwrite_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    ws = _workspace_root(workspace)
    _, stage_dir = _validated_stage_dir(ws, stage)
    _verify_stage_manifest(ws, stage)
    approved_paths = set()
    if approved_overwrite_paths is not None:
        if not isinstance(approved_overwrite_paths, list) or len(approved_overwrite_paths) > MAX_FILES:
            raise GenerationError("Approved overwrite path list is invalid.")
        for approved_path in approved_overwrite_paths:
            approved_target = validate_relative_path(ws, approved_path)
            approved_paths.add(_norm_case(approved_target.relative_to(ws).as_posix()))
    _assert_no_reparse_tree(stage_dir)
    files = stage.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise GenerationError("Staging manifest is invalid.")

    applied = []
    total_bytes = 0
    for record in files:
        if not isinstance(record, dict):
            raise GenerationError("Staging manifest contains an invalid entry.")
        rel = record.get("path")
        operation = record.get("operation")
        if not isinstance(operation, str) or operation not in {"write", "delete"}:
            raise GenerationError(f"Invalid staged operation: {operation!r}")
        if not isinstance(rel, str):
            raise GenerationError("Staged file path must be a string.")

        target = validate_relative_path(ws, rel)
        parts = rel.replace("\\", "/").split("/")
        staged_path = stage_dir.joinpath(*parts) if operation == "write" else None
        supplied_stage_path = record.get("staged_path")
        if operation == "write":
            if not isinstance(supplied_stage_path, str) or _norm_case(os.path.abspath(supplied_stage_path)) != _norm_case(os.path.abspath(str(staged_path))):
                raise GenerationError(f"Staged file reference does not match its validated path: {rel}")
            staged_info = _lstat_path(staged_path, str(staged_path))
            if staged_info is None or not stat.S_ISREG(staged_info.st_mode):
                raise GenerationError(f"Staged file is missing or not a regular file: {rel}")
            total_bytes += staged_info.st_size
            if staged_info.st_size > MAX_FILE_BYTES or total_bytes > MAX_TOTAL_BYTES:
                raise GenerationError("Staged content exceeds configured size limits.")

        target_info = _lstat_path(target, rel)
        exists = target_info is not None
        overwrite_allowed = approved_overwrites or _norm_case(target.relative_to(ws).as_posix()) in approved_paths
        if exists and not overwrite_allowed:
            raise GenerationError(f"Overwrite approval required: {rel}")
        if target_info is not None and not stat.S_ISREG(target_info.st_mode):
            raise GenerationError(f"Refusing to replace a non-regular file: {rel}")

        if operation == "delete":
            if exists:
                # Recheck immediately before unlink. Parent links are rejected by
                # validate_relative_path; unlink never follows the final component.
                latest = _lstat_path(target, rel)
                if latest is None or not stat.S_ISREG(latest.st_mode):
                    raise GenerationError(f"Target changed before deletion: {rel}")
                target.unlink()
                applied.append({"path": rel, "operation": "delete", "status": "deleted"})
            else:
                applied.append({"path": rel, "operation": "delete", "status": "missing"})
            continue

        parent = _safe_directory_tree(ws, parts[:-1], create=True)
        target = parent / parts[-1]
        staged_bytes = staged_path.read_bytes()
        fd, temporary_name = tempfile.mkstemp(dir=str(parent), prefix=".crane-tmp-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(staged_bytes)
                stream.flush()
                os.fsync(stream.fileno())

            # Revalidate parent components and final target immediately before
            # applying. New files use a no-replace hard link so a file created
            # after collision inspection cannot be overwritten accidentally.
            _safe_directory_tree(ws, parts[:-1], create=False)
            latest = _lstat_path(target, rel)
            if latest is not None and not overwrite_allowed:
                raise GenerationError(f"Target appeared after staging; overwrite approval required: {rel}")
            if latest is not None and not stat.S_ISREG(latest.st_mode):
                raise GenerationError(f"Target changed to a non-regular file: {rel}")
            if overwrite_allowed:
                os.replace(temporary_name, target)
            else:
                try:
                    os.link(temporary_name, target)
                except FileExistsError as exc:
                    raise GenerationError(f"Target appeared during apply; refusing to overwrite: {rel}") from exc
                except OSError as exc:
                    raise GenerationError(f"Cannot safely create new file without replacement on this filesystem: {rel}") from exc
                os.unlink(temporary_name)
        finally:
            try:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
            except OSError:
                pass
        applied.append({"path": rel, "operation": "write", "status": "written", "bytes": len(staged_bytes)})

    return {"applied": applied, "count": len(applied)}


def cleanup_stage(stage: Dict[str, Any], workspace: Path) -> bool:
    try:
        _, stage_dir = _validated_stage_dir(workspace, stage)
        _verify_stage_manifest(workspace, stage)
        return _remove_validated_stage_dir(stage_dir)
    except (GenerationError, OSError):
        # Invalid or tampered staging metadata must never trigger arbitrary cleanup.
        return False


def _resolved_validation_argv(argv: Any) -> Optional[List[str]]:
    if not isinstance(argv, (list, tuple)) or not argv or len(argv) > MAX_VALIDATION_ARGV:
        return None
    values = [str(arg) for arg in argv if str(arg)]
    if not values or any(len(arg) > MAX_VALIDATION_ARG_CHARS for arg in values):
        return None
    raw = values[0].replace("\\", "/")
    basename = raw.rsplit("/", 1)[-1]
    key = basename[:-4].lower() if basename.lower().endswith(".exe") else basename.lower()
    trusted = _TRUSTED_EXECUTABLES.get(key)
    if trusted is None:
        return None
    if "/" in raw or re.match(r"^[A-Za-z]:", raw):
        try:
            candidate = Path(raw).resolve(strict=True)
        except OSError:
            return None
        if candidate != trusted:
            return None
    else:
        candidate = trusted
    return [str(candidate), *values[1:]]


def allowed_validation_command(argv: List[str]) -> bool:
    return _resolved_validation_argv(argv) is not None


def _validation_environment() -> Dict[str, str]:
    env: Dict[str, str] = {}
    for key in VALIDATION_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env["PATH"] = _TRUSTED_PATH
    return env


def _redact_validation_text(text: str) -> str:
    result = text
    for pattern in _REDACTION_PATTERNS:
        result = pattern.sub("[redacted]", result)
    return result


def _redact_validation_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_validation_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_validation_value(item) for item in value]
    if isinstance(value, str):
        return _redact_validation_text(value)
    return value


def redact_validation_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Apply durable-output redaction to every validation result field."""
    cleaned = _redact_validation_value(result)
    return cleaned if isinstance(cleaned, dict) else {"status": "failed", "error": "invalid validation result"}


def _create_windows_job(process: subprocess.Popen[Any]) -> Tuple[Any, Any]:
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimit),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    create_job = getattr(kernel32, "CreateJobObjectW")
    create_job.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    create_job.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = create_job(None, None)
    if not job:
        raise OSError("could not create validation process job")
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        raise OSError("could not configure validation process job")
    if not kernel32.AssignProcessToJobObject(job, process._handle):
        kernel32.CloseHandle(job)
        raise OSError("could not assign validation process to job")
    return job, kernel32


def _drain_bounded(stream: Any, limit: int, results: Dict[str, Any], key: str) -> None:
    captured = bytearray()
    truncated = False
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            remaining = max(0, limit - len(captured))
            if remaining:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
    results[key] = (bytes(captured), truncated)


def run_validation_command(workspace: Path, command: ValidationCommand) -> Dict[str, Any]:
    def failed(error: str, argv: Optional[List[str]] = None) -> Dict[str, Any]:
        return redact_validation_result({
            "name": command.name, "argv": argv or [], "status": "failed", "error": error,
        })

    ws = _workspace_root(workspace)
    original_argv = [str(arg) for arg in command.argv if str(arg)]
    resolved = _resolved_validation_argv(original_argv)
    if not original_argv:
        return failed("empty command")
    if resolved is None:
        return failed(f"command not allowed: {original_argv[0]}", original_argv)
    argv = resolved
    cwd = validate_relative_path(ws, command.cwd) if command.cwd not in {"", "."} else ws
    cwd_info = _lstat_path(cwd, str(cwd))
    if cwd_info is None or not stat.S_ISDIR(cwd_info.st_mode):
        return failed("cwd must be an existing directory inside the workspace", argv)

    timeout = max(1, min(int(command.timeout_seconds or DEFAULT_COMMAND_TIMEOUT_SECONDS), MAX_COMMAND_TIMEOUT_SECONDS))
    env = _validation_environment()
    popen_kwargs: Dict[str, Any] = {}
    if _is_windows():
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    start = time.time()
    process = None
    job = None
    kernel32 = None
    results: Dict[str, Any] = {}
    readers = []
    timed_out = False
    try:
        process = subprocess.Popen(
            argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=env, shell=False, close_fds=True, **popen_kwargs,
        )
        if _is_windows():
            try:
                job, kernel32 = _create_windows_job(process)
            except Exception:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                raise
        for key, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            if pipe is not None:
                reader = threading.Thread(target=_drain_bounded, args=(pipe, MAX_COMMAND_OUTPUT, results, key), daemon=True)
                reader.start()
                readers.append(reader)
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if job is not None and kernel32 is not None:
                kernel32.TerminateJobObject(job, 1)
            elif _is_windows():
                process.kill()
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
            exit_code = process.wait(timeout=10)
        for reader in readers:
            reader.join(timeout=1.0)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        for reader in readers:
            reader.join(timeout=0.25)

        stdout_bytes, stdout_truncated = results.get("stdout", (b"", False))
        stderr_bytes, stderr_truncated = results.get("stderr", (b"", False))
        base = {
            "name": command.name, "argv": argv, "exit_code": exit_code,
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            "stdout_truncated": stdout_truncated, "stderr_truncated": stderr_truncated,
            "duration_ms": int((time.time() - start) * 1000),
        }
        if timed_out:
            return redact_validation_result({**base, "status": "failed", "error": f"timeout after {timeout}s"})
        return redact_validation_result({**base, "status": "succeeded" if exit_code == 0 else "failed"})
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), argv)
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        for reader in readers:
            reader.join(timeout=1.0)
        if job is not None and kernel32 is not None:
            kernel32.CloseHandle(job)

def default_validation_commands(workspace: Path) -> List[ValidationCommand]:
    ws = workspace.resolve(strict=True)
    cmds: List[ValidationCommand] = []
    if (ws / "pyproject.toml").exists() or (ws / "requirements.txt").exists() or any(ws.rglob("*.py")):
        # Syntax check only; no install.
        py_files = [p.relative_to(ws).as_posix() for p in ws.rglob("*.py") if ".crane-staging" not in p.parts][:50]
        if py_files:
            cmds.append(ValidationCommand(name="python_syntax", argv=[sys.executable, "-m", "py_compile", *py_files], timeout_seconds=120))
        if (ws / "tests").exists() or any(ws.rglob("test_*.py")):
            cmds.append(ValidationCommand(name="pytest", argv=[sys.executable, "-m", "pytest", "-q"], timeout_seconds=180, required=False))
    if (ws / "package.json").exists():
        cmds.append(ValidationCommand(name="npm_test", argv=["npm", "test", "--", "--runInBand"], timeout_seconds=180, required=False))
    if (ws / "go.mod").exists():
        cmds.append(ValidationCommand(name="go_test", argv=["go", "test", "./..."], timeout_seconds=180, required=False))
    return cmds
