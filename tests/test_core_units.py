"""Unit tests: state machine, router, time parsing, wake word, speech chunking, confirmations, config."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jarvis.api.schemas import AssistantState as S
from jarvis.config import load_dotenv, load_settings
from jarvis.core.confirmation import parse_confirmation
from jarvis.core.router import IntentKind, IntentRouter, normalize
from jarvis.core.state import InvalidTransition, StateMachine
from jarvis.core.timeparse import parse_duration, parse_when
from jarvis.llm.speech import SentenceChunker, speech_text
from jarvis.logging_setup import Redactor
from jarvis.voice.wake import WakeWordDetector

# ── State machine ────────────────────────────────────────────────────────────


async def test_state_machine_publishes_legal_transitions() -> None:
    seen: list[tuple[S, S]] = []

    async def on_change(new: S, old: S, reason: str) -> None:
        seen.append((old, new))

    sm = StateMachine(on_change)
    await sm.transition(S.LISTENING)
    await sm.transition(S.THINKING)
    await sm.transition(S.EXECUTING)
    await sm.transition(S.SPEAKING)
    await sm.transition(S.LISTENING)
    assert seen == [(S.STANDBY, S.LISTENING), (S.LISTENING, S.THINKING), (S.THINKING, S.EXECUTING),
                    (S.EXECUTING, S.SPEAKING), (S.SPEAKING, S.LISTENING)]


async def test_state_machine_rejects_illegal_transition() -> None:
    sm = StateMachine()
    with pytest.raises(InvalidTransition):
        await sm.transition(S.EXECUTING)  # STANDBY → EXECUTING skips THINKING
    assert sm.state is S.STANDBY


async def test_state_machine_set_recovers_instead_of_sticking() -> None:
    sm = StateMachine(initial=S.INTERRUPTED)
    await sm.set(S.WAITING_FOR_CONFIRMATION)
    assert sm.state is S.STANDBY  # recovered to a safe state rather than an inconsistent one


# ── Intent router ────────────────────────────────────────────────────────────

router = IntentRouter()


@pytest.mark.parametrize("text", [
    "Open Chrome.", "Could you open Chrome?", "JARVIS, fire up Chrome.", "Let's get Chrome running.",
    "Can you launch my browser?", "please open spotify for me",
])
def test_open_app_variants(text: str) -> None:
    intent = router.route(text)
    assert intent.kind is IntentKind.OPEN_APP


def test_normalize_strips_politeness() -> None:
    assert normalize("Could you please open Chrome, please?") == "open Chrome"
    assert normalize("Hey JARVIS, would you mind opening the door") == "opening the door"


@pytest.mark.parametrize("text,kind", [
    ("stop", IntentKind.STOP), ("Never mind.", IntentKind.STOP), ("shut up", IntentKind.STOP),
    ("abort the task", IntentKind.CANCEL_TASK), ("stop the laptop research", IntentKind.CANCEL_TASK),
    ("What are you working on?", IntentKind.WORKING_ON), ("what time is it", IntentKind.TIME),
    ("what's the date today", IntentKind.DATE), ("JARVIS, diagnostics.", IntentKind.DIAGNOSTICS),
    ("what's using most of my memory?", IntentKind.TOP_PROCESSES),
    ("Remember that I prefer VS Code", IntentKind.REMEMBER), ("Forget that.", IntentKind.FORGET_LAST),
    ("forget that I prefer VS Code", IntentKind.FORGET), ("What do you remember about me?", IntentKind.RECALL_ALL),
    ("clear my memories", IntentKind.CLEAR_MEMORY), ("Set a timer for 10 minutes", IntentKind.TIMER),
    ("Remind me in 30 minutes to stretch", IntentKind.REMINDER), ("show me the weather", IntentKind.WEATHER),
    ("mute", IntentKind.VOLUME), ("set volume to 40%", IntentKind.VOLUME),
    ("Research the best laptops under 5000 zł", IntentKind.RESEARCH),
    ("Continue the laptop research from yesterday", IntentKind.RESEARCH),
    ("Organize all my screenshots from this month", IntentKind.AGENT_TASK),
    ("check system status", IntentKind.SYSTEM_STATUS),
])
def test_deterministic_intents(text: str, kind: IntentKind) -> None:
    assert router.route(text).kind is kind


@pytest.mark.parametrize("text", [
    "Open Chrome, search for today's weather, and tell me whether I need an umbrella.",
    "What is quantum entanglement?",
    "open the report I wrote yesterday",
    "close the window with the YouTube video",
    "Only show me ones with OLED.",
    "No, I meant tomorrow.",
])
def test_complex_or_contextual_requests_go_to_the_llm(text: str) -> None:
    assert router.route(text).kind is IntentKind.LLM


def test_research_extracts_deadline_and_continuation() -> None:
    intent = router.route("research GPU prices before 8 pm")
    assert intent.params["topic"] == "GPU prices"
    assert intent.params["deadline"].hour == 20
    assert router.route("continue the laptop research").params["continue"] is True


def test_remember_classifies_preferences() -> None:
    assert router.route("remember I prefer dark mode").params["kind"] == "preference"
    assert router.route("remember my project lives in D:/code/atlas").params["kind"] == "fact"


# ── Time parsing ─────────────────────────────────────────────────────────────

NOW = datetime(2026, 9, 2, 18, 34).astimezone()


def test_relative_times() -> None:
    parsed = parse_when("in 30 minutes to stretch", NOW)
    assert parsed and parsed.when - NOW == timedelta(minutes=30)
    assert parsed.remainder == "to stretch"
    assert parse_duration("an hour and a half")[0] == timedelta(minutes=90)  # type: ignore[index]
    assert parse_duration("1h 20m")[0] == timedelta(minutes=80)  # type: ignore[index]


def test_absolute_times() -> None:
    at8 = parse_when("at 8 pm remind me to study", NOW)
    assert at8 and (at8.when.hour, at8.when.day) == (20, 2)
    morning = parse_when("tomorrow morning about the meeting", NOW)
    assert morning and (morning.when.hour, morning.when.day) == (9, 3)
    past = parse_when("at 9:15", NOW)  # already passed today → tomorrow
    assert past and past.when.day == 3
    assert parse_when("sometime", NOW) is None


# ── Wake word ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,remainder", [
    ("Hey Jarvis, open Chrome", "open Chrome"), ("Jarvis.", ""), ("Jervis what time is it", "what time is it"),
    ("OK Jarvice, mute", "mute"), ("Open Spotify, Jarvis.", "Open Spotify"),
])
def test_wake_word_tolerates_transcription_errors(text: str, remainder: str) -> None:
    result = WakeWordDetector(["jarvis", "hey jarvis"]).check(text)
    assert result.detected and result.remainder == remainder


def test_wake_word_ignores_ordinary_speech() -> None:
    detector = WakeWordDetector(["jarvis"])
    assert not detector.check("I was talking to Jarrod about the service").detected
    assert not detector.check("what a nice day").detected


# ── Streaming speech ─────────────────────────────────────────────────────────


def test_sentence_chunker_emits_sentences_as_they_complete() -> None:
    chunker = SentenceChunker()
    assert chunker.feed("Certainly. I'm check") == ["Certainly."]
    assert chunker.feed("ing Dr. Smith's notes now. The") == ["I'm checking Dr. Smith's notes now."]
    assert chunker.flush() == ["The"]


def test_sentence_chunker_skips_code_blocks() -> None:
    chunker = SentenceChunker()
    out = chunker.feed("Here you go:\n```python\nprint('x.')\n```\nDone. ")
    assert out == ["Here you go:", "Done."]
    assert chunker.code_skipped


def test_speech_text_sanitises_markdown() -> None:
    assert speech_text("**Bold** and [a link](https://x.com) [1] — see https://y.org/z") == "Bold and a link — see"


# ── Confirmation parsing ─────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("yes", True), ("Yes, go ahead.", True), ("do it", True), ("proceed", True),
    ("no", False), ("don't", False), ("wait", False), ("yes... no, stop", False),
    ("what's the weather", None), ("", None),
])
def test_confirmation_parsing(text: str, expected: bool | None) -> None:
    assert parse_confirmation(text) is expected


# ── Config & secrets ─────────────────────────────────────────────────────────


def test_dotenv_and_secret_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text('GROQ_API_KEY="gsk_testtesttesttesttesttest1234"\nJARVIS_ALLOW_VISION=yes # comment\n')
    for key in ("GROQ_API_KEY", "JARVIS_ALLOW_VISION", "JARVIS_LLM_PROVIDER", "JARVIS_MODE"):
        monkeypatch.delenv(key, raising=False)
    load_dotenv(env)
    settings = load_settings(env_file=env)
    assert settings.permissions.allow_vision is True
    assert settings.llm.api_key and "gsk_" not in repr(settings)  # SecretStr hides it
    assert "gsk_testtesttesttesttesttest1234" not in str(settings.public_summary())


def test_redactor_scrubs_known_and_unknown_keys() -> None:
    redactor = Redactor()
    redactor.add_secrets(["my-configured-secret-value"])
    text = "key my-configured-secret-value and gsk_ABCDEFGHIJKLMNOPQRSTUVWX api_key=abcdef123456"
    cleaned = redactor.redact(text)
    assert "my-configured-secret-value" not in cleaned
    assert "gsk_ABC" not in cleaned
    assert "abcdef123456" not in cleaned


def test_logging_never_writes_secrets(tmp_path: Path) -> None:
    from jarvis.logging_setup import setup_logging

    setup_logging("INFO", tmp_path, ["super-secret-token-123"])
    logging.getLogger("test").warning("leaking super-secret-token-123 and gsk_ZZZZZZZZZZZZZZZZZZZZZZZZ")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = (tmp_path / "jarvis.log").read_text()
    assert "super-secret-token-123" not in content and "gsk_ZZZ" not in content
