"""Service container, boot sequence and diagnostics.

Everything JARVIS is made of is constructed here once and shared. Tests and
mock mode inject replacements (LLM, platform, web) through the constructor.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from jarvis import __version__
from jarvis.api.schemas import (
    ActivityEvent,
    AssistantState,
    AssistantStateEvent,
    BootEvent,
    BootStage,
    SystemMetricsEvent,
)
from jarvis.config import Settings
from jarvis.core.confirmation import ConfirmationManager
from jarvis.core.events import EventBus
from jarvis.core.metrics import Metrics
from jarvis.core.session import Session, SessionManager
from jarvis.llm.base import LLMError, LLMProvider
from jarvis.llm.factory import build_llm
from jarvis.llm.models import ModelStrategy
from jarvis.logging_setup import recent_errors
from jarvis.memory.store import MemoryStore
from jarvis.platform import get_platform
from jarvis.platform.base import PlatformAdapter
from jarvis.storage.db import Database
from jarvis.tasks.launcher import TaskLauncher
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.reminders import ReminderService
from jarvis.telemetry.system import Telemetry
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.stats import ToolStats
from jarvis.voice.speech import SpeechToText, TextToSpeech
from jarvis.web.client import MockWebClient, WebClient
from jarvis.web.weather import WeatherService

log = logging.getLogger(__name__)

_STATE_ACTIVITY = {
    AssistantState.LISTENING: "Listening…",
    AssistantState.THINKING: "Understanding request…",
    AssistantState.PLANNING: "Planning…",
    AssistantState.EXECUTING: "Executing…",
    AssistantState.WAITING_FOR_CONFIRMATION: "Awaiting your confirmation",
    AssistantState.INTERRUPTED: "Interrupted",
    AssistantState.ERROR: "Error",
}

BOOT_STAGES = [
    ("core", "Initializing core"),
    ("memory", "Loading memory"),
    ("services", "Connecting services"),
    ("tools", "Loading tools"),
    ("system", "Checking system"),
    ("voice", "Voice interface"),
]


class Services:
    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMProvider | None = None,
        platform: PlatformAdapter | None = None,
        web: WebClient | None = None,
        weather: WeatherService | None = None,
        db: Database | None = None,
    ) -> None:
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.bus = EventBus()
        self.metrics = Metrics()
        self.db = db or Database(settings.data_dir / "jarvis.db")
        self.platform = platform or get_platform(settings.mock)
        self.llm = llm or build_llm(settings)
        self.models = ModelStrategy(settings.llm)
        self.memory = MemoryStore(self.db)
        self.tool_stats = ToolStats(self.db)
        self.registry = ToolRegistry()
        self.registry.load_builtin()
        self.executor = ToolExecutor(self, settings.data_dir / "audit.log")
        self.confirmations = ConfirmationManager(self.bus, settings.permissions.confirmation_timeout_s)
        self.tasks = TaskManager(self.bus, self.db, self.metrics, settings.tasks.max_concurrent)
        self.reminders = ReminderService(self.db)
        self.web = web or (MockWebClient() if settings.mock else WebClient(
            settings.search, allow_private=settings.permissions.allow_private_network_fetch))
        self.weather = weather or WeatherService(simulated=settings.mock)
        self.telemetry = Telemetry()
        self.stt = SpeechToText(settings)
        self.tts = TextToSpeech(settings)
        self.sessions = SessionManager(self.db, self._on_state)
        self.launcher = TaskLauncher(self)
        self.boot_stages = [BootStage(key=k, label=label) for k, label in BOOT_STAGES]
        self.boot_complete = False
        self.llm_status: dict[str, Any] = {"ok": None, "detail": "not checked yet", "last_error": None}
        self._background: list[asyncio.Task[None]] = []

    # ── State publication ───────────────────────────────────────────────────
    async def _on_state(self, session: Session, state: AssistantState, previous: AssistantState, reason: str) -> None:
        await self.bus.publish(AssistantStateEvent(session_id=session.id, state=state, previous=previous,
                                                   reason=reason))
        label = _STATE_ACTIVITY.get(state)
        if label:
            await self.bus.publish(ActivityEvent(session_id=session.id, text=label, kind="state"))

    # ── Boot ────────────────────────────────────────────────────────────────
    async def _stage(self, key: str, status: str, detail: str) -> None:
        for stage in self.boot_stages:
            if stage.key == key:
                stage.status, stage.detail = status, detail  # type: ignore[assignment]
        await self.bus.publish(BootEvent(stages=[s.model_copy() for s in self.boot_stages]))

    async def boot(self) -> None:
        """Run real initialisation checks; each stage reports its true outcome."""
        try:
            self.db.query("SELECT 1")
            await self._stage("core", "ok", f"v{__version__}, {self.settings.mode.value} mode, {self.platform.name}")
        except Exception as exc:
            await self._stage("core", "failed", f"database error: {exc}")

        episodic = len(self.memory.entries("episodic", limit=10_000))
        total = self.memory.count()
        await self._stage("memory", "ok", f"{total} memories ({episodic} episodic)")

        await self.check_llm()
        status = "ok" if self.llm_status["ok"] else "failed"
        if self.llm_status["ok"] and self.models.warnings:
            status = "warning"
        await self._stage("services", status, self.llm_status["detail"])

        rows = self.registry.describe(self)
        enabled = [r for r in rows if r["enabled"]]
        disabled = [f"{r['name']} ({r['reason']})" for r in rows if not r["enabled"]]
        await self._stage("tools", "ok" if enabled else "failed",
                          f"{len(enabled)} of {len(rows)} tools online" + (f"; off: {len(disabled)}" if disabled else ""))

        try:
            sample = await asyncio.to_thread(self.telemetry.sample)
            await self._stage("system", "ok", f"CPU {sample.cpu_percent:.0f}%, RAM {sample.ram_percent:.0f}%, "
                                              f"{'online' if sample.online else 'offline'}")
        except Exception as exc:
            await self._stage("system", "warning", f"telemetry unavailable: {exc}")

        stt_ok, stt_detail = self.stt.status()
        tts_ok, tts_detail = self.tts.status()
        await self._stage("voice", "ok" if stt_ok and tts_ok else "warning",
                          f"STT: {stt_detail}; TTS: {tts_detail}")

        self.tasks.start()
        self.reminders.start()
        self._background.append(asyncio.create_task(self._metrics_loop(), name="metrics-broadcast"))
        self.boot_complete = True
        await self.bus.publish(BootEvent(stages=[s.model_copy() for s in self.boot_stages], complete=True))
        log.info("J.A.R.V.I.S. online (%s mode)", self.settings.mode.value)

    async def check_llm(self) -> None:
        provider = self.settings.llm.provider
        try:
            available = await asyncio.wait_for(self.llm.list_models(), timeout=8)
            warnings = [] if self.settings.mock else self.models.reconcile(available)
            detail = f"{provider}: connected" + (" (simulated)" if self.settings.mock else "")
            if warnings:
                detail += "; " + "; ".join(warnings)
            self.llm_status = {"ok": True, "detail": detail, "last_error": None}
        except LLMError as exc:
            self.llm_status = {"ok": False, "detail": f"{provider}: {exc}", "last_error": str(exc)}
        except TimeoutError:
            self.llm_status = {"ok": False, "detail": f"{provider}: connection timed out", "last_error": "timeout"}

    async def _metrics_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            if self.bus.subscriber_count == 0:
                continue
            try:
                metrics = await asyncio.to_thread(self.telemetry.sample)
                await self.bus.publish(SystemMetricsEvent(metrics=metrics))
            except Exception:
                log.exception("telemetry sample failed")

    async def shutdown(self) -> None:
        for task in self._background:
            task.cancel()
        await self.tasks.shutdown()
        await self.reminders.shutdown()
        for closer in (self.llm.close, self.web.close, self.weather.close, self.stt.close, self.tts.close):
            with contextlib.suppress(Exception):
                await closer()
        self.db.close()

    # ── Diagnostics ─────────────────────────────────────────────────────────
    async def diagnostics(self) -> dict[str, Any]:
        rows = self.registry.describe(self)
        stt_ok, stt_detail = self.stt.status()
        tts_ok, tts_detail = self.tts.status()
        sessions = self.sessions.all()
        mic_errors = [s.mic_error for s in sessions if s.mic_error]
        active_tasks = self.tasks.active()
        return {
            "version": __version__,
            "mode": self.settings.mode.value,
            "platform": self.platform.name,
            "components": {
                "backend": {"ok": self.boot_complete, "detail": f"uptime {int(time.time() - self.metrics.started_at)}s"},
                "llm": {"ok": bool(self.llm_status["ok"]), "detail": self.llm_status["detail"],
                        "models": self.models.snapshot(), "last_error": self.llm_status["last_error"]},
                "speech_to_text": {"ok": stt_ok, "detail": stt_detail},
                "text_to_speech": {"ok": tts_ok, "detail": tts_detail},
                "microphone": {"ok": not mic_errors,
                               "detail": mic_errors[-1] if mic_errors else
                               ("active" if any(s.mic_active for s in sessions) else "not active")},
                "tools": {"ok": True, "detail": f"{sum(r['enabled'] for r in rows)} of {len(rows)} enabled"},
                "memory": {"ok": True, "detail": f"{self.memory.count()} memories stored"},
                "websocket": {"ok": self.bus.subscriber_count > 0,
                              "detail": f"{self.bus.subscriber_count} HUD connection(s)"},
                "tasks": {"ok": True, "detail": f"{len(active_tasks)} active"},
            },
            "active_tasks": [{"id": t.id, "description": t.description, "progress": round(t.progress, 2),
                              "step": t.current_step} for t in active_tasks],
            "recent_errors": list(recent_errors)[-5:],
            "metrics": self.metrics.snapshot(),
            "tool_methods": self.tool_stats.snapshot(),
        }
