"""Streams one assistant response to the HUD and, sentence by sentence, to TTS."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from jarvis.api.schemas import (
    AssistantState,
    ResponseDeltaEvent,
    ResponseEndEvent,
    ResponseMode,
    ResponseStartEvent,
    SpeakEvent,
)
from jarvis.core.metrics import Stopwatch
from jarvis.llm.speech import SentenceChunker

if TYPE_CHECKING:
    from jarvis.core.services import Services
    from jarvis.core.session import Session


class ResponseStream:
    def __init__(self, services: Services, session: Session, mode: ResponseMode, *,
                 turn_clock: Stopwatch | None = None) -> None:
        self._services = services
        self._session = session
        self.mode = mode
        self.id = uuid.uuid4().hex[:12]
        self.text = ""
        self._chunker = SentenceChunker()
        self._seq = 0
        self._started = False
        self._clock = turn_clock or Stopwatch()
        self.spoke = False
        self.finished = False

    @property
    def speaking_enabled(self) -> bool:
        session = self._session
        return (session.voice_output and session.muted_response_id != self.id
                and self._services.bus.has_subscribers(session.id))

    async def _start(self) -> None:
        if not self._started:
            self._started = True
            self._session.current_response_id = self.id
            self._services.metrics.record("first_token", self._clock.ms)
            await self._services.bus.publish(ResponseStartEvent(session_id=self._session.id, response_id=self.id,
                                                                mode=self.mode))

    async def delta(self, text: str, speak: bool = True) -> None:
        """Append streamed text. `speak=False` shows it on the HUD without reading it aloud."""
        if not text:
            return
        await self._start()
        self.text += text
        await self._services.bus.publish(ResponseDeltaEvent(session_id=self._session.id, response_id=self.id,
                                                            delta=text))
        if speak:
            for sentence in self._chunker.feed(text):
                await self._speak(sentence)

    async def say(self, text: str) -> None:
        """Emit a complete sentence (deterministic responses)."""
        separator = " " if self.text and not self.text.endswith((" ", "\n")) else ""
        await self.delta(separator + text.strip() + " ")

    async def _speak(self, sentence: str) -> None:
        if not self.speaking_enabled:
            return
        if not self.spoke:
            self._services.metrics.record("first_sentence", self._clock.ms)
            state = self._session.state
            if state.state in {AssistantState.THINKING, AssistantState.PLANNING, AssistantState.EXECUTING,
                               AssistantState.STANDBY, AssistantState.LISTENING, AssistantState.BACKGROUND_TASK}:
                await state.set(AssistantState.SPEAKING, "speaking")
        self.spoke = True
        self._session.spoke(sentence)
        self._seq += 1
        await self._services.bus.publish(SpeakEvent(session_id=self._session.id, response_id=self.id,
                                                    seq=self._seq, text=sentence))

    async def finish(self, interrupted: bool = False) -> None:
        if self.finished:
            return
        self.finished = True
        if not interrupted:
            for sentence in self._chunker.flush():
                await self._speak(sentence)
            if self._chunker.code_skipped and self.spoke:
                await self._speak("I've put the code on screen.")
        if self._started:
            self._services.metrics.record("response_total", self._clock.ms)
            await self._services.bus.publish(ResponseEndEvent(session_id=self._session.id, response_id=self.id,
                                                              text=self.text.strip(), interrupted=interrupted))
