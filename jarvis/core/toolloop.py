"""The OBSERVE → ACT → VERIFY loop around structured tool calling.

Shared by the foreground conversation and background agents. Bounded by a
maximum number of model steps and tool calls, so it can never loop forever.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarvis.llm.base import LLMError, Message, ToolCall
from jarvis.tools.base import Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from jarvis.core.services import Services

log = logging.getLogger(__name__)

TextCallback = Callable[[str], Awaitable[None]]
PhaseCallback = Callable[[str], Awaitable[None]]


@dataclass
class LoopOutcome:
    text: str
    messages: list[Message] = field(default_factory=list)
    results: list[tuple[ToolCall, ToolResult]] = field(default_factory=list)
    steps: int = 0
    hit_limit: bool = False
    finish_args: dict[str, Any] | None = None


class ToolLoop:
    def __init__(self, services: Services) -> None:
        self._services = services

    async def run(
        self,
        *,
        model: str,
        messages: list[Message],
        tools: list[Tool],
        ctx: ToolContext,
        on_text: TextCallback | None = None,
        on_phase: PhaseCallback | None = None,
        max_steps: int = 8,
        max_tool_calls: int = 10,
        finish_tool: dict[str, Any] | None = None,
    ) -> LoopOutcome:
        services = self._services
        convo = list(messages)
        outcome = LoopOutcome(text="")
        schemas: list[dict[str, Any]] | None = services.registry.schemas(tools) or None
        if finish_tool:
            schemas = [*(schemas or []), finish_tool]
        finish_name = finish_tool["function"]["name"] if finish_tool else None
        malformed_retries = 0
        total_calls = 0

        for step in range(max_steps + 1):
            ctx.cancel.raise_if_cancelled()
            final_step = step == max_steps
            if final_step:
                outcome.hit_limit = True
                convo.append({"role": "system", "content": (
                    "Step limit reached. Do not call more tools. Summarise truthfully what was completed, "
                    "what was not, and what remains.")})
            text_parts: list[str] = []
            calls: list[ToolCall] = []
            try:
                async for chunk in services.llm.stream_chat(
                    model, convo, None if final_step else schemas, cancel=ctx.cancel
                ):
                    if chunk.text:
                        text_parts.append(chunk.text)
                        if on_text:
                            await on_text(chunk.text)
                    if chunk.tool_calls:
                        calls = chunk.tool_calls
            except LLMError as exc:
                if exc.kind == "tool_use_failed" and not text_parts:
                    malformed_retries += 1
                    if malformed_retries == 1:
                        convo.append({"role": "system", "content": (
                            "Your previous tool call was malformed. Call the tool again with valid JSON arguments "
                            "that match its schema.")})
                    else:
                        schemas = None  # answer without tools rather than failing outright
                    continue
                raise
            outcome.steps = step + 1
            text = "".join(text_parts)
            assistant: Message = {"role": "assistant"}
            if text:
                assistant["content"] = text
            if calls:
                assistant["tool_calls"] = [c.to_message() for c in calls]
            if not text and not calls:
                assistant["content"] = ""
            convo.append(assistant)
            outcome.messages.append(assistant)
            if not calls:
                outcome.text = text
                return outcome

            if finish_name:
                finish = next((c for c in calls if c.name == finish_name), None)
                if finish:
                    args, _ = finish.parsed()
                    outcome.finish_args = args or {}
                    outcome.text = text
                    self._append_results(convo, outcome, [(finish, ToolResult.success("step reported"))])
                    return outcome

            if total_calls + len(calls) > max_tool_calls:
                budget = [(c, ToolResult.failure("Tool-call budget for this request is exhausted.", kind="budget"))
                          for c in calls]
                self._append_results(convo, outcome, budget)
                max_steps = step + 1  # force the summarising final step next
                continue
            total_calls += len(calls)
            if on_phase:
                await on_phase("executing")
            results = await services.executor.execute_calls(calls, ctx)
            self._append_results(convo, outcome, list(zip(calls, results, strict=True)))
            if on_phase:
                await on_phase("thinking")
        return outcome

    @staticmethod
    def _append_results(convo: list[Message], outcome: LoopOutcome,
                        pairs: list[tuple[ToolCall, ToolResult]]) -> None:
        for call, result in pairs:
            message = {"role": "tool", "tool_call_id": call.id, "content": result.to_llm()}
            convo.append(message)
            outcome.messages.append(message)
            outcome.results.append((call, result))
