"""The assistant state machine.

A single authoritative state per session with an explicit transition table.
Every change is published so the HUD always reflects the real state.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from jarvis.api.schemas import AssistantState as S

log = logging.getLogger(__name__)

TRANSITIONS: dict[S, frozenset[S]] = {
    S.STANDBY: frozenset({S.LISTENING, S.THINKING, S.BACKGROUND_TASK, S.SPEAKING, S.ERROR}),
    S.LISTENING: frozenset({S.THINKING, S.STANDBY, S.SPEAKING, S.INTERRUPTED, S.BACKGROUND_TASK, S.ERROR}),
    S.THINKING: frozenset(
        {
            S.PLANNING,
            S.EXECUTING,
            S.SPEAKING,
            S.WAITING_FOR_CONFIRMATION,
            S.INTERRUPTED,
            S.ERROR,
            S.STANDBY,
            S.LISTENING,
            S.BACKGROUND_TASK,
        }
    ),
    S.PLANNING: frozenset(
        {S.EXECUTING, S.THINKING, S.SPEAKING, S.WAITING_FOR_CONFIRMATION, S.INTERRUPTED, S.ERROR,
         S.STANDBY, S.LISTENING, S.BACKGROUND_TASK}
    ),
    S.EXECUTING: frozenset(
        {S.THINKING, S.SPEAKING, S.WAITING_FOR_CONFIRMATION, S.INTERRUPTED, S.ERROR, S.STANDBY,
         S.LISTENING, S.BACKGROUND_TASK}
    ),
    S.SPEAKING: frozenset(
        {S.LISTENING, S.STANDBY, S.INTERRUPTED, S.THINKING, S.EXECUTING, S.BACKGROUND_TASK,
         S.WAITING_FOR_CONFIRMATION, S.ERROR}
    ),
    S.WAITING_FOR_CONFIRMATION: frozenset(
        {S.EXECUTING, S.THINKING, S.SPEAKING, S.INTERRUPTED, S.STANDBY, S.LISTENING, S.BACKGROUND_TASK, S.ERROR}
    ),
    S.INTERRUPTED: frozenset({S.LISTENING, S.THINKING, S.STANDBY, S.BACKGROUND_TASK, S.SPEAKING}),
    S.BACKGROUND_TASK: frozenset({S.LISTENING, S.THINKING, S.STANDBY, S.SPEAKING, S.ERROR}),
    S.ERROR: frozenset({S.STANDBY, S.LISTENING, S.THINKING, S.BACKGROUND_TASK, S.SPEAKING}),
}

# States that mean "idle, ready for input".
IDLE_STATES = frozenset({S.STANDBY, S.LISTENING, S.BACKGROUND_TASK})

StateListener = Callable[[S, S, str], Awaitable[None]]


class InvalidTransition(RuntimeError):
    pass


class StateMachine:
    def __init__(self, on_change: StateListener | None = None, initial: S = S.STANDBY) -> None:
        self._state = initial
        self._on_change = on_change

    @property
    def state(self) -> S:
        return self._state

    def can_transition(self, target: S) -> bool:
        return target == self._state or target in TRANSITIONS[self._state]

    async def transition(self, target: S, reason: str = "") -> None:
        """Move to `target`. Raises `InvalidTransition` for illegal moves."""
        if target == self._state:
            return
        if target not in TRANSITIONS[self._state]:
            raise InvalidTransition(f"{self._state} → {target} is not allowed ({reason})")
        previous, self._state = self._state, target
        log.debug("state %s → %s (%s)", previous, target, reason)
        if self._on_change:
            await self._on_change(target, previous, reason)

    async def set(self, target: S, reason: str = "") -> None:
        """Transition if legal; otherwise log and route through a legal path.

        Used by the orchestrator so a race (e.g. an interrupt arriving mid-step)
        can never leave the assistant stuck in a stale state.
        """
        try:
            await self.transition(target, reason)
        except InvalidTransition as exc:
            log.warning("%s — recovering via STANDBY", exc)
            await self.reset(reason=f"recover:{reason}")
            if target != S.STANDBY and target in TRANSITIONS[S.STANDBY]:
                await self.transition(target, reason)

    async def reset(self, reason: str = "reset") -> None:
        if self._state == S.STANDBY:
            return
        previous, self._state = self._state, S.STANDBY
        if self._on_change:
            await self._on_change(S.STANDBY, previous, reason)
