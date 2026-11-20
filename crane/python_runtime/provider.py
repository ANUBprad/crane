"""Explicit model-provider selection for the Python agent runtime.

Local deterministic execution is the default. Hosted providers are never
selected implicitly. When Ollama is configured, LangChain's maintained Ollama
integration is the only supported provider boundary.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional


class ProviderError(RuntimeError):
    """An actionable provider configuration or availability failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def selected_provider() -> str:
    return os.environ.get("CRANE_MODEL_PROVIDER", "local").strip().lower()


def provider_details() -> Dict[str, Any]:
    provider = selected_provider()
    details: Dict[str, Any] = {"name": provider, "local": provider in {"local", "ollama"}}
    if provider == "ollama":
        details["model"] = os.environ.get("CRANE_MODEL", "")
        details["base_url_configured"] = bool(os.environ.get("OLLAMA_HOST"))
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


def invoke(user_input: str) -> Dict[str, Any]:
    """Execute the configured provider path. No fallback is permitted."""
    provider = selected_provider()
    details = provider_details()
    if provider == "local":
        return deterministic_result(user_input, details)
    if provider == "ollama":
        return _invoke_ollama(user_input, details)
    raise ProviderError(
        "provider_not_supported",
        f"Model provider {provider!r} is not supported. Configure CRANE_MODEL_PROVIDER=local or =ollama.",
    )


def _invoke_ollama(user_input: str, details: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from langchain_ollama import ChatOllama  # type: ignore
    except ImportError as exc:
        raise ProviderError(
            "provider_unavailable",
            "Ollama provider requested but langchain-ollama is not installed. Install the pinned Ollama provider dependency or use CRANE_MODEL_PROVIDER=local.",
        ) from exc

    model = os.environ.get("CRANE_MODEL", "").strip()
    if not model:
        raise ProviderError(
            "provider_not_configured",
            "CRANE_MODEL_PROVIDER=ollama requires CRANE_MODEL to name a locally installed model.",
        )
    kwargs: Dict[str, Any] = {"model": model, "temperature": 0}
    base_url = os.environ.get("OLLAMA_HOST", "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    try:
        response = ChatOllama(**kwargs).invoke(normalize_input(user_input))
    except Exception as exc:  # noqa: BLE001 - provider errors must be actionable, not leaked verbatim
        raise ProviderError(
            "provider_unavailable",
            "The configured Ollama model could not be reached. Start Ollama locally and confirm CRANE_MODEL is installed.",
        ) from exc
    content = getattr(response, "content", response)
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("provider_empty_response", "The configured model returned an empty response.")
    return {
        "execution_mode": "langchain_ollama",
        "provider": "ollama",
        "model": model,
        "model_output": content.strip(),
        "notice": None,
    }
