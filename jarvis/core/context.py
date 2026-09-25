"""Context-window management: what the model sees for each request.

    system prompt (identity, personality, safety, profile, relevant memories,
    tasks, live context) + rolling summary + recent messages.

Old history is summarised by the fast model instead of growing forever.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from jarvis.api.schemas import ResponseMode
from jarvis.core.session import Session, llm_messages
from jarvis.llm.base import LLMError, Message
from jarvis.llm.models import ModelTier
from jarvis.llm.prompts import PromptContext, build_system_prompt

if TYPE_CHECKING:
    from jarvis.core.services import Services

log = logging.getLogger(__name__)

TOOL_RESULT_HISTORY_CHARS = 2500


class ContextBuilder:
    def __init__(self, services: Services) -> None:
        self._services = services

    async def _active_window_title(self) -> str | None:
        platform = self._services.platform
        try:
            window = await asyncio.wait_for(platform.call(platform.active_window), timeout=0.6)
        except Exception:  # not supported / slow — context is best-effort
            return None
        return window.title if window else None

    async def build(self, session: Session, query: str, mode: ResponseMode, voice: bool) -> list[Message]:
        services = self._services
        settings = services.settings
        recent_user = " ".join(
            m["content"] for m in session.history[-6:] if m.get("role") == "user" and isinstance(m.get("content"), str)
        )
        memories = services.memory.context_for(f"{query} {recent_user}")
        tasks = [
            f"[{t.id}] {t.description} — {t.status.value}, {t.progress:.0%}"
            + (f", now: {t.current_step}" if t.current_step else "")
            for t in services.tasks.active(session.id)
        ]
        now = datetime.now().astimezone()
        system_context = [
            f"Local time: {now:%A %d %B %Y, %H:%M} ({now.tzname()})",
            f"Operating system: {services.platform.name}",
        ]
        if settings.user.location_name:
            system_context.append(f"User's location: {settings.user.location_name}")
        window = await self._active_window_title()
        if window:
            system_context.append(f"Active window: {window}")
        if settings.mock:
            system_context.append("MOCK MODE: tools are simulated; say so when reporting results.")
        prompt = build_system_prompt(PromptContext(
            honorific=settings.user.honorific,
            user_name=settings.user.name,
            memories=[f"({m.kind}, {m.updated_at[:10]}) {m.content}" for m in memories],
            tasks=tasks,
            system_context=system_context,
            conversation_summary=session.summary,
            mode=mode,
            voice=voice,
        ))
        history = llm_messages(session.window(settings.history_window))
        for message in history:
            if message.get("role") == "tool" and len(message.get("content", "")) > TOOL_RESULT_HISTORY_CHARS:
                message["content"] = message["content"][:TOOL_RESULT_HISTORY_CHARS] + "…[truncated]"
        return [{"role": "system", "content": prompt}, *history]

    async def compact(self, session: Session) -> None:
        """Summarise old turns once history grows past twice the window."""
        window = self._services.settings.history_window
        if len(session.history) <= window * 2:
            return
        cut = len(session.history) - window
        while cut < len(session.history) and session.history[cut].get("role") != "user":
            cut += 1
        old, keep = session.history[:cut], session.history[cut:]
        lines = []
        for message in old:
            role, content = message.get("role"), message.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                lines.append(f"{role}: {content[:500]}")
            elif role == "tool":
                lines.append(f"tool result: {str(content)[:200]}")
        transcript = "\n".join(lines)[-12_000:]
        try:
            summary = await self._services.llm.complete(
                self._services.models.model_for(ModelTier.FAST),
                [{"role": "system", "content": (
                    "Summarise this conversation for an assistant's memory in under 180 words. Keep facts, "
                    "decisions, file paths, names, numbers, open questions and user constraints. No preamble.")},
                 {"role": "user", "content": f"Existing summary:\n{session.summary or '(none)'}\n\nNew turns:\n{transcript}"}],
                max_tokens=400,
                temperature=0.1,
            )
        except LLMError as exc:
            log.warning("history summarisation failed (%s); keeping a truncated summary", exc)
            summary = (session.summary + "\n" + transcript)[-1500:]
        session.summary = summary.strip()
        session.history = keep
        self._services.sessions.save_summary(session)
