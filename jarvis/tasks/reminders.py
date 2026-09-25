"""Reminders and timers, persisted so they survive restarts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from jarvis.api.schemas import utcnow
from jarvis.storage.db import Database

log = logging.getLogger(__name__)

Repeat = Literal["none", "daily", "weekdays", "weekly"]
FireHook = Callable[["Reminder", bool], Awaitable[None]]


@dataclass
class Reminder:
    id: str
    text: str
    due_at: datetime
    repeat: str
    status: str
    session_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "due_at": self.due_at.astimezone().isoformat(timespec="minutes"),
            "repeat": self.repeat,
            "status": self.status,
        }


def _next_occurrence(due: datetime, repeat: str) -> datetime | None:
    if repeat == "daily":
        return due + timedelta(days=1)
    if repeat == "weekly":
        return due + timedelta(weeks=1)
    if repeat == "weekdays":
        nxt = due + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt
    return None


class ReminderService:
    MISSED_GRACE = timedelta(hours=12)

    def __init__(self, db: Database) -> None:
        self._db = db
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self.on_fire: FireHook | None = None

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop(), name="reminders")

    async def shutdown(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task

    def create(self, text: str, due_at: datetime, *, session_id: str | None, repeat: Repeat = "none") -> Reminder:
        reminder = Reminder(uuid.uuid4().hex[:8], text.strip() or "Reminder", due_at.astimezone(), repeat,
                            "pending", session_id)
        self._db.execute(
            "INSERT INTO reminders(id, session_id, text, due_at, repeat, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (reminder.id, session_id, reminder.text, reminder.due_at.isoformat(), repeat, "pending",
             utcnow().isoformat()),
        )
        self._wake.set()
        return reminder

    def pending(self) -> list[Reminder]:
        rows = self._db.query("SELECT * FROM reminders WHERE status = 'pending' ORDER BY due_at")
        return [Reminder(r["id"], r["text"], datetime.fromisoformat(r["due_at"]), r["repeat"], r["status"],
                         r["session_id"]) for r in rows]

    def cancel(self, reminder_id: str) -> bool:
        changed = self._db.execute("UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                                   (reminder_id,))
        self._wake.set()
        return changed > 0

    async def _loop(self) -> None:
        while True:
            try:
                await self.fire_due()
            except Exception:
                log.exception("reminder loop error")
            upcoming = self.pending()
            delay = 30.0
            if upcoming:
                delay = max(0.2, min(delay, (upcoming[0].due_at - datetime.now().astimezone()).total_seconds()))
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)

    async def fire_due(self) -> None:
        now = datetime.now().astimezone()
        for reminder in self.pending():
            if reminder.due_at > now:
                break
            missed = now - reminder.due_at > self.MISSED_GRACE
            nxt = _next_occurrence(reminder.due_at, reminder.repeat)
            while nxt and nxt <= now:
                nxt = _next_occurrence(nxt, reminder.repeat)
            if nxt:
                self._db.execute("UPDATE reminders SET due_at = ? WHERE id = ?", (nxt.isoformat(), reminder.id))
            else:
                self._db.execute("UPDATE reminders SET status = 'fired' WHERE id = ?", (reminder.id,))
            if self.on_fire:
                await self.on_fire(reminder, missed)
