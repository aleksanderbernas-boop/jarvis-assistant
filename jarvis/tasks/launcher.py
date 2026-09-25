"""Starts background work (research / agent jobs) from either the router or the LLM."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from jarvis.api.schemas import TaskSnapshot
from jarvis.tasks.agent import AgentRunner
from jarvis.web.research import ResearchAgent

if TYPE_CHECKING:
    from jarvis.core.services import Services
    from jarvis.tasks.manager import TaskHandle


class TaskLauncher:
    def __init__(self, services: Services) -> None:
        self._services = services

    def prior_research(self, topic: str) -> str | None:
        """Earlier research on a similar topic, so we continue instead of restarting."""
        matches = self._services.memory.search(topic, kinds=["episodic"], limit=1, min_score=0.5)
        return matches[0].memory.content if matches else None

    async def start_research(self, topic: str, *, session_id: str | None, deadline: datetime | None = None,
                             continue_previous: bool = False) -> TaskSnapshot:
        prior = self.prior_research(topic) if continue_previous else None

        async def runner(handle: TaskHandle) -> str:
            return await ResearchAgent(self._services).run(handle, topic, prior)

        label = f"Research: {topic}" + (" (continued)" if prior else "")
        return await self._services.tasks.create("research", label, runner, session_id=session_id, deadline=deadline)

    async def start_agent(self, goal: str, *, session_id: str | None, deadline: datetime | None = None,
                          context: str = "") -> TaskSnapshot:
        async def runner(handle: TaskHandle) -> str:
            return await AgentRunner(self._services).run(handle, goal, session_id, context)

        return await self._services.tasks.create("agent", goal, runner, session_id=session_id, deadline=deadline)
