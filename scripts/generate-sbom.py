#!/usr/bin/env python3
"""Generate a deterministic CycloneDX 1.5 inventory for CRANE release inputs.

The BOM covers the pinned Python runtime dependency closure and the complete Go
module graph. It is standard-library-only: release generation does not depend
on another toolchain or an unpinned scanner package.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

GENERATOR_VERSION = "1.0.0"
PYTHON_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")
HASH_PATTERN = re.compile(r"--hash=sha256:([0-9a-fA-F]{64})")


def normalize_pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_python_lock(path: Path) -> list[dict]:
    components: list[dict] = []
    current: dict | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        match = PYTHON_REQUIREMENT.match(line)
        if match:
            if current is not None:
                if not current["hashes"]:
                    raise ValueError(f"Python lock entry has no SHA-256 hash: {current['name']}")
                components.append(current)
            name, version = match.groups()
            current = {"name": name, "version": version, "hashes": []}
        elif current is not None:
            current["hashes"].extend(HASH_PATTERN.findall(line))
    if current is not None:
        if not current["hashes"]:
            raise ValueError(f"Python lock entry has no SHA-256 hash: {current['name']}")
        components.append(current)
    if not components:
        raise ValueError(f"No pinned Python dependencies found in {path}")
    output = []
    for item in components:
        purl = f"pkg:pypi/{quote(normalize_pypi_name(item['name']), safe='-._~')}@{quote(item['version'], safe='-._~+')}"
        output.append({
            "type": "library",
            "bom-ref": purl,
            "name": item["name"],
            "version": item["version"],
            "purl": purl,
            "scope": "required",
            "hashes": [{"alg": "SHA-256", "content": value.lower()} for value in sorted(set(item["hashes"]))],
            "properties": [{"name": "crane:ecosystem", "value": "pypi"}],
        })
    return output


def _go_purl_path(path: str) -> str:
    # Go PURLs encode uppercase path letters as !lowercase.
    encoded = "".join(("!" + char.lower()) if char.isupper() else char for char in path)
    return quote(encoded, safe="/.-_~!")


def parse_go_modules(raw_json: str) -> list[dict]:
    decoder = json.JSONDecoder()
    position = 0
    modules = []
    while position < len(raw_json):
        while position < len(raw_json) and raw_json[position].isspace():
            position += 1
        if position >= len(raw_json):
            break
        item, position = decoder.raw_decode(raw_json, position)
        if item.get("Main") or not item.get("Path") or not item.get("Version"):
            continue
        path = str(item["Path"])
        version = str(item["Version"])
        purl = f"pkg:golang/{_go_purl_path(path)}@{quote(version, safe='-._~+')}"
        properties = [{"name": "crane:ecosystem", "value": "golang"}]
        if item.get("Indirect"):
            properties.append({"name": "crane:go:indirect", "value": "true"})
        replacement = item.get("Replace")
        if isinstance(replacement, dict):
            replacement_label = str(replacement.get("Path") or "")
            if replacement.get("Version"):
                replacement_label += "@" + str(replacement["Version"])
            if replacement_label:
                properties.append({"name": "crane:go:replace", "value": replacement_label})
        modules.append({
            "type": "library",
            "bom-ref": purl,
            "name": path,
            "version": version,
            "purl": purl,
            "scope": "required",
            "properties": properties,
        })
    if not modules:
        raise ValueError("Go module graph returned no third-party dependencies")
    return modules


def build_bom(version: str, commit: str, build_date: str, python_version: str,
              go_version: str, components: list[dict]) -> dict:
    root_purl = f"pkg:generic/crane@{quote(version, safe='-._~+')}"
    sorted_components = sorted(components, key=lambda item: (item["purl"], item["version"]))
    refs = [item["bom-ref"] for item in sorted_components]
    serial_seed = "|".join([version, commit, *refs])
    serial = uuid.uuid5(uuid.NAMESPACE_URL, "https://crane.local/sbom/" + serial_seed)
    timestamp = datetime.fromisoformat(build_date.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:" + str(serial),
        "version": 1,
        "metadata": {
            "timestamp": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tools": [{"vendor": "CRANE Project", "name": "generate-sbom.py", "version": GENERATOR_VERSION}],
            "component": {
                "type": "application",
                "bom-ref": root_purl,
                "name": "CRANE",
                "version": version,
                "purl": root_purl,
            },
            "properties": [
                {"name": "crane:source:commit", "value": commit},
                {"name": "crane:target", "value": "windows-x64"},
                {"name": "crane:python-runtime", "value": python_version},
                {"name": "crane:go-version", "value": go_version},
            ],
        },
        "components": sorted_components,
        "dependencies": [{"ref": root_purl, "dependsOn": refs}],
    }


def _self_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        lock = Path(temp) / "requirements.lock.txt"
        lock.write_text(
            "\n".join([
                "Example_Pkg==1.2.3",
                "    --hash=sha256:" + "a" * 64,
                "Second==4.5.6",
                "    --hash=sha256:" + "b" * 64,
                "",
            ]),
            encoding="utf-8",
        )
        python = parse_python_lock(lock)
    assert len(python) == 2
    assert python[0]["purl"] == "pkg:pypi/example-pkg@1.2.3"
    go = parse_go_modules('{"Path":"github.com/Example/Module","Version":"v1.2.3"}')
    assert go[0]["purl"] == "pkg:golang/github.com/!example/!module@v1.2.3"
    bom = build_bom("0.1.0", "abc123", "2026-01-01T00:00:00Z", "3.12.7", "go1.26.5", python + go)
    assert bom["bomFormat"] == "CycloneDX" and len(bom["components"]) == 3
    assert bom["dependencies"][0]["dependsOn"] == sorted(bom["dependencies"][0]["dependsOn"])
    print("SBOM generator self-test: PASS")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--commit")
    parser.add_argument("--build-date")
    parser.add_argument("--python-version")
    parser.add_argument("--go-version")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        return 0
    required = [args.repo_root, args.version, args.commit, args.build_date, args.python_version, args.go_version, args.output]
    if any(value is None for value in required):
        parser.error("release generation requires --repo-root, --version, --commit, --build-date, --python-version, --go-version and --output")
    try:
        repo_root = args.repo_root.resolve(strict=True)
        python_components = parse_python_lock(repo_root / "requirements.lock.txt")
        completed = subprocess.run(
            ["go", "list", "-m", "-json", "all"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        go_components = parse_go_modules(completed.stdout)
        bom = build_bom(
            args.version, args.commit, args.build_date,
            args.python_version, args.go_version, python_components + go_components,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(bom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            f"SBOM written: {args.output} "
            f"(CycloneDX {bom['specVersion']}, {len(bom['components'])} dependencies)"
        )
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None)
        print(f"SBOM generation failed: {exc}" + (f"\n{detail}" if detail else ""), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
