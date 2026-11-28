"""Explicit model-provider selection for the Python agent runtime.

Local deterministic execution is the default. Hosted providers are never
selected implicitly. When Ollama is configured, LangChain's maintained Ollama
integration is the only supported provider boundary.
"""
from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from crane.python_runtime.workflow_state import MAX_TOOL_CALLS_PER_TASK, MAX_TOOL_OUTPUT_CHARS


DEFAULT_MODEL = "llama3.2:3b"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_INVOKE_TIMEOUT_SECONDS = 180.0
MAX_CONNECT_TIMEOUT_SECONDS = 30.0
MAX_INVOKE_TIMEOUT_SECONDS = 300.0
DEFAULT_NUM_CTX = 4096
MAX_NUM_CTX = 131072
OLLAMA_DEPENDENCY = "langchain-ollama==1.1.0"
_TAGS_RESPONSE_LIMIT = 1 << 20
MAX_TOOL_ROUNDS = 4


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
    num_ctx: int
    allow_remote: bool

    def details(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "invoke_timeout_seconds": self.invoke_timeout_seconds,
            "num_ctx": self.num_ctx,
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


def _import_chat_ollama() -> Any:
    """Import ChatOllama without loading LangChain's unused transformers path."""
    block_transformers = "transformers" not in sys.modules
    if block_transformers:
        sys.modules["transformers"] = None
    try:
        from langchain_ollama import ChatOllama  # type: ignore
        return ChatOllama
    finally:
        if block_transformers:
            sys.modules.pop("transformers", None)


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
        _import_chat_ollama()
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


def _parse_num_ctx(raw: Optional[str]) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_NUM_CTX
    try:
        value = int(raw.strip())
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            "provider_invalid_configuration",
            f"CRANE_OLLAMA_NUM_CTX must be a whole number of tokens between 1 and {MAX_NUM_CTX}.",
        ) from exc
    if value < 1 or value > MAX_NUM_CTX:
        raise ProviderError(
            "provider_invalid_configuration",
            f"CRANE_OLLAMA_NUM_CTX must be between 1 and {MAX_NUM_CTX} tokens.",
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
    num_ctx = _parse_num_ctx(values.get("CRANE_OLLAMA_NUM_CTX"))
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
    return OllamaConfig(model, base_url, connect_timeout, invoke_timeout, num_ctx, allow_remote)


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


def _context_answer(user_input: str, context: Dict[str, Any]) -> Optional[str]:
    """Answer identity/capability questions from real runtime context only."""
    text = normalize_input(user_input).lower()
    if not text:
        return None
    identity = context.get("identity") or {}
    caps = context.get("capabilities") or {}
    tools = context.get("tools") or []
    limits = context.get("limitations") or []
    provider = context.get("provider") or {}
    asks_identity = any(k in text for k in ["what is crane", "who are you", "what are you", "describe crane"])
    asks_can_do = any(k in text for k in ["what can you do", "capabilities", "what do you do", "help"])
    asks_provider = any(k in text for k in ["which model", "what model", "provider", "ollama", "llm"])
    asks_tools = "tool" in text and any(k in text for k in ["available", "what", "which", "list", "have"])
    asks_limits = any(k in text for k in ["limitation", "can't", "cannot", "what are your limits"])
    if not (asks_identity or asks_can_do or asks_provider or asks_tools or asks_limits):
        return None
    lines: List[str] = []
    if asks_identity or asks_can_do:
        lines.append(str(identity.get("summary") or "CRANE is a local-first, offline-first MCP execution and orchestration runtime."))
        if identity.get("architecture"):
            lines.append(f"Architecture: {identity['architecture']}")
    if asks_provider or asks_identity or asks_can_do:
        prov_name = provider.get("name", "unknown")
        model = provider.get("model")
        lines.append(f"Configured model provider: {prov_name}" + (f" (model: {model})" if model else ""))
        if provider.get("configuration_error"):
            lines.append(f"Provider configuration issue: {provider['configuration_error']}")
    if asks_tools or asks_can_do:
        if tools:
            lines.append("Available tools:")
            for t in tools[:30]:
                name = t.get("name") if isinstance(t, dict) else str(t)
                desc = t.get("description") if isinstance(t, dict) else ""
                lines.append(f"- {name}" + (f": {desc}" if desc else ""))
        else:
            lines.append("Available tools: none currently discovered/registered.")
    if asks_limits or asks_can_do:
        if limits:
            lines.append("Limitations:")
            for lim in limits[:20]:
                lines.append(f"- {lim}")
    if asks_can_do:
        available = caps.get("available") or []
        configured_unavailable = caps.get("configured_but_unavailable") or []
        not_implemented = caps.get("not_implemented") or []
        if available:
            lines.append("Available capabilities: " + "; ".join(str(x) for x in available[:15]))
        if configured_unavailable:
            lines.append("Configured but currently unavailable: " + "; ".join(str(x) for x in configured_unavailable[:15]))
        if not_implemented:
            lines.append("Not implemented: " + "; ".join(str(x) for x in not_implemented[:15]))
    return "\n".join(lines).strip() if lines else None


def deterministic_result(user_input: str, details: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Produce a real execution result without inventing model output.

    When runtime context is available, answer identity/capability questions
    factually from that context. Otherwise report deterministic workflow facts.
    """
    normalized = normalize_input(user_input)
    ctx = context or {}
    answer = _context_answer(user_input, ctx) if ctx else None
    return {
        "execution_mode": "deterministic_local",
        "provider": details["name"],
        "normalized_input": normalized,
        "input_bytes": len(user_input.encode("utf-8")),
        "model_output": answer,
        "context_used": bool(ctx),
        "notice": None if answer else "No model provider is configured; this result contains deterministic workflow output only.",
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


def _create_chat_model(config: OllamaConfig, json_mode: bool = False) -> Any:
    try:
        chat_class = _import_chat_ollama()
    except ImportError as exc:
        raise ProviderError(
            "provider_unavailable",
            f"Ollama provider dependency is missing. Install {OLLAMA_DEPENDENCY} or use CRANE_MODEL_PROVIDER=local.",
        ) from exc
    try:
        kwargs: Dict[str, Any] = {
            "model": config.model,
            "base_url": config.base_url,
            "temperature": 0,
            "num_ctx": config.num_ctx,
            "client_kwargs": {"timeout": config.invoke_timeout_seconds},
        }
        if json_mode:
            kwargs["format"] = "json"
        return chat_class(**kwargs)
    except Exception as exc:  # noqa: BLE001 - normalize third-party initialization errors
        raise ProviderError(
            "provider_initialization_failed",
            "LangChain could not initialize the configured Ollama provider. Check CRANE_MODEL and OLLAMA_HOST.",
        ) from exc


def _build_messages(user_input: str, system_prompt: str = "", messages: Optional[List[Dict[str, str]]] = None) -> List[Any]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    built: List[Any] = []
    if system_prompt and system_prompt.strip():
        built.append(SystemMessage(content=system_prompt.strip()))
    for item in messages or []:
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "")
        if not content.strip():
            continue
        if role == "user":
            built.append(HumanMessage(content=content))
        elif role == "assistant":
            built.append(AIMessage(content=content))
        elif role == "system":
            built.append(SystemMessage(content=content))
    # Ensure the current user input is present as the latest user turn.
    normalized = normalize_input(user_input)
    if not built or not isinstance(built[-1], HumanMessage) or built[-1].content != normalized:
        built.append(HumanMessage(content=normalized))
    return built


def _content_to_text(content: Any) -> str:
    if isinstance(content, list):
        return _tool_output_text(content)
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return _tool_output_text(content)


def _invoke_ollama(
    user_input: str,
    env: Optional[Mapping[str, str]] = None,
    system_prompt: str = "",
    messages: Optional[List[Dict[str, str]]] = None,
    on_delta: Optional[Any] = None,
    json_mode: bool = False,
) -> Dict[str, Any]:
    config = load_ollama_config(env)
    preflight_ollama(config)
    chat_model = _create_chat_model(config, json_mode=json_mode)
    prompt_messages = _build_messages(user_input, system_prompt=system_prompt, messages=messages)
    try:
        if on_delta is None or not hasattr(chat_model, "stream"):
            response = chat_model.invoke(prompt_messages)
            content = _content_to_text(getattr(response, "content", response))
            if on_delta and content:
                try:
                    on_delta(content)
                except Exception:
                    pass
        else:
            try:
                chunks: List[str] = []
                emitted = False
                for chunk in chat_model.stream(prompt_messages):
                    text = _content_to_text(getattr(chunk, "content", ""))
                    if not text:
                        continue
                    chunks.append(text)
                    try:
                        on_delta(text)
                        emitted = True
                    except Exception:
                        emitted = True
                content = "".join(chunks)
                if not content.strip() and not emitted:
                    response = chat_model.invoke(prompt_messages)
                    content = _content_to_text(getattr(response, "content", response))
                    if content:
                        try:
                            on_delta(content)
                        except Exception:
                            pass
            except Exception:
                # Fall back to non-streaming invoke for compatibility.
                response = chat_model.invoke(prompt_messages)
                content = _content_to_text(getattr(response, "content", response))
                if content:
                    try:
                        on_delta(content)
                    except Exception:
                        pass
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider errors must be actionable, not leaked verbatim
        raise ProviderError(
            "provider_invocation_failed",
            f"The configured Ollama model {config.model!r} did not complete the request. Confirm the model supports chat, is loaded locally, and CRANE_OLLAMA_INVOKE_TIMEOUT_SECONDS allows enough time.",
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("provider_empty_response", "The configured model returned an empty response.")
    return {
        "execution_mode": "langchain_ollama",
        "provider": "ollama",
        "model": config.model,
        "model_output": content.strip(),
        "notice": None,
    }


def _tool_output_text(output: Any) -> str:
    """Normalize LangChain tool output to the text sent back to the model."""
    if isinstance(output, tuple):
        output = output[0] if output else ""
    if isinstance(output, str):
        return output.strip() or "Tool completed"
    if isinstance(output, list):
        parts: List[str] = []
        for item in output:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    try:
        return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(output)


def _bounded_tool_count(value: Optional[int], default: int, maximum: int, label: str) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ProviderError("tool_loop_limit", f"{label} must be a whole number.") from exc
    if number < 1:
        raise ProviderError("tool_loop_limit", f"{label} must be at least 1.")
    return min(number, maximum)


def _extract_tool_calls(response: Any) -> List[Dict[str, Any]]:
    """Read standard or additional-kwargs tool calls from a LangChain response."""
    calls = getattr(response, "tool_calls", None)
    if isinstance(calls, (list, tuple)):
        return [item if isinstance(item, dict) else {} for item in calls]
    additional = getattr(response, "additional_kwargs", None)
    raw = additional.get("tool_calls") if isinstance(additional, dict) else None
    if not isinstance(raw, (list, tuple)):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            normalized.append({})
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        normalized.append({
            "id": item.get("id"),
            "name": function.get("name") or item.get("name"),
            "args": function.get("arguments") if function.get("arguments") is not None else item.get("args"),
        })
    return normalized


def _parse_tool_arguments(value: Any) -> Dict[str, Any]:
    """Accept object arguments and serialized JSON objects, rejecting other shapes."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("tool arguments must be a JSON object")
    raise ValueError("tool arguments must be an object")


def _resolve_tool(name: str, tools: List[Any]) -> Tuple[Any, Optional[str]]:
    """Resolve exact names first, then an unambiguous unprefixed raw name."""
    if not name:
        return None, "Malformed tool call response"
    exact = next((item for item in tools if getattr(item, "name", None) == name), None)
    if exact is not None:
        return exact, None
    suffix = f"_{name}"
    matches = [item for item in tools if str(getattr(item, "name", "") or "").endswith(suffix)]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"MCP tool name {name!r} is ambiguous"
    return None, f"MCP tool {name!r} is not available"


def _bounded_tool_result(output: Any, limit: int) -> str:
    text = _tool_output_text(output)
    if len(text) > limit:
        return text[:limit] + "…[truncated]"
    return text


def _classify_tool_result(text: str) -> Tuple[str, Optional[str]]:
    lower = text.lower()
    if "approval" in lower and any(word in lower for word in ("failed", "error", "requires")):
        return "blocked", "approval_required"
    if "mcp tool failed:" in lower or "mcp tool call failed" in lower or "mcp tool returned an error" in lower or "tool returned an error" in lower:
        return "failed", "MCP tool reported an error"
    return "succeeded", None


def _append_observed_call(
    observed: List[Dict[str, Any]],
    *,
    name: str,
    call_id: str,
    arguments: Dict[str, Any],
    result: str,
    status: str,
    error: Optional[str] = None,
) -> None:
    record: Dict[str, Any] = {
        "name": name,
        "tool_call_id": call_id,
        "arguments": arguments,
        "result": result,
        "status": status,
    }
    if error:
        record["error"] = error
    from crane.mcp_runtime import redact
    observed.append(redact(record))


async def _invoke_ollama_with_tools(
    user_input: str,
    tools: List[Any],
    env: Optional[Mapping[str, str]] = None,
    system_prompt: str = "",
    messages: Optional[List[Dict[str, str]]] = None,
    *,
    max_tool_rounds: Optional[int] = None,
    max_tool_calls: Optional[int] = None,
    max_tool_output_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Run a bounded Ollama tool loop using real LangChain MCP tools."""
    from langchain_core.messages import AIMessage, ToolMessage

    config = load_ollama_config(env)
    preflight_ollama(config)
    chat_model = _create_chat_model(config)
    try:
        bound_model = chat_model.bind_tools(tools)
    except Exception as exc:  # noqa: BLE001 - normalize provider binding failures
        raise ProviderError(
            "provider_tool_binding_failed",
            "The configured Ollama model could not bind the discovered MCP tools.",
        ) from exc

    rounds = _bounded_tool_count(max_tool_rounds, MAX_TOOL_ROUNDS, MAX_TOOL_ROUNDS, "max_tool_rounds")
    call_limit = _bounded_tool_count(max_tool_calls, MAX_TOOL_CALLS_PER_TASK, MAX_TOOL_CALLS_PER_TASK, "max_tool_calls")
    result_limit = _bounded_tool_count(max_tool_output_chars, MAX_TOOL_OUTPUT_CHARS, MAX_TOOL_OUTPUT_CHARS, "max_tool_output_chars")

    default_system = (
        "You are CRANE, a headless execution agent. Use the provided MCP tools "
        "when a task requires a tool action. When a tool is used, base the final "
        "answer on its actual returned content and do not claim unperformed effects."
    )
    prompt = system_prompt.strip() or default_system
    convo: List[Any] = _build_messages(user_input, system_prompt=prompt, messages=messages)
    observed: List[Dict[str, Any]] = []
    used_call_ids: set[str] = set()
    calls_executed = 0

    for round_index in range(rounds):
        try:
            # Non-streaming invoke keeps tool_calls and content in one final AIMessage.
            response = await asyncio.to_thread(bound_model.invoke, convo, stream=False)
        except Exception as exc:  # noqa: BLE001 - provider errors must be actionable
            raise ProviderError(
                "provider_invocation_failed",
                f"The configured Ollama model {config.model!r} did not complete the tool workflow.",
            ) from exc

        raw_calls = _extract_tool_calls(response)
        prepared_calls: List[Tuple[str, Dict[str, Any]]] = []
        message_calls: List[Dict[str, Any]] = []
        for index, raw_call in enumerate(raw_calls):
            call = dict(raw_call)
            raw_id = str(call.get("id") or "").strip()
            call_id = raw_id or f"call-{round_index + 1}-{index + 1}"
            candidate = call_id
            suffix = 2
            while candidate in used_call_ids:
                candidate = f"{call_id}-{suffix}"
                suffix += 1
            call_id = candidate
            used_call_ids.add(call_id)
            call["id"] = call_id
            prepared_calls.append((call_id, call))
            # LangChain requires dictionary args in AIMessage.tool_calls; pass
            # malformed/serialized args through to the execution parser instead.
            message_args: Dict[str, Any] = {}
            try:
                message_args = _parse_tool_arguments(call.get("args"))
            except (TypeError, ValueError):
                pass
            message_calls.append({
                "id": call_id,
                "name": str(call.get("name") or "invalid_tool_call"),
                "args": message_args,
            })

        content = getattr(response, "content", "")
        content_text = _content_to_text(content) if content is not None else ""
        convo.append(AIMessage(content=content_text, tool_calls=message_calls))
        if not prepared_calls:
            if not content_text.strip():
                raise ProviderError("provider_empty_response", "The configured model returned an empty response.")
            return {
                "execution_mode": "langchain_ollama_mcp",
                "provider": "ollama",
                "model": config.model,
                "model_output": content_text.strip(),
                "notice": None,
                "tool_calls": observed,
            }

        if calls_executed + len(prepared_calls) > call_limit:
            raise ProviderError(
                "tool_call_limit",
                f"The model exceeded the bounded MCP tool call limit ({MAX_TOOL_CALLS_PER_TASK}).",
            )

        for call_id, call in prepared_calls:
            calls_executed += 1
            name = str(call.get("name") or "").strip()
            tool, resolve_error = _resolve_tool(name, tools)
            if tool is None:
                error_text = resolve_error or "MCP tool call failed"
                convo.append(ToolMessage(content=error_text, tool_call_id=call_id, status="error"))
                _append_observed_call(
                    observed,
                    name=name,
                    call_id=call_id,
                    arguments={},
                    result=error_text,
                    status="failed",
                    error=error_text,
                )
                continue
            try:
                arguments = _parse_tool_arguments(call.get("args"))
            except (TypeError, ValueError) as exc:
                error_text = str(exc)
                convo.append(ToolMessage(content=error_text, tool_call_id=call_id, status="error"))
                _append_observed_call(
                    observed,
                    name=name,
                    call_id=call_id,
                    arguments={},
                    result=error_text,
                    status="failed",
                    error=error_text,
                )
                continue
            try:
                raw_output = await tool.ainvoke(arguments)
                tool_text = _bounded_tool_result(raw_output, result_limit)
            except Exception:  # noqa: BLE001 - never leak transport internals to the model
                tool_text = "MCP tool call failed"
            status, error = _classify_tool_result(tool_text)
            if status == "succeeded":
                convo.append(ToolMessage(content=tool_text, tool_call_id=call_id))
            else:
                convo.append(ToolMessage(content=tool_text, tool_call_id=call_id, status="error"))
            _append_observed_call(
                observed,
                name=name,
                call_id=call_id,
                arguments=arguments,
                result=tool_text,
                status=status,
                error=error,
            )

    raise ProviderError(
        "tool_loop_limit",
        f"The model exceeded the bounded MCP tool loop ({rounds} rounds).",
    )


def invoke(
    user_input: str,
    env: Optional[Mapping[str, str]] = None,
    system_prompt: str = "",
    messages: Optional[List[Dict[str, str]]] = None,
    context: Optional[Dict[str, Any]] = None,
    on_delta: Optional[Any] = None,
    json_mode: bool = False,
) -> Dict[str, Any]:
    """Execute the configured provider path. No fallback is permitted."""
    provider = selected_provider(env)
    if provider == "local":
        result = deterministic_result(user_input, provider_details(env), context=context)
        if on_delta and isinstance(result.get("model_output"), str) and result["model_output"]:
            try:
                on_delta(result["model_output"])
            except Exception:
                pass
        return result
    if provider == "ollama":
        return _invoke_ollama(
            user_input,
            env,
            system_prompt=system_prompt,
            messages=messages,
            on_delta=on_delta,
            json_mode=json_mode,
        )
    raise ProviderError(
        "provider_not_supported",
        f"Model provider {provider!r} is not supported. Configure CRANE_MODEL_PROVIDER=local or =ollama.",
    )


def ollama_installed() -> bool:
    """Report whether the Ollama CLI is discoverable on PATH."""
    return shutil.which("ollama") is not None


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


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
    required_modules = {
        "jsonschema",
        "langchain_core",
        "langchain_mcp_adapters",
        "langchain_ollama",
        "langgraph",
        "mcp",
        "ollama",
    }
    modules = {name: _module_available(name) for name in sorted(required_modules)}
    for name, available in modules.items():
        if not available:
            issues.append({
                "code": "python_module_missing",
                "message": f"Required Python module {name!r} is not available in this runtime.",
            })
    sqlite_available = _module_available("sqlite3")
    if not sqlite_available:
        issues.append({
            "code": "sqlite_module_missing",
            "message": "The packaged Python runtime does not include the sqlite3 module.",
        })
    runtime = {
        "available": all(modules.values()) and sqlite_available,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "executable": sys.executable,
        "sqlite_module": sqlite_available,
        "modules": modules,
    }
    mcp_modules = {
        "jsonschema": modules["jsonschema"],
        "langchain_mcp_adapters": modules["langchain_mcp_adapters"],
        "mcp": modules["mcp"],
    }
    degraded = bool(issues) or not runtime["available"]
    return {
        "protocol": "crane-agent/0.1.0",
        "status": "degraded" if degraded else "ok",
        "provider": provider,
        "runtime": runtime,
        "mcp": {"available": all(mcp_modules.values()), "modules": mcp_modules},
        "ollama": ollama,
        "issues": issues,
    }


invoke_with_tools = _invoke_ollama_with_tools

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_INVOKE_TIMEOUT_SECONDS",
    "DEFAULT_MODEL",
    "DEFAULT_NUM_CTX",
    "MAX_NUM_CTX",
    "OllamaConfig",
    "OLLAMA_DEPENDENCY",
    "ProviderError",
    "collect_diagnostics",
    "deterministic_result",
    "fetch_ollama_models",
    "invoke",
    "invoke_with_tools",
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
