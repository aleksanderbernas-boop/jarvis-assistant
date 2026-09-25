"""Construct the configured LLM provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from jarvis.config import Settings
from jarvis.core.cancellation import CancelToken
from jarvis.llm.base import LLMError, LLMProvider, Message, StreamChunk
from jarvis.llm.mock import MockLLM
from jarvis.llm.openai_compat import OpenAICompatibleProvider


class UnconfiguredLLM:
    """Stand-in when no API key is set: every call fails with a clear, actionable error."""

    def __init__(self, provider: str) -> None:
        self.name = provider
        key = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider, "JARVIS_LLM_API_KEY")
        self._message = f"no API key configured for {provider} — set {key} in .env"

    async def stream_chat(self, model: str, messages: list[Message], tools: list[dict[str, Any]] | None = None, *,
                          temperature: float | None = None, max_tokens: int | None = None,
                          cancel: CancelToken | None = None) -> AsyncIterator[StreamChunk]:
        raise LLMError(self._message, kind="auth")
        yield StreamChunk()  # pragma: no cover — makes this an async generator

    async def complete(self, model: str, messages: list[Message], *, temperature: float | None = None,
                       max_tokens: int | None = None, json_mode: bool = False) -> str:
        raise LLMError(self._message, kind="auth")

    async def list_models(self) -> list[str]:
        raise LLMError(self._message, kind="auth")

    async def close(self) -> None:
        return None


def build_llm(settings: Settings) -> LLMProvider:
    llm = settings.llm
    if settings.mock or llm.provider == "mock":
        return MockLLM()
    needs_key = llm.provider in {"groq", "openai"}
    if needs_key and not llm.api_key:
        return UnconfiguredLLM(llm.provider)
    return OpenAICompatibleProvider(
        llm.base_url,
        llm.api_key.get_secret_value() if llm.api_key else None,
        name=llm.provider,
        timeout_s=llm.timeout_s,
        max_retries=llm.max_retries,
        default_temperature=llm.temperature,
    )
