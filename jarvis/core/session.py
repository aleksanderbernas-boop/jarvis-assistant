"""Conversation sessions: shared by voice and text, persisted, and restored on reconnect."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from jarvis.api.schemas import AssistantState, HistoryItem, utcnow
from jarvis.core.cancellation import CancelToken
from jarvis.core.state import StateMachine
from jarvis.llm.base import Message
from jarvis.storage.db import Database

StateHook = Callable[["Session", AssistantState, AssistantState, str], Awaitable[None]]


class Session:
    def __init__(self, session_id: str, created_at: datetime, on_state: StateHook) -> None:
        self.id = session_id
        self.created_at = created_at
        self.last_activity = created_at
        self.history: list[Message] = []
        self.summary = ""

        async def _changed(state: AssistantState, previous: AssistantState, reason: str) -> None:
            await on_state(self, state, previous, reason)

        self.state = StateMachine(on_change=_changed)
        self.turn: asyncio.Task[None] | None = None
        self.turn_cancel: CancelToken | None = None
        self.current_response_id: str | None = None
        self.muted_response_id: str | None = None
        self.voice_output = True
        self.mic_active = False
        self.mic_error: str | None = None
        self.tts_playing = False
        self.conversation_open_until = 0.0  # monotonic; follow-ups need no wake word before this
        self.last_speech_stop = 0.0
        self.last_memory_id: str | None = None
        self.sticky_categories: set[str] = set()
        self.pending_announcements: list[str] = []
        self.recent_speech: deque[tuple[float, str]] = deque(maxlen=16)

    @property
    def busy(self) -> bool:
        return self.turn is not None and not self.turn.done()

    def touch(self) -> None:
        self.last_activity = utcnow()

    def open_conversation(self, seconds: float) -> None:
        self.conversation_open_until = time.monotonic() + seconds

    def in_conversation(self) -> bool:
        return time.monotonic() < self.conversation_open_until or self.busy or self.tts_playing

    def spoke(self, sentence: str) -> None:
        self.recent_speech.append((time.monotonic(), sentence))

    def is_echo(self, heard: str, within_s: float = 30.0) -> bool:
        """True if a transcript is just JARVIS's own recent speech picked up by the microphone."""
        words = set(re.findall(r"[a-z0-9']{2,}", heard.lower()))
        if len(words) < 3:
            return False
        cutoff = time.monotonic() - within_s
        spoken = set()
        for at, sentence in self.recent_speech:
            if at >= cutoff:
                spoken |= set(re.findall(r"[a-z0-9']{2,}", sentence.lower()))
        return len(words & spoken) / len(words) >= 0.8

    def window(self, size: int) -> list[Message]:
        """The most recent messages, never starting on an orphaned tool result."""
        recent = self.history[-size:]
        while recent and recent[0].get("role") == "tool":
            recent = recent[1:]
        return recent

    def transcript(self, limit: int = 40) -> list[HistoryItem]:
        items: list[HistoryItem] = []
        for message in self.history:
            role, content = message.get("role"), message.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                items.append(HistoryItem(role=role, text=content, timestamp=message.get("_ts") or self.created_at))
        return items[-limit:]


class SessionManager:
    RESTORE_MESSAGES = 30

    def __init__(self, db: Database, on_state: StateHook) -> None:
        self._db = db
        self._on_state = on_state
        self._sessions: dict[str, Session] = {}

    def get(self, session_id: str | None) -> Session | None:
        return self._sessions.get(session_id or "")

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def get_or_create(self, session_id: str | None) -> Session:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        row = self._db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,)) if session_id else None
        if row:
            session = Session(row["id"], datetime.fromisoformat(row["created_at"]), self._on_state)
            session.summary = row["summary"]
            rows = self._db.query(
                "SELECT role, content, created_at FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session.id, self.RESTORE_MESSAGES),
            )
            session.history = [
                {"role": r["role"], "content": r["content"], "_ts": datetime.fromisoformat(r["created_at"])}
                for r in reversed(rows)
            ]
        else:
            session = Session(session_id or uuid.uuid4().hex[:16], utcnow(), self._on_state)
            self._db.execute("INSERT INTO sessions(id, created_at, last_activity) VALUES (?, ?, ?)",
                             (session.id, session.created_at.isoformat(), session.created_at.isoformat()))
        self._sessions[session.id] = session
        return session

    def add_message(self, session: Session, message: Message, persist: bool = True) -> None:
        stamped: dict[str, Any] = {**message, "_ts": utcnow()}
        session.history.append(stamped)
        session.touch()
        content = message.get("content")
        if persist and message.get("role") in {"user", "assistant"} and isinstance(content, str) and content.strip():
            self._db.execute("INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                             (session.id, message["role"], content, stamped["_ts"].isoformat()))
            self._db.execute("UPDATE sessions SET last_activity = ? WHERE id = ?",
                             (stamped["_ts"].isoformat(), session.id))

    def save_summary(self, session: Session) -> None:
        self._db.execute("UPDATE sessions SET summary = ? WHERE id = ?", (session.summary, session.id))


def llm_messages(messages: list[Message]) -> list[Message]:
    """Strip internal bookkeeping keys before sending history to a model."""
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
