"""Async publish/subscribe bus connecting the brain to the HUD and to internal observers."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from jarvis.api.schemas import ActivityEvent, Event

log = logging.getLogger(__name__)

Listener = Callable[[Event], Awaitable[None] | None]

# Events that are fine to drop for a slow client; everything else must arrive.
_DROPPABLE = {"system_metrics", "response_delta"}


@dataclass(eq=False)
class Subscription:
    session_id: str | None
    queue: asyncio.Queue[Event] = field(default_factory=lambda: asyncio.Queue(maxsize=2000))


class EventBus:
    """Delivers events to WebSocket subscribers and in-process listeners.

    An event with a `session_id` goes to subscribers of that session; an event
    without one is broadcast to every subscriber.
    """

    def __init__(self, activity_history: int = 100) -> None:
        self._subscriptions: set[Subscription] = set()
        self._listeners: list[Listener] = []
        self.activity: deque[ActivityEvent] = deque(maxlen=activity_history)

    def subscribe(self, session_id: str | None) -> Subscription:
        sub = Subscription(session_id)
        self._subscriptions.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subscriptions.discard(sub)

    def rebind(self, sub: Subscription, session_id: str) -> None:
        sub.session_id = session_id

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def has_subscribers(self, session_id: str) -> bool:
        return any(s.session_id == session_id for s in self._subscriptions)

    async def publish(self, event: Event) -> None:
        if isinstance(event, ActivityEvent):
            self.activity.append(event)
        for sub in list(self._subscriptions):
            if event.session_id is not None and sub.session_id not in (None, event.session_id):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                if event.type not in _DROPPABLE:
                    log.warning("Subscriber queue full; dropping %s event", event.type)
        for listener in self._listeners:
            try:
                result = listener(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("Event listener failed for %s", event.type)

    def activity_for(self, session_id: str | None) -> list[ActivityEvent]:
        return [a for a in self.activity if a.session_id in (None, session_id)]

    async def activity_event(
        self, text: str, kind: str = "info", session_id: str | None = None, ref: str | None = None
    ) -> None:
        await self.publish(ActivityEvent(text=text, kind=kind, session_id=session_id, ref=ref))  # type: ignore[arg-type]
