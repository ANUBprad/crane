"""Explicit model-provider selection for the Python agent runtime.

Local deterministic execution is the default. Hosted providers are never
selected implicitly. When Ollama is configured, LangChain's maintained Ollama
integration is the only supported provider boundary.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import shutil
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional


DEFAULT_MODEL = "llama3.2:3b"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_INVOKE_TIMEOUT_SECONDS = 45.0
MAX_CONNECT_TIMEOUT_SECONDS = 30.0
MAX_INVOKE_TIMEOUT_SECONDS = 300.0
OLLAMA_DEPENDENCY = "langchain-ollama==1.1.0"
_TAGS_RESPONSE_LIMIT = 1 << 20


class ProviderError(RuntimeError):
    """An actionable provider configuration or availability failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OllamaConfig:
    """Validated, non-secret configuration for the local Ollama provider."""

    model: str
    base_url: str
    connect_timeout_seconds: float
    invoke_timeout_seconds: float
    allow_remote: bool

    def details(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "invoke_timeout_seconds": self.invoke_timeout_seconds,
            "remote_endpoint_authorized": self.allow_remote,
        }


def _environment(env: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    return os.environ if env is None else env


def selected_provider(env: Optional[Mapping[str, str]] = None) -> str:
    return _environment(env).get("CRANE_MODEL_PROVIDER", "local").strip().lower()


def prepare_provider(env: Optional[Mapping[str, str]] = None) -> None:
    """Validate configuration and Ollama availability before provider import."""
    if selected_provider(env) == "ollama":
        preflight_ollama(load_ollama_config(env))


def warm_provider(env: Optional[Mapping[str, str]] = None) -> None:
    """Import the configured provider on the caller thread before task work.

    LangChain integration imports are comparatively expensive on Windows. The
    workflow calls this from its event-loop thread so the worker that invokes
    the model does not pay the first-import cost while cancellation is polled.
    Missing dependencies remain actionable at invocation time.
    """
    if selected_provider(env) != "ollama":
        return
    try:
        from langchain_ollama import ChatOllama  # type: ignore  # noqa: F401
    except ImportError:
        pass


def _parse_timeout(
    name: str,
    raw: Optional[str],
    default: float,
    maximum: float,
) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            "provider_invalid_configuration",
            f"{name} must be a positive number of seconds.",
        ) from exc
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise ProviderError(
            "provider_invalid_configuration",
            f"{name} must be greater than 0 and at most {maximum:g} seconds.",
        )
    return value


def _parse_bool(name: str, raw: Optional[str]) -> bool:
    if raw is None or not raw.strip():
        return False
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ProviderError(
        "provider_invalid_configuration",
        f"{name} must be true or false.",
    )


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalize_base_url(raw: Optional[str]) -> str:
    value = (raw or "").strip() or DEFAULT_BASE_URL
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProviderError(
            "provider_invalid_configuration",
            "OLLAMA_HOST must be a valid http:// or https:// endpoint.",
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ProviderError(
            "provider_invalid_configuration",
            "OLLAMA_HOST must be a valid http:// or https:// endpoint.",
        )
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ProviderError(
            "provider_invalid_configuration",
            "OLLAMA_HOST must be an origin without credentials, path, query or fragment.",
        )
    host = parsed.hostname
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return f"{parsed.scheme.lower()}://{netloc}"


def load_ollama_config(env: Optional[Mapping[str, str]] = None) -> OllamaConfig:
    """Read and validate Ollama configuration without contacting any service."""
    values = _environment(env)
    configured_model = values.get("CRANE_MODEL")
    if configured_model is None:
        model = DEFAULT_MODEL
    else:
        model = configured_model.strip()
        if not model:
            raise ProviderError(
                "provider_not_configured",
                "CRANE_MODEL is empty. Set a locally installed Ollama model name or remove CRANE_MODEL to use the documented default.",
            )
    base_url = _normalize_base_url(values.get("OLLAMA_HOST"))
    connect_timeout = _parse_timeout(
        "CRANE_OLLAMA_CONNECT_TIMEOUT_SECONDS",
        values.get("CRANE_OLLAMA_CONNECT_TIMEOUT_SECONDS"),
        DEFAULT_CONNECT_TIMEOUT_SECONDS,
        MAX_CONNECT_TIMEOUT_SECONDS,
    )
    invoke_timeout = _parse_timeout(
        "CRANE_OLLAMA_INVOKE_TIMEOUT_SECONDS",
        values.get("CRANE_OLLAMA_INVOKE_TIMEOUT_SECONDS"),
        DEFAULT_INVOKE_TIMEOUT_SECONDS,
        MAX_INVOKE_TIMEOUT_SECONDS,
    )
    allow_remote = _parse_bool(
        "CRANE_ALLOW_REMOTE_OLLAMA",
        values.get("CRANE_ALLOW_REMOTE_OLLAMA"),
    )
    host = urllib.parse.urlsplit(base_url).hostname or ""
    if not allow_remote and not _is_loopback_host(host):
        raise ProviderError(
            "ollama_remote_endpoint_not_authorized",
            "OLLAMA_HOST names a non-local endpoint. Set CRANE_ALLOW_REMOTE_OLLAMA=1 only when you explicitly authorize sending task text to that endpoint.",
        )
    return OllamaConfig(model, base_url, connect_timeout, invoke_timeout, allow_remote)


def provider_details(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    provider = selected_provider(env)
    details: Dict[str, Any] = {"name": provider, "local": provider in {"local", "ollama"}}
    if provider == "ollama":
        try:
            details.update(load_ollama_config(env).details())
        except ProviderError as exc:
            details["configuration_error"] = exc.code
    return details


def normalize_input(user_input: str) -> str:
    """Return a bounded, deterministic normalization of caller input."""
    normalized = re.sub(r"\s+", " ", user_input).strip()
    if len(normalized) > 4000:
        normalized = normalized[:4000] + "…"
    return normalized


def deterministic_result(user_input: str, details: Dict[str, Any]) -> Dict[str, Any]:
    """Produce a real execution result without inventing model output.

    This mode performs input normalization and reports workflow execution
    facts. It deliberately does not claim to answer or reason about the task.
    """
    normalized = normalize_input(user_input)
    return {
        "execution_mode": "deterministic_local",
        "provider": details["name"],
        "normalized_input": normalized,
        "input_bytes": len(user_input.encode("utf-8")),
        "model_output": None,
        "notice": "No model provider is configured; this result contains deterministic workflow output only.",
    }


def fetch_ollama_models(config: OllamaConfig) -> List[str]:
    """List installed models from the configured Ollama endpoint."""
    request = urllib.request.Request(
        f"{config.base_url}/api/tags",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.connect_timeout_seconds) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise ProviderError(
                    "ollama_unavailable",
                    f"Ollama returned HTTP {status} from {config.base_url}. Check the configured OLLAMA_HOST endpoint.",
                )
            raw = response.read(_TAGS_RESPONSE_LIMIT)
    except ProviderError:
        raise
    except urllib.error.HTTPError as exc:
        raise ProviderError(
            "ollama_unavailable",
            f"Ollama returned HTTP {exc.code} from {config.base_url}. Confirm OLLAMA_HOST points to an Ollama server.",
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise ProviderError(
            "ollama_unavailable",
            f"Cannot reach Ollama at {config.base_url}. Start Ollama locally or correct OLLAMA_HOST.",
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(
            "ollama_unavailable",
            "The Ollama endpoint returned an invalid model list response.",
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderError(
            "ollama_unavailable",
            "The Ollama endpoint returned an invalid model list response.",
        )
    models: List[str] = []
    for item in payload.get("models") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            models.append(item["name"].strip())
    return models


def _model_aliases(model: str) -> set[str]:
    aliases = {model}
    if ":" not in model:
        aliases.add(f"{model}:latest")
    return aliases


def model_is_available(model: str, models: List[str]) -> bool:
    installed = {name for name in models if name in _model_aliases(model)}
    if installed:
        return True
    base = model.rsplit(":", 1)[0] if ":" in model else model
    return any(name == f"{base}:latest" for name in models)


def _model_not_found_error(config: OllamaConfig) -> ProviderError:
    return ProviderError(
        "model_not_found",
        f"Ollama is reachable but model {config.model!r} is not installed. Install it explicitly with `ollama pull {config.model}`; CRANE does not download model weights automatically.",
    )


def preflight_ollama(config: OllamaConfig) -> List[str]:
    """Verify connectivity and the configured model before any task is sent."""
    models = fetch_ollama_models(config)
    if not model_is_available(config.model, models):
        raise _model_not_found_error(config)
    return models


def _create_chat_model(config: OllamaConfig) -> Any:
    try:
        from langchain_ollama import ChatOllama  # type: ignore
    except ImportError as exc:
        raise ProviderError(
            "provider_unavailable",
            f"Ollama provider dependency is missing. Install {OLLAMA_DEPENDENCY} or use CRANE_MODEL_PROVIDER=local.",
        ) from exc
    try:
        return ChatOllama(
            model=config.model,
            base_url=config.base_url,
            temperature=0,
            client_kwargs={"timeout": config.invoke_timeout_seconds},
        )
    except Exception as exc:  # noqa: BLE001 - normalize third-party initialization errors
        raise ProviderError(
            "provider_initialization_failed",
            "LangChain could not initialize the configured Ollama provider. Check CRANE_MODEL and OLLAMA_HOST.",
        ) from exc


def _invoke_ollama(user_input: str, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    config = load_ollama_config(env)
    preflight_ollama(config)
    chat_model = _create_chat_model(config)
    try:
        response = chat_model.invoke(normalize_input(user_input))
    except Exception as exc:  # noqa: BLE001 - provider errors must be actionable, not leaked verbatim
        raise ProviderError(
            "provider_invocation_failed",
            f"The configured Ollama model {config.model!r} did not complete the request. Confirm the model supports chat, is loaded locally, and CRANE_OLLAMA_INVOKE_TIMEOUT_SECONDS allows enough time.",
        ) from exc
    content = getattr(response, "content", response)
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("provider_empty_response", "The configured model returned an empty response.")
    return {
        "execution_mode": "langchain_ollama",
        "provider": "ollama",
        "model": config.model,
        "model_output": content.strip(),
        "notice": None,
    }


def invoke(user_input: str, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Execute the configured provider path. No fallback is permitted."""
    provider = selected_provider(env)
    if provider == "local":
        return deterministic_result(user_input, provider_details(env))
    if provider == "ollama":
        return _invoke_ollama(user_input, env)
    raise ProviderError(
        "provider_not_supported",
        f"Model provider {provider!r} is not supported. Configure CRANE_MODEL_PROVIDER=local or =ollama.",
    )


def ollama_installed() -> bool:
    """Report whether the Ollama CLI is discoverable on PATH."""
    return shutil.which("ollama") is not None


def collect_diagnostics(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Return safe provider diagnostics without task data or credentials."""
    values = _environment(env)
    provider = selected_provider(values)
    ollama: Dict[str, Any] = {
        "installed": ollama_installed(),
        "base_url": DEFAULT_BASE_URL,
        "reachable": False,
        "configured_model": DEFAULT_MODEL,
        "model_available": False,
        "available_model_count": 0,
        "provider_initializable": False,
    }
    issues: List[Dict[str, str]] = []
    try:
        config = load_ollama_config(values)
    except ProviderError as exc:
        ollama["configured_model"] = (values.get("CRANE_MODEL") or "").strip() or DEFAULT_MODEL
        issues.append({"code": exc.code, "message": exc.message})
        config = None
    if config is not None:
        ollama.update({
            "base_url": config.base_url,
            "configured_model": config.model,
        })
        try:
            models = fetch_ollama_models(config)
            ollama["reachable"] = True
            ollama["available_model_count"] = len(models)
            if model_is_available(config.model, models):
                ollama["model_available"] = True
            else:
                missing = _model_not_found_error(config)
                issues.append({"code": missing.code, "message": missing.message})
        except ProviderError as exc:
            issues.append({"code": exc.code, "message": exc.message})
        try:
            _create_chat_model(config)
            ollama["provider_initializable"] = True
        except ProviderError as exc:
            issues.append({"code": exc.code, "message": exc.message})
    if provider not in {"local", "ollama"}:
        issues.append({
            "code": "provider_not_supported",
            "message": "CRANE_MODEL_PROVIDER must be local or ollama.",
        })
    degraded = provider == "ollama" and bool(issues)
    return {
        "protocol": "crane-agent/0.1.0",
        "status": "degraded" if degraded else "ok",
        "provider": provider,
        "ollama": ollama,
        "issues": issues,
    }


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_INVOKE_TIMEOUT_SECONDS",
    "DEFAULT_MODEL",
    "OllamaConfig",
    "OLLAMA_DEPENDENCY",
    "ProviderError",
    "collect_diagnostics",
    "deterministic_result",
    "fetch_ollama_models",
    "invoke",
    "load_ollama_config",
    "model_is_available",
    "normalize_input",
    "ollama_installed",
    "preflight_ollama",
    "prepare_provider",
    "provider_details",
    "selected_provider",
    "warm_provider",
]
