"""The brain: turns input into verified action and speech.

    input → (wake word) → (pending confirmation?) → intent router
          → deterministic handler | background task | LLM tool loop
          → streamed response + TTS sentences → state back to idle

Interruptions: a client barge-in mutes speech instantly; "stop" cancels the
current turn; a new request supersedes the old one. Background tasks are only
cancelled when the user explicitly asks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from jarvis import __version__
from jarvis.api.schemas import (
    AssistantState as S,
)
from jarvis.api.schemas import (
    CancelTaskMessage,
    ClientMessage,
    ClientSettingsMessage,
    ConfirmMessage,
    Event,
    HelloMessage,
    InterruptMessage,
    NotificationEvent,
    ResponseMode,
    SessionSnapshotEvent,
    SpeechStopEvent,
    TaskSnapshot,
    TaskStatus,
    TranscriptionEvent,
    TtsStateMessage,
    UserMessageEvent,
    UserTextMessage,
)
from jarvis.core.cancellation import Cancelled, CancelToken
from jarvis.core.confirmation import PendingConfirmation, parse_confirmation
from jarvis.core.context import ContextBuilder
from jarvis.core.metrics import Stopwatch
from jarvis.core.phrases import greeting, ordinal, phrase, spoken_memory, spoken_size_mb
from jarvis.core.response import ResponseStream
from jarvis.core.router import Intent, IntentKind, IntentRouter
from jarvis.core.services import Services
from jarvis.core.session import Session
from jarvis.core.timeparse import humanize_delta, humanize_time
from jarvis.core.toolloop import ToolLoop
from jarvis.llm.base import LLMError, ToolCall
from jarvis.logging_setup import session_id_var
from jarvis.tools.base import ToolContext, ToolResult
from jarvis.tools.builtin.apps import resolve_app
from jarvis.tools.registry import ALWAYS_INCLUDED
from jarvis.voice.wake import WakeWordDetector

log = logging.getLogger(__name__)

Handler = Callable[[Session, Intent, ResponseStream, ToolContext], Awaitable[bool]]


class Orchestrator:
    def __init__(self, services: Services) -> None:
        self.s = services
        self.router = IntentRouter()
        self.context = ContextBuilder(services)
        self.wake = WakeWordDetector(services.settings.voice.wake_words)
        self._recent_announcements: dict[str, float] = {}
        self._background: set[asyncio.Task[None]] = set()
        services.confirmations.on_request = self._on_confirmation_request
        services.confirmations.on_resolve = self._on_confirmation_resolved
        services.tasks.on_finished = self._on_task_finished
        services.reminders.on_fire = self._on_reminder
        services.bus.add_listener(self._on_event)
        self._handlers: dict[IntentKind, Handler] = {
            IntentKind.TIME: self._h_time,
            IntentKind.DATE: self._h_date,
            IntentKind.GREETING: self._h_greeting,
            IntentKind.THANKS: self._h_thanks,
            IntentKind.WORKING_ON: self._h_working_on,
            IntentKind.CANCEL_TASK: self._h_cancel_task,
            IntentKind.OPEN_APP: self._h_open_app,
            IntentKind.CLOSE_APP: self._h_close_app,
            IntentKind.SYSTEM_STATUS: self._h_system_status,
            IntentKind.TOP_PROCESSES: self._h_top_processes,
            IntentKind.REMEMBER: self._h_remember,
            IntentKind.FORGET_LAST: self._h_forget_last,
            IntentKind.FORGET: self._h_forget,
            IntentKind.RECALL_ALL: self._h_recall_all,
            IntentKind.CLEAR_MEMORY: self._h_clear_memory,
            IntentKind.TIMER: self._h_timer,
            IntentKind.REMINDER: self._h_reminder,
            IntentKind.DIAGNOSTICS: self._h_diagnostics,
            IntentKind.LIST_TOOLS: self._h_list_tools,
            IntentKind.WEATHER: self._h_weather,
            IntentKind.VOLUME: self._h_volume,
            IntentKind.RESEARCH: self._h_research,
            IntentKind.AGENT_TASK: self._h_agent_task,
        }

    @property
    def honorific(self) -> str:
        return self.s.settings.user.honorific or "sir"

    # ── Client protocol ─────────────────────────────────────────────────────
    async def handle_client_message(self, session: Session, message: ClientMessage) -> None:
        session_id_var.set(session.id)
        if isinstance(message, UserTextMessage):
            await self.on_user_input(session, message.text, message.source)
        elif isinstance(message, InterruptMessage):
            await self.on_barge_in(session, message.reason)
        elif isinstance(message, ConfirmMessage):
            self.s.confirmations.resolve(message.confirmation_id, message.approved)
        elif isinstance(message, CancelTaskMessage):
            self.s.tasks.cancel(message.task_id, "cancelled from the HUD")
        elif isinstance(message, TtsStateMessage):
            await self.on_tts_state(session, message.playing, message.first_audio_ms)
        elif isinstance(message, ClientSettingsMessage):
            await self.on_client_settings(session, message)
        elif isinstance(message, HelloMessage):
            await self.on_client_settings(session, ClientSettingsMessage(voice_output=message.voice_output,
                                                                         mic_active=message.mic_active))

    def snapshot(self, session: Session) -> SessionSnapshotEvent:
        pending = self.s.confirmations.pending_for(session.id)
        return SessionSnapshotEvent(
            session_id=session.id,
            state=session.state.state,
            history=session.transcript(),
            tasks=self.s.tasks.active(session.id) + [t for t in self.s.tasks.recent(8) if t.status.is_terminal],
            activity=self.s.bus.activity_for(session.id)[-40:],
            pending_confirmation=pending.event if pending else None,
            config=self.s.settings.public_summary() | {
                "stt_server_side": self.s.stt.server_side,
                "tts_server_side": self.s.tts.server_side,
                "boot": [s.model_dump() for s in self.s.boot_stages],
                "boot_complete": self.s.boot_complete,
                "memory_count": self.s.memory.count(),
            },
            version=__version__,
        )

    async def on_client_settings(self, session: Session, message: ClientSettingsMessage) -> None:
        if message.voice_output is not None:
            session.voice_output = message.voice_output
        if message.mic_error is not None:
            session.mic_error = message.mic_error or None
        if message.mic_active is not None:
            session.mic_active = message.mic_active
            if not session.busy and session.state.state in {S.STANDBY, S.LISTENING}:
                await self._to_idle(session)

    # ── Input ───────────────────────────────────────────────────────────────
    async def on_user_input(self, session: Session, text: str, source: str = "text",
                            stt_ms: float | None = None) -> None:
        text = text.strip()
        if not text:
            return
        pending = self.s.confirmations.pending_for(session.id)
        if source == "voice":
            if session.is_echo(text):
                log.info("ignored transcript that matches JARVIS's own recent speech")
                await self.s.bus.publish(TranscriptionEvent(session_id=session.id, text=text, ignored=True,
                                                            stt_ms=stt_ms))
                return
            wake = self.wake.check(text)
            needs_wake = self.s.settings.voice.require_wake_word and not session.in_conversation() and not pending
            if needs_wake and not wake.detected:
                await self.s.bus.publish(TranscriptionEvent(session_id=session.id, text=text, ignored=True,
                                                            stt_ms=stt_ms))
                return
            await self.s.bus.publish(TranscriptionEvent(session_id=session.id, text=text,
                                                        wake_detected=wake.detected, stt_ms=stt_ms))
            if wake.detected:
                text = wake.remainder
                if not text:
                    await self._acknowledge_wake(session)
                    return

        await self.s.bus.publish(UserMessageEvent(session_id=session.id, message_id=uuid.uuid4().hex[:10],
                                                  text=text, source=source))  # type: ignore[arg-type]
        if pending:
            decision = parse_confirmation(text)
            if decision is not None:
                self.s.confirmations.resolve(pending.event.confirmation_id, decision)
                return
            # Anything that isn't a clear yes is NOT approval.
            self.s.confirmations.resolve(pending.event.confirmation_id, False)

        intent = self.router.route(text)
        log.info("intent %s (%s)", intent.kind.value, intent.mode.value)
        if intent.kind is IntentKind.STOP:
            await self.stop(session)
            return
        if session.busy:
            await self._cancel_turn(session, "superseded by a new request")
        session.turn_cancel = CancelToken()
        session.turn = asyncio.create_task(self._run_turn(session, text, source, intent),
                                           name=f"turn-{session.id}")

    async def _acknowledge_wake(self, session: Session) -> None:
        await self._silence(session, "wake word")
        if session.busy:
            await self._cancel_turn(session, "wake word")
        session.open_conversation(self.s.settings.voice.conversation_window_s)
        response = ResponseStream(self.s, session, ResponseMode.QUICK)
        await response.say(phrase("wake_ack", self.honorific))
        await response.finish()
        if not response.spoke:
            await session.state.set(S.LISTENING, "wake word")

    # ── Interruption ────────────────────────────────────────────────────────
    async def _silence(self, session: Session, reason: str) -> None:
        session.muted_response_id = session.current_response_id
        session.tts_playing = False
        session.last_speech_stop = time.monotonic()
        await self.s.bus.publish(SpeechStopEvent(session_id=session.id, reason=reason))

    async def on_barge_in(self, session: Session, reason: str) -> None:
        """The user started talking over JARVIS: stop audio now, decide the rest once we hear them."""
        await self._silence(session, reason)
        session.open_conversation(self.s.settings.voice.conversation_window_s)
        if not session.busy and session.state.state in {S.SPEAKING, S.STANDBY, S.LISTENING, S.BACKGROUND_TASK}:
            await session.state.set(S.INTERRUPTED, reason)
            await self._to_idle(session)

    async def _cancel_turn(self, session: Session, reason: str) -> None:
        turn, token = session.turn, session.turn_cancel
        if token:
            token.cancel(reason)
        if turn and not turn.done():
            turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(turn), timeout=3)
        session.turn = None

    async def stop(self, session: Session) -> None:
        was_active = session.busy or session.tts_playing or time.monotonic() - session.last_speech_stop < 4
        await self._silence(session, "stop")
        declined = self.s.confirmations.decline_all(session.id)
        if session.busy:
            await self._cancel_turn(session, "stopped by user")
        await session.state.set(S.INTERRUPTED, "stop")
        response = ResponseStream(self.s, session, ResponseMode.QUICK)
        active = self.s.tasks.active(session.id)
        if was_active or declined:
            await response.say(phrase("stopped"))
        elif active:
            await response.say(f"Nothing's running in the foreground. '{active[0].description}' is still going "
                               "in the background — say 'cancel the task' to stop it.")
        else:
            await response.say(phrase("nothing_to_stop"))
        await response.finish()
        self.s.sessions.add_message(session, {"role": "assistant", "content": response.text.strip()})
        if not response.spoke:
            await self._to_idle(session)

    async def on_tts_state(self, session: Session, playing: bool, first_audio_ms: float | None) -> None:
        session.tts_playing = playing
        if first_audio_ms is not None:
            self.s.metrics.record("first_audio", first_audio_ms)
        if playing:
            if session.state.state in {S.STANDBY, S.LISTENING, S.BACKGROUND_TASK, S.INTERRUPTED}:
                await session.state.set(S.SPEAKING, "audio playing")
            return
        session.open_conversation(self.s.settings.voice.conversation_window_s)
        if not session.busy and session.state.state is S.SPEAKING:
            await self._to_idle(session)
            await self._flush_announcements(session)

    # ── Turn execution ──────────────────────────────────────────────────────
    async def _run_turn(self, session: Session, text: str, source: str, intent: Intent) -> None:
        session_id_var.set(session.id)
        clock = Stopwatch()
        token = session.turn_cancel or CancelToken()
        response = ResponseStream(self.s, session, intent.mode, turn_clock=clock)
        ctx = ToolContext(services=self.s, session_id=session.id, cancel=token)
        self.s.sessions.add_message(session, {"role": "user", "content": text})
        cancelled = False
        try:
            await session.state.set(S.THINKING, f"{intent.kind.value} request")
            handled = False
            handler = self._handlers.get(intent.kind)
            if handler:
                handled = await handler(session, intent, response, ctx)
                if handled and response.text:
                    self.s.sessions.add_message(session, {"role": "assistant", "content": response.text.strip()})
            if not handled:
                await self._llm_turn(session, text, intent, response, ctx, voice=source == "voice")
            await response.finish()
            self.s.metrics.incr("turns")
        except (asyncio.CancelledError, Cancelled):
            cancelled = True
            await response.finish(interrupted=True)
            if response.text.strip():
                self.s.sessions.add_message(session, {"role": "assistant",
                                                      "content": response.text.strip() + " [interrupted]"})
        except LLMError as exc:
            log.warning("LLM failure: %s", exc)
            self.s.llm_status["last_error"] = str(exc)
            await self._fail(session, response, exc.user_message())
        except Exception:
            log.exception("turn failed")
            await self._fail(session, response, "I've run into an internal problem handling that. "
                                                "The details are in the log.")
        finally:
            session.open_conversation(self.s.settings.voice.conversation_window_s)
            if session.turn is asyncio.current_task():
                session.turn = None
            if not cancelled:
                await self._settle(session)
            compaction = asyncio.create_task(self.context.compact(session))
            self._background.add(compaction)  # keep a reference until it finishes
            compaction.add_done_callback(self._background.discard)

    async def _fail(self, session: Session, response: ResponseStream, message: str) -> None:
        self.s.metrics.incr("turn_errors")
        await session.state.set(S.ERROR, "error")
        if response.finished:
            response = ResponseStream(self.s, session, ResponseMode.ALERT)
        await response.say(message)
        await response.finish()
        self.s.sessions.add_message(session, {"role": "assistant", "content": response.text.strip()})

    async def _settle(self, session: Session) -> None:
        if session.tts_playing and session.state.state is S.SPEAKING:
            return  # the client reports when audio ends
        await self._to_idle(session)
        await self._flush_announcements(session)

    def _idle_state(self, session: Session) -> S:
        if self.s.tasks.active(session.id):
            return S.BACKGROUND_TASK
        return S.LISTENING if session.mic_active else S.STANDBY

    async def _to_idle(self, session: Session) -> None:
        await session.state.set(self._idle_state(session), "idle")

    async def _llm_turn(self, session: Session, text: str, intent: Intent, response: ResponseStream,
                        ctx: ToolContext, voice: bool) -> None:
        services = self.s
        tools, categories = services.registry.select(services, text, session.sticky_categories)
        session.sticky_categories = categories - ALWAYS_INCLUDED
        messages = await self.context.build(session, text, intent.mode, voice)

        async def on_phase(phase: str) -> None:
            await session.state.set(S.EXECUTING if phase == "executing" else S.THINKING, phase)

        outcome = await ToolLoop(services).run(
            model=services.models.model_for(intent.tier),
            messages=messages,
            tools=tools,
            ctx=ctx,
            on_text=response.delta,
            on_phase=on_phase,
            max_steps=services.settings.tasks.max_agent_steps,
            max_tool_calls=services.settings.tasks.max_tool_calls_per_turn,
        )
        services.llm_status.update(ok=True, last_error=None)
        for message in outcome.messages:
            services.sessions.add_message(session, message)
        if not outcome.text.strip() and outcome.results:
            # The model went quiet after acting: report the last tool outcome verbatim — never invent one.
            last = outcome.results[-1][1]
            await response.say(("Done: " if last.ok else "That didn't work: ") + last.summary)

    # ── Tool helper for deterministic handlers ──────────────────────────────
    async def _tool(self, ctx: ToolContext, tool_name: str, /, **args: Any) -> ToolResult:
        call = ToolCall(id=f"det_{uuid.uuid4().hex[:8]}", name=tool_name, arguments=json.dumps(args, default=str))
        return await self.s.executor.execute(call, ctx)

    # ── Deterministic handlers (return False to fall back to the LLM) ───────
    async def _h_time(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        await r.say(f"It's {datetime.now():%H:%M}.")
        return True

    async def _h_date(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        now = datetime.now()
        await r.say(f"It's {now:%A}, the {ordinal(now.day)} of {now:%B %Y}.")
        return True

    async def _h_greeting(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        if intent.text.lower() in {"jarvis", "you there", "are you there"}:
            await r.say(phrase("wake_ack", self.honorific))
        else:
            await r.say(greeting(self.honorific))
        return True

    async def _h_thanks(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        await r.say(phrase("thanks", self.honorific))
        return True

    async def _h_working_on(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        active = self.s.tasks.active(session.id)
        if not active:
            recent = [t for t in self.s.tasks.recent(3) if t.status.is_terminal]
            last = f" The last one — {recent[0].description} — {recent[0].status.value}." if recent else ""
            await r.say(f"Nothing in the background at the moment.{last}")
            return True
        parts = []
        for task in active:
            elapsed = ""
            if task.started_at:
                elapsed = f", running for {humanize_delta(datetime.now().astimezone() - task.started_at)}"
            step = f", currently {task.current_step.lower()}" if task.current_step else ""
            parts.append(f"{task.description} — {task.progress:.0%} done{step}{elapsed}")
        noun = "one task" if len(parts) == 1 else f"{len(parts)} tasks"
        await r.say(f"I'm working on {noun}: " + "; ".join(parts) + ".")
        return True

    async def _h_cancel_task(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        active = self.s.tasks.active(session.id)
        if not active:
            await r.say("There's nothing running in the background.")
            return True
        query = str(intent.params.get("query", ""))
        matches = active if len(active) == 1 or " all " in f" {query} " else self.s.tasks.find_active(query)
        if len(matches) != 1 and " all " not in f" {query} ":
            options = " or ".join(f"'{t.description}'" for t in (matches or active)[:3])
            await r.say(f"Which one should I stop: {options}? Or say 'cancel all tasks'.")
            return True
        for task in matches:
            self.s.tasks.cancel(task.id, "cancelled by user")
        await r.say("Cancelled " + ("that task." if len(matches) == 1 else f"all {len(matches)} tasks."))
        return True

    async def _h_open_app(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        platform = self.s.platform
        try:
            apps = await platform.call(platform.installed_apps)
            default_browser = await platform.call(platform.default_browser)
        except Exception:
            return False
        matches = resolve_app(intent.params["name"], apps, default_browser)
        if not matches or (len(matches) > 1 and matches[0][0] < 0.9 and matches[1][0] >= matches[0][0] - 0.03):
            return False  # unknown or ambiguous — the LLM can ask or try a website
        app_name = matches[0][1].name
        await r.say(phrase("opening", self.honorific, app=app_name))
        await session.state.set(S.EXECUTING, "launching")
        result = await self._tool(ctx, "open_app", name=app_name)
        if not result.ok:
            await r.say(f"That didn't work — {result.error}")
        elif (result.data or {}).get("already_running"):
            await r.say("It was already running, so I've brought it to the front.")
        elif result.verified is False:
            await r.say("It launched, but I can't confirm the window yet.")
        return True

    async def _h_close_app(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        await session.state.set(S.EXECUTING, "closing")
        result = await self._tool(ctx, "close_app", name=intent.params["name"])
        if result.error_kind == "not_running":
            await r.say(f"{intent.params['name'].title()} isn't running.")
        elif not result.ok:
            await r.say(f"I couldn't close it: {result.error}")
        else:
            await r.say(result.summary[0].upper() + result.summary[1:] + ".")
        return True

    async def _h_system_status(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        await session.state.set(S.EXECUTING, "checking")
        result = await self._tool(ctx, "system_status")
        if not result.ok:
            await r.say(f"I couldn't read the system status: {result.error}")
            return True
        m = result.data
        busy = m["cpu_percent"] > 85 or m["ram_percent"] > 90 or m["disk_percent"] > 95
        lead = "Things are under some strain" if busy else "All systems normal"
        text = (f"{lead}: CPU at {m['cpu_percent']:.0f} percent, memory at {m['ram_percent']:.0f} percent, "
                f"and the system drive is {m['disk_percent']:.0f} percent full")
        if m.get("battery_percent") is not None:
            text += f". Battery at {m['battery_percent']:.0f} percent{', charging' if m.get('battery_plugged') else ''}"
        if not m.get("online"):
            text += ". I don't see an active network connection"
        await r.say(text + ".")
        return True

    async def _h_top_processes(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        sort_by = intent.params.get("sort_by", "memory")
        await r.say(phrase("checking"))
        await session.state.set(S.EXECUTING, "checking")
        result = await self._tool(ctx, "top_processes", sort_by=sort_by, limit=5)
        if not result.ok:
            await r.say(f"I couldn't read the process list: {result.error}")
            return True
        rows = result.data["processes"]
        top = rows[0]
        count = f" across {top['processes']} processes" if top["processes"] > 1 else ""
        if sort_by == "memory":
            text = f"{top['name']} is using the most memory — about {spoken_size_mb(top['memory_mb'])}{count}."
        else:
            text = f"{top['name']} is using the most CPU — about {top['cpu_percent']:.0f} percent{count}."
        if len(rows) > 1:
            text += f" Next is {rows[1]['name']}."
        if result.simulated:
            text += " (Simulated data — mock mode.)"
        await r.say(text)
        return True

    async def _h_remember(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        fact = str(intent.params["content"]).strip()
        # Keep the user's own words, attributed, so the memory is unambiguous in any later prompt.
        result = await self._tool(ctx, "memory_remember", content=f"User: {fact}", kind=intent.params["kind"])
        if not result.ok:
            await r.say(f"I didn't store that: {result.error}")
        else:
            await r.say(phrase("remembered" if result.data.get("created") else "memory_updated", self.honorific))
        return True

    async def _h_forget_last(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        if not session.last_memory_id or not self.s.memory.get(session.last_memory_id):
            return False  # "forget that" may refer to something else — let the LLM interpret
        result = await self._tool(ctx, "memory_forget", memory_id=session.last_memory_id)
        await r.say("Forgotten." if result.ok else f"I couldn't: {result.error}")
        return True

    async def _h_forget(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        result = await self._tool(ctx, "memory_forget", query=intent.params["query"])
        if result.ok:
            await r.say("Done — I've forgotten that.")
        elif result.error_kind == "ambiguous":
            options = "; ".join(c["content"] for c in result.data["candidates"][:3])
            await r.say(f"I have a few memories like that: {options}. Which one?")
        elif result.error_kind == "not_found":
            await r.say("I don't have anything stored about that.")
        else:
            await r.say(f"I couldn't: {result.error}")
        return True

    async def _h_recall_all(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        memories = self.s.memory.entries(limit=200)
        durable = [m for m in memories if m.kind != "episodic"]
        episodes = len(memories) - len(durable)
        if not memories:
            await r.say("I don't have anything stored about you yet. Tell me to remember something and I will.")
            return True
        if durable:
            await r.say("Here's what I remember: " + "; ".join(spoken_memory(m.content) for m in durable[:10]) + ".")
        if episodes:
            await r.say(f"I also have notes on {episodes} past task{'s' if episodes != 1 else ''} and research.")
        return True

    async def _h_clear_memory(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        result = await self._tool(ctx, "memory_clear")
        await r.say("Memory cleared." if result.ok else "Understood — I've left your memories as they are."
                    if result.error_kind == "declined" else f"I couldn't: {result.error}")
        return True

    async def _h_timer(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        seconds = float(intent.params["seconds"])
        due = datetime.now().astimezone() + timedelta(seconds=seconds)
        label = humanize_delta(timedelta(seconds=seconds))
        result = await self._tool(ctx, "reminder_create", text=f"Your {label} timer is done.", when=due.isoformat())
        await r.say(f"Timer set for {label}." if result.ok else f"I couldn't set the timer: {result.error}")
        return True

    async def _h_reminder(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        due: datetime = intent.params["when"]
        text = str(intent.params["text"])
        result = await self._tool(ctx, "reminder_create", text=text, when=due.isoformat())
        if result.ok:
            await r.say(f"I'll remind you {humanize_time(due)} — {text}.")
        else:
            await r.say(f"I couldn't set that reminder: {result.error}")
        return True

    async def _h_diagnostics(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        await session.state.set(S.EXECUTING, "diagnostics")
        result = await self._tool(ctx, "diagnostics")
        report = result.data or {}
        components = report.get("components", {})
        problems = [f"{name.replace('_', ' ')}: {c['detail']}" for name, c in components.items() if not c["ok"]]
        tools = components.get("tools", {}).get("detail", "")
        memory = components.get("memory", {}).get("detail", "")
        tasks = components.get("tasks", {}).get("detail", "")
        if not problems:
            await r.say(f"Diagnostics complete. All systems nominal — language model connected, voice online, "
                        f"tools {tools}, {memory}, {tasks} tasks.")
        else:
            await r.say(f"Diagnostics complete. {len(problems)} issue{'s' if len(problems) != 1 else ''}: "
                        + "; ".join(problems) + f". Otherwise: tools {tools}, {memory}.")
        errors = report.get("recent_errors") or []
        if errors:
            await r.say(f"Most recent logged problem: {errors[-1]['message'][:160]}.")
        return True

    async def _h_list_tools(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        rows = [row for row in self.s.registry.describe(self.s) if row["enabled"]]
        by_category: dict[str, int] = {}
        for row in rows:
            by_category[row["category"]] = by_category.get(row["category"], 0) + 1
        labels = {"apps": "launch and close apps", "windows": "arrange windows", "files": "find, read and organise files",
                  "web": "search and read the web", "system": "monitor the system", "memory": "remember things",
                  "tasks": "run background research and jobs", "reminders": "set reminders and timers",
                  "audio": "control volume", "display": "adjust the display", "screen": "take and analyse screenshots",
                  "clipboard": "use the clipboard", "input": "type and click", "diagnostics": "diagnose myself"}
        abilities = [labels[c] for c in labels if c in by_category]
        await r.say(f"I have {len(rows)} tools online. I can " + ", ".join(abilities[:-1]) + f", and {abilities[-1]}.")
        return True

    async def _h_weather(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        await session.state.set(S.EXECUTING, "weather")
        result = await self._tool(ctx, "weather", location=intent.params.get("location"))
        if not result.ok:
            await r.say(f"I couldn't get the weather: {result.error}")
            return True
        data = result.data
        current = data["current"]
        text = (f"In {data['location']} it's {round(current['temperature_c'])} degrees and "
                f"{current['condition'].lower()}.")
        if len(data["daily"]) > 1:
            tomorrow = data["daily"][1]
            rain = tomorrow.get("precipitation_probability")
            text += (f" Tomorrow: {tomorrow['condition'].lower()}, up to {round(tomorrow['max_c'])}"
                     + (f", with a {rain} percent chance of rain." if rain is not None else "."))
        if data.get("simulated"):
            text += " That's simulated data — mock mode."
        await r.say(text)
        return True

    async def _h_volume(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        result = await self._tool(ctx, "volume_control", **intent.params)
        await r.say(result.summary + "." if result.ok else f"I couldn't change the volume: {result.error}")
        return True

    async def _h_research(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        if not self.s.settings.permissions.allow_web:
            await r.say("Web access is disabled, so I can't research that. Enable JARVIS_ALLOW_WEB to allow it.")
            return True
        topic = str(intent.params["topic"])
        task = await self.s.launcher.start_research(topic, session_id=session.id, deadline=intent.params.get("deadline"),
                                                    continue_previous=bool(intent.params.get("continue")))
        key = "research_continue" if task.description.endswith("(continued)") else "research_start"
        deadline = intent.params.get("deadline")
        await r.say(phrase(key, self.honorific) + (f" I'm aiming to finish before {deadline:%H:%M}." if deadline else ""))
        return True

    async def _h_agent_task(self, session: Session, intent: Intent, r: ResponseStream, ctx: ToolContext) -> bool:
        await self.s.launcher.start_agent(str(intent.params["goal"]), session_id=session.id,
                                          deadline=intent.params.get("deadline"))
        await r.say(phrase("agent_start", self.honorific))
        return True

    # ── Confirmations ───────────────────────────────────────────────────────
    async def _on_confirmation_request(self, pending: PendingConfirmation) -> None:
        session = self.s.sessions.get(pending.event.session_id)
        self.s.tasks.mark_waiting(pending.task_id, True)
        if not session:
            return
        session.open_conversation(self.s.settings.permissions.confirmation_timeout_s)
        if not pending.task_id:
            await session.state.set(S.WAITING_FOR_CONFIRMATION, "confirmation required")
        prefix = "A background task needs your approval. " if pending.task_id else ""
        response = ResponseStream(self.s, session, ResponseMode.ALERT)
        await response.say(prefix + pending.event.summary)
        await response.finish()

    async def _on_confirmation_resolved(self, pending: PendingConfirmation, approved: bool) -> None:
        self.s.tasks.mark_waiting(pending.task_id, False)
        session = self.s.sessions.get(pending.event.session_id)
        if session and not pending.task_id and session.busy:
            await session.state.set(S.EXECUTING if approved else S.THINKING, "confirmation resolved")

    # ── Proactive notifications ─────────────────────────────────────────────
    async def _on_event(self, event: Event) -> None:
        if isinstance(event, NotificationEvent) and event.speak:
            await self.announce(event.session_id, event.message)

    async def _on_task_finished(self, task: TaskSnapshot) -> None:
        if task.status is TaskStatus.COMPLETED:
            await self.s.bus.publish(NotificationEvent(session_id=task.session_id, level="success",
                                                       title="Task complete", message=task.description))
            label = task.description.removeprefix("Research: ")
            if task.kind == "research":
                spoken = f"I've finished the research on {label}. The report is on screen."
            else:
                spoken = f"Finished: {label}. " + (task.result or "").split("\n")[0][:300]
            await self.announce(task.session_id, spoken, detail=task.result)
        elif task.status is TaskStatus.FAILED:
            await self.s.bus.publish(NotificationEvent(session_id=task.session_id, level="error",
                                                       title="Task failed", message=f"{task.description}: {task.error}"))
            await self.announce(task.session_id, f"The task '{task.description}' failed: {task.error}")
        elif task.status is TaskStatus.CANCELLED:
            await self.s.bus.publish(NotificationEvent(session_id=task.session_id, level="info",
                                                       title="Task cancelled", message=task.description))
        for session in self.s.sessions.all():
            if not session.busy and session.state.state in {S.BACKGROUND_TASK, S.STANDBY, S.LISTENING}:
                await self._to_idle(session)

    async def _on_reminder(self, reminder: Any, missed: bool) -> None:
        when = reminder.due_at.astimezone().strftime("%H:%M")
        message = (f"You missed a reminder from {when}: {reminder.text}" if missed else f"Reminder: {reminder.text}")
        await self.s.bus.publish(NotificationEvent(session_id=reminder.session_id, level="info", title="Reminder",
                                                   message=message))
        await self.announce(reminder.session_id, message)

    async def announce(self, session_id: str | None, text: str, detail: str | None = None) -> None:
        """Speak a proactive update — queued if the user is mid-conversation, de-duplicated, never spammy."""
        now = time.monotonic()
        if now - self._recent_announcements.get(text, 0) < 60:
            return
        self._recent_announcements[text] = now
        session = self.s.sessions.get(session_id)
        targets = [session] if session else self.s.sessions.all()
        for target in targets:
            item = text if detail is None else f"{text}\n\n{detail}"
            if target.busy or target.tts_playing or self.s.confirmations.pending_for(target.id):
                target.pending_announcements.append(item)
            else:
                await self._deliver(target, item)

    async def _deliver(self, session: Session, item: str) -> None:
        spoken, _, detail = item.partition("\n\n")
        response = ResponseStream(self.s, session, ResponseMode.ALERT)
        await response.say(spoken)
        if detail:
            await response.delta("\n\n" + detail, speak=False)  # full result on screen, not read aloud
        await response.finish()
        self.s.sessions.add_message(session, {"role": "assistant", "content": response.text.strip()})
        session.open_conversation(self.s.settings.voice.conversation_window_s)
        if not response.spoke:
            await self._to_idle(session)

    async def _flush_announcements(self, session: Session) -> None:
        while session.pending_announcements and not session.busy and not session.tts_playing:
            await self._deliver(session, session.pending_announcements.pop(0))
