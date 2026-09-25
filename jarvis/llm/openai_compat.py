"""Streaming chat client for OpenAI-compatible APIs (Groq, OpenAI, Ollama, LM Studio...)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx

from jarvis.core.cancellation import Cancelled, CancelToken
from jarvis.llm.base import LLMError, Message, StreamChunk, ToolCall

log = logging.getLogger(__name__)


def _classify(status: int, body: str) -> LLMError:
    lowered = body.lower()
    if status in (401, 403):
        return LLMError("authentication failed", kind="auth", status=status)
    if status == 404 or "model_not_found" in lowered or "does not exist" in lowered or "decommissioned" in lowered:
        return LLMError(f"model not found: {body[:300]}", kind="model_not_found", status=status)
    if status == 429:
        return LLMError("rate limited", kind="rate_limit", retryable=True, status=status)
    if status >= 500:
        return LLMError(f"provider error {status}", kind="unavailable", retryable=True, status=status)
    if "tool_use_failed" in lowered:
        return LLMError(f"model produced an invalid tool call: {body[:300]}", kind="tool_use_failed",
                        retryable=True, status=status)
    return LLMError(f"request rejected ({status}): {body[:300]}", kind="bad_request", status=status)


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        name: str = "openai_compatible",
        timeout_s: float = 60.0,
        max_retries: int = 2,
        default_temperature: float = 0.4,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._temperature = default_temperature
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(8.0, 0.75 * (2**attempt)) + random.uniform(0, 0.25)
        if retry_after:
            try:
                delay = min(20.0, float(retry_after))
            except ValueError:
                pass
        await asyncio.sleep(delay)

    async def stream_chat(
        self,
        model: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cancel: CancelToken | None = None,
    ) -> AsyncIterator[StreamChunk]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": self._temperature if temperature is None else temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        for attempt in range(self._max_retries + 1):
            emitted = False
            try:
                async with self._client.stream("POST", f"{self._base_url}/chat/completions", json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        error = _classify(resp.status_code, body)
                        if error.retryable and attempt < self._max_retries:
                            log.warning("LLM %s (attempt %d); retrying", error.kind, attempt + 1)
                            await self._backoff(attempt, resp.headers.get("retry-after"))
                            continue
                        raise error
                    async for chunk in self._parse_sse(resp, cancel):
                        emitted = True
                        yield chunk
                    return
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                if emitted or attempt >= self._max_retries:
                    raise LLMError(f"network error: {exc}", kind="network", retryable=True) from exc
                await self._backoff(attempt, None)
            except httpx.TimeoutException as exc:
                if emitted or attempt >= self._max_retries:
                    raise LLMError("request timed out", kind="timeout", retryable=True) from exc
                await self._backoff(attempt, None)

    async def _parse_sse(self, resp: httpx.Response, cancel: CancelToken | None) -> AsyncIterator[StreamChunk]:
        pending: dict[int, ToolCall] = {}
        finish: str | None = None
        async for line in resp.aiter_lines():
            if cancel and cancel.cancelled:
                raise Cancelled(cancel.reason)
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                log.debug("skipping malformed SSE line")
                continue
            if "error" in event and not event.get("choices"):
                message = json.dumps(event["error"])[:300]
                raise _classify(400, message)
            for choice in event.get("choices", []):
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    yield StreamChunk(text=delta["content"])
                for tc in delta.get("tool_calls") or []:
                    index = tc.get("index", len(pending))
                    call = pending.setdefault(index, ToolCall(id=tc.get("id") or f"call_{index}", name=""))
                    if tc.get("id"):
                        call.id = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        call.name += fn["name"]
                    if fn.get("arguments"):
                        call.arguments += fn["arguments"]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        calls = [pending[i] for i in sorted(pending) if pending[i].name]
        yield StreamChunk(tool_calls=calls, finish_reason=finish or ("tool_calls" if calls else "stop"))

    async def complete(
        self,
        model: str,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._temperature if temperature is None else temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.post(f"{self._base_url}/chat/completions", json=payload)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise LLMError("request timed out", kind="timeout") from exc
                await self._backoff(attempt, None)
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise LLMError(f"network error: {exc}", kind="network") from exc
                await self._backoff(attempt, None)
                continue
            if resp.status_code == 200:
                data = resp.json()
                return str(data["choices"][0]["message"].get("content") or "")
            error = _classify(resp.status_code, resp.text)
            if json_mode and error.kind == "bad_request" and "json" in resp.text.lower():
                # Some models reject json_object mode — retry once without it.
                payload.pop("response_format", None)
                json_mode = False
                continue
            if error.retryable and attempt < self._max_retries:
                await self._backoff(attempt, resp.headers.get("retry-after"))
                continue
            raise error
        raise LLMError("exhausted retries", kind="unavailable")

    async def list_models(self) -> list[str]:
        try:
            resp = await self._client.get(f"{self._base_url}/models", timeout=8.0)
        except httpx.TransportError as exc:
            raise LLMError(f"network error: {exc}", kind="network") from exc
        if resp.status_code != 200:
            raise _classify(resp.status_code, resp.text)
        return sorted(m["id"] for m in resp.json().get("data", []) if "id" in m)
