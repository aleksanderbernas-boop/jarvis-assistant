"""Shared fixtures: a fully wired JARVIS with a simulated computer, fake web and scripted LLM.

Filesystem tools run for real, but only inside a temporary "home" folder.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.api.schemas import Event
from jarvis.config import PermissionSettings, RunMode, load_settings
from jarvis.core.orchestrator import Orchestrator
from jarvis.core.services import Services
from jarvis.core.session import Session
from jarvis.llm.mock import MockLLM, ScriptedLLM, ScriptedTurn
from jarvis.platform.mock import MockPlatform
from jarvis.web.client import MockWebClient
from jarvis.web.weather import WeatherService


class Harness:
    def __init__(self, services: Services, orchestrator: Orchestrator, session: Session, home: Path) -> None:
        self.services = services
        self.orchestrator = orchestrator
        self.session = session
        self.home = home
        self.events: list[Event] = []
        self._sub = services.bus.subscribe(session.id)

    def drain(self) -> list[Event]:
        while not self._sub.queue.empty():
            self.events.append(self._sub.queue.get_nowait())
        return self.events

    def of_type(self, kind: str) -> list[Any]:
        return [e for e in self.drain() if e.type == kind]

    def responses(self) -> list[str]:
        return [e.text for e in self.of_type("response_end")]

    async def say(self, text: str, source: str = "text") -> None:
        await self.orchestrator.on_user_input(self.session, text, source)
        await self.settle()

    async def settle(self) -> None:
        for _ in range(200):
            turn = self.session.turn
            if turn is None or turn.done():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)


def build_harness(tmp_path: Path, llm: Any = None, mode: RunMode = RunMode.LIVE, **permissions: Any) -> Harness:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    settings = load_settings(env_file=tmp_path / "missing.env", mode=mode, data_dir=tmp_path / "data")
    settings.permissions = PermissionSettings(allowed_paths=[home], **permissions)
    settings.user.latitude, settings.user.longitude, settings.user.location_name = 50.06, 19.94, "Kraków, PL"
    settings.voice.require_wake_word = True
    services = Services(
        settings,
        llm=llm or MockLLM(),
        platform=MockPlatform(),
        web=MockWebClient(),
        weather=WeatherService(simulated=True),
    )
    orchestrator = Orchestrator(services)
    session = services.sessions.get_or_create(None)
    return Harness(services, orchestrator, session, home)


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    h = build_harness(tmp_path)
    await h.services.boot()
    yield h
    await h.services.shutdown()


@pytest.fixture
async def scripted(tmp_path: Path) -> AsyncIterator[Any]:
    """Factory: a harness driven by an exact LLM script."""
    created: list[Harness] = []

    async def make(turns: list[ScriptedTurn], completions: list[str] | None = None, **permissions: Any) -> Harness:
        h = build_harness(tmp_path, llm=ScriptedLLM(turns, completions), **permissions)
        await h.services.boot()
        created.append(h)
        return h

    yield make
    for h in created:
        await h.services.shutdown()
