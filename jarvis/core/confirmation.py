"""Explicit confirmation gate for risky actions.

Silence is never approval: a request that times out is treated as declined.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from jarvis.api.schemas import ConfirmationRequestEvent, ConfirmationResolvedEvent, RiskLevel, utcnow
from jarvis.core.events import EventBus

log = logging.getLogger(__name__)

_YES = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|proceed|go ahead|go for it|do it|confirm(ed)?|affirmative|approved?|"
    r"please do|absolutely|correct|that's right|tak)\b"
)
_NO = re.compile(
    r"\b(no|nope|nah|don't|do not|cancel|stop|abort|negative|never ?mind|hold on|wait|not now|nie)\b"
)


def parse_confirmation(text: str) -> bool | None:
    """Interpret a short reply as yes/no. Returns None when it isn't a clear answer."""
    cleaned = re.sub(r"[^\w\s']", " ", text.lower()).strip()
    cleaned = re.sub(r"^(jarvis|hey jarvis)\s+", "", cleaned)
    if not cleaned or len(cleaned.split()) > 8:
        return None
    if _NO.search(cleaned):
        return False  # any hint of "no" wins — the safe interpretation
    if _YES.search(cleaned):
        return True
    return None


@dataclass
class PendingConfirmation:
    event: ConfirmationRequestEvent
    future: asyncio.Future[bool]
    task_id: str | None = None


RequestHook = Callable[[PendingConfirmation], Awaitable[None]]
ResolveHook = Callable[[PendingConfirmation, bool], Awaitable[None]]


class ConfirmationManager:
    def __init__(self, bus: EventBus, timeout_s: float) -> None:
        self._bus = bus
        self._timeout_s = timeout_s
        self._pending: dict[str, PendingConfirmation] = {}
        self.on_request: RequestHook | None = None
        self.on_resolve: ResolveHook | None = None

    async def request(
        self,
        session_id: str | None,
        tool: str,
        risk: RiskLevel,
        summary: str,
        details: list[str] | None = None,
        task_id: str | None = None,
    ) -> bool:
        confirmation_id = uuid.uuid4().hex[:12]
        event = ConfirmationRequestEvent(
            session_id=session_id,
            confirmation_id=confirmation_id,
            tool=tool,
            risk=risk,
            summary=summary,
            details=details or [],
            expires_at=utcnow() + timedelta(seconds=self._timeout_s),
        )
        pending = PendingConfirmation(event, asyncio.get_running_loop().create_future(), task_id)
        self._pending[confirmation_id] = pending
        await self._bus.publish(event)
        if self.on_request:
            await self.on_request(pending)
        approved, reason = False, "timeout"
        try:
            approved = await asyncio.wait_for(asyncio.shield(pending.future), timeout=self._timeout_s)
            reason = "approved" if approved else "declined"
        except TimeoutError:
            log.info("confirmation %s timed out — treating as declined", confirmation_id)
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        finally:
            self._pending.pop(confirmation_id, None)
            if not pending.future.done():
                pending.future.set_result(False)
            await self._bus.publish(
                ConfirmationResolvedEvent(
                    session_id=session_id, confirmation_id=confirmation_id, approved=approved, reason=reason
                )
            )
            if self.on_resolve:
                await self.on_resolve(pending, approved)
        return approved

    def resolve(self, confirmation_id: str, approved: bool) -> bool:
        pending = self._pending.get(confirmation_id)
        if not pending or pending.future.done():
            return False
        pending.future.set_result(approved)
        return True

    def pending_for(self, session_id: str | None) -> PendingConfirmation | None:
        matches = [p for p in self._pending.values() if p.event.session_id == session_id]
        return matches[-1] if matches else None

    def decline_all(self, session_id: str | None) -> int:
        count = 0
        for pending in list(self._pending.values()):
            if pending.event.session_id == session_id and not pending.future.done():
                pending.future.set_result(False)
                count += 1
        return count
