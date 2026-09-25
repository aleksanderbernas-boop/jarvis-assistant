"""Cooperative cancellation tokens that propagate through LLM streams, tools and tasks."""

from __future__ import annotations

import asyncio


class Cancelled(Exception):
    """Raised when work observes its cancellation token."""


class CancelToken:
    def __init__(self, parent: CancelToken | None = None) -> None:
        self._event = asyncio.Event()
        self.reason = ""
        self._children: list[CancelToken] = []
        if parent is not None:
            parent._children.append(self)
            if parent.cancelled:
                self.cancel(parent.reason)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "cancelled") -> None:
        if self._event.is_set():
            return
        self.reason = reason
        self._event.set()
        for child in self._children:
            child.cancel(reason)

    def child(self) -> CancelToken:
        return CancelToken(parent=self)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled(self.reason)

    async def wait(self) -> None:
        await self._event.wait()
