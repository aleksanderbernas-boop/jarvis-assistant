"""Provider-neutral LLM interface.

Messages use the OpenAI chat format (the de-facto standard supported by Groq,
OpenAI, OpenRouter, Ollama, LM Studio...).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from jarvis.core.cancellation import CancelToken

log = logging.getLogger(__name__)

Message = dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = ""

    def parsed(self) -> tuple[dict[str, Any] | None, str | None]:
        """Return (args, error). Never raises on malformed model output."""
        raw = self.arguments.strip() or "{}"
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"arguments are not valid JSON: {exc.msg}"
        if not isinstance(value, dict):
            return None, "arguments must be a JSON object"
        return value, None

    def to_message(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": self.arguments or "{}"}}


@dataclass
class StreamChunk:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None


class LLMError(Exception):
    """A classified LLM failure. `kind` drives retry and user messaging."""

    def __init__(self, message: str, kind: str = "error", retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.status = status

    def user_message(self) -> str:
        return {
            "auth": "My reasoning engine rejected the API key. Check GROQ_API_KEY (or your provider key) in .env.",
            "rate_limit": "The language model provider is rate-limiting me. Give it a moment.",
            "unavailable": "The language model provider is unavailable at the moment.",
            "timeout": "The language model took too long to respond.",
            "network": "I can't reach the language model provider — the network appears to be down.",
            "model_not_found": "The configured model isn't available on this provider.",
        }.get(self.kind, "My reasoning engine returned an error.")


class LLMProvider(Protocol):
    name: str

    def stream_chat(
        self,
        model: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cancel: CancelToken | None = None,
    ) -> AsyncIterator[StreamChunk]: ...

    async def complete(
        self,
        model: str,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str: ...

    async def list_models(self) -> list[str]: ...

    async def close(self) -> None: ...


T = TypeVar("T", bound=BaseModel)


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


async def complete_structured(
    llm: LLMProvider,
    model: str,
    messages: list[Message],
    schema: type[T],
    *,
    temperature: float = 0.2,
    max_tokens: int | None = 2000,
) -> T:
    """Ask for JSON matching `schema`, validate it, and retry once with the error.

    Structured output is validated by pydantic — never regex-parsed.
    """
    schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
    instruction = (
        f"Respond ONLY with a JSON object for schema `{schema.__name__}` "
        f"matching this JSON Schema: {schema_json}"
    )
    convo = [*messages, {"role": "system", "content": instruction}]
    last_error = ""
    for attempt in range(2):
        raw = await llm.complete(model, convo, temperature=temperature, max_tokens=max_tokens, json_mode=True)
        try:
            return schema.model_validate_json(_extract_json(raw))
        except ValidationError as exc:
            last_error = str(exc)[:800]
            log.warning("structured output invalid (attempt %d) for %s", attempt + 1, schema.__name__)
            convo = [
                *convo,
                {"role": "assistant", "content": raw[:4000]},
                {"role": "user", "content": f"That JSON was invalid: {last_error}. Return corrected JSON only."},
            ]
    raise LLMError(f"model did not produce valid {schema.__name__}: {last_error}", kind="malformed")
