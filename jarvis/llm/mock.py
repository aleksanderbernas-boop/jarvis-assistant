"""Deterministic LLM stand-ins.

`MockLLM`     — used in mock mode: keyword heuristics that exercise the real
                tool pipeline without any API key or network access.
`ScriptedLLM` — used by tests: replays an exact script of responses.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from jarvis.config import ModelSettings
from jarvis.core.cancellation import Cancelled, CancelToken
from jarvis.llm.base import Message, StreamChunk, ToolCall


def _tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    return {t["function"]["name"] for t in tools or []}


def goal_text(messages: list[Message]) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")


def _last_user_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


async def _stream_text(text: str, cancel: CancelToken | None) -> AsyncIterator[StreamChunk]:
    for word in re.findall(r"\S+\s*", text):
        if cancel and cancel.cancelled:
            raise Cancelled(cancel.reason)
        await asyncio.sleep(0.01)
        yield StreamChunk(text=word)


class MockLLM:
    name = "mock"

    def __init__(self) -> None:
        self._counter = 0

    def _call(self, name: str, args: dict[str, Any]) -> ToolCall:
        self._counter += 1
        return ToolCall(id=f"mock_{self._counter}", name=name, arguments=json.dumps(args))

    def _choose_tool(self, text: str, tools: set[str]) -> ToolCall | None:
        lowered = text.lower()
        rules: list[tuple[bool, str, Callable[[], dict[str, Any]]]] = [
            ("weather" in lowered or "umbrella" in lowered, "weather", dict),
            ("screen" in lowered, "describe_screen", lambda: {"question": text}),
            (any(k in lowered for k in ("search", "look up", "google", "find out")), "web_search",
             lambda: {"query": re.sub(r"(?i)^.*?(search( the web)? for|look up|google|find out)\s*", "", text) or text}),
            ("memory" in lowered or "ram" in lowered, "top_processes", lambda: {"sort_by": "memory"}),
            ("file" in lowered or "folder" in lowered, "fs_list", lambda: {"path": "~"}),
        ]
        for matched, name, make_args in rules:
            if matched and name in tools:
                return self._call(name, make_args())
        return None

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
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            results = []
            for message in reversed(messages):
                if message.get("role") != "tool":
                    break
                try:
                    payload = json.loads(message["content"])
                    results.append(str(payload.get("summary") or payload.get("error") or "")[:300])
                except (json.JSONDecodeError, AttributeError):
                    results.append(str(message["content"])[:200])
            text = "Mock mode result: " + " ".join(r for r in reversed(results) if r) + "."
            async for chunk in _stream_text(text, cancel):
                yield chunk
            yield StreamChunk(finish_reason="stop")
            return

        user_text = _last_user_text(messages)
        call = self._choose_tool(user_text, _tool_names(tools))
        if call:
            yield StreamChunk(tool_calls=[call], finish_reason="tool_calls")
            return
        text = (
            f"I'm running in mock mode, so my reasoning engine is simulated. "
            f"You said: “{user_text[:160]}”. Set JARVIS_MODE=live with an API key for real answers."
        )
        async for chunk in _stream_text(text, cancel):
            yield chunk
        yield StreamChunk(finish_reason="stop")

    async def complete(
        self,
        model: str,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        instruction = messages[-1].get("content", "") if messages else ""
        goal = _last_user_text(messages)[:200] or "the request"
        if "`Plan`" in instruction:
            return json.dumps(
                {
                    "goal": goal,
                    "steps": [
                        {"id": "s1", "description": f"Inspect what is needed for: {goal}", "depends_on": []},
                        {"id": "s2", "description": "Carry out the work and verify the result", "depends_on": ["s1"]},
                    ],
                }
            )
        if "`ResearchQueries`" in instruction:
            return json.dumps({"question": goal, "queries": [goal, f"{goal} comparison"]})
        if json_mode:
            return "{}"
        system = messages[0].get("content", "") if messages else ""
        if "research module" in str(system):
            sources = re.findall(r"^\[(\d+)\] (.+?) \((https?://[^)]+)\)", goal_text(messages), re.MULTILINE)
            listing = "\n".join(f"[{n}] {title} — {url}" for n, title, url in sources)
            return ("Simulated report (mock mode): no real research was performed. With a live model, this is "
                    f"where the cited comparison would appear.\n\nSources\n{listing}")
        return "(mock) Summary: this text is simulated because JARVIS is running in mock mode."

    async def list_models(self) -> list[str]:
        return list(ModelSettings().model_dump().values())

    async def close(self) -> None:
        return None


@dataclass
class ScriptedTurn:
    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    raw_tool_calls: list[ToolCall] = field(default_factory=list)
    error: Exception | None = None


class ScriptedLLM:
    """Replays `turns` for stream_chat and `completions` for complete()."""

    name = "scripted"

    def __init__(self, turns: list[ScriptedTurn] | None = None, completions: list[str] | None = None):
        self.turns = list(turns or [])
        self.completions = list(completions or [])
        self.calls: list[dict[str, Any]] = []
        self._ids = 0

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
        self.calls.append({"model": model, "messages": [dict(m) for m in messages], "tools": tools})
        turn = self.turns.pop(0) if self.turns else ScriptedTurn(text="(script exhausted)")
        if turn.error:
            raise turn.error
        async for chunk in _stream_text(turn.text, cancel):
            yield chunk
        calls = list(turn.raw_tool_calls)
        for name, args in turn.tool_calls:
            self._ids += 1
            calls.append(ToolCall(id=f"s_{self._ids}", name=name, arguments=json.dumps(args)))
        yield StreamChunk(tool_calls=calls, finish_reason="tool_calls" if calls else "stop")

    async def complete(
        self,
        model: str,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        self.calls.append({"model": model, "messages": messages, "complete": True})
        return self.completions.pop(0) if self.completions else "{}"

    async def list_models(self) -> list[str]:
        return list(ModelSettings().model_dump().values())

    async def close(self) -> None:
        return None
