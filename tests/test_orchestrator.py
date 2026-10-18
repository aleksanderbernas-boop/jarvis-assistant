"""Integration: input → brain → tools → verified result → response, with interruption and failures."""

from __future__ import annotations

import asyncio
import json

from jarvis.api.schemas import AssistantState as S
from jarvis.llm.base import LLMError, ToolCall
from jarvis.llm.mock import ScriptedTurn
from tests.conftest import Harness


def tool_messages(h: Harness) -> list[dict]:
    return [m for m in h.session.history if m.get("role") == "tool"]


# ── Deterministic fast path ─────────────────────────────────────────────────


async def test_fast_path_answers_without_llm(scripted) -> None:  # type: ignore[no-untyped-def]
    h = await scripted([])
    await h.say("what time is it?")
    await h.say("Could you open Spotify, please?")
    assert h.services.llm.calls == []  # no LLM call for either
    responses = h.responses()
    assert responses[0].startswith("It's ")
    assert "Spotify" in responses[1]
    completed = h.of_type("tool_completed")
    assert completed[-1].tool == "open_app" and completed[-1].verified is True


async def test_state_returns_to_idle_after_turn(harness: Harness) -> None:
    await harness.say("what's the date")
    states = [e.state for e in harness.of_type("assistant_state")]
    assert states[0] is S.THINKING
    assert harness.session.state.state in {S.STANDBY, S.LISTENING}


# ── LLM tool loop ───────────────────────────────────────────────────────────


async def test_llm_uses_tools_then_answers_from_results(scripted) -> None:  # type: ignore[no-untyped-def]
    h = await scripted([
        ScriptedTurn(text="Checking. ", tool_calls=[("weather", {})]),
        ScriptedTurn(text="It's 18 degrees and partly cloudy, so no umbrella needed."),
    ])
    await h.say("Do I need an umbrella today?")
    assert h.responses()[-1].startswith("Checking. It's 18 degrees")
    assert json.loads(tool_messages(h)[0]["content"])["ok"] is True
    # The second LLM call saw the tool result.
    assert h.services.llm.calls[1]["messages"][-1]["role"] == "tool"


async def test_conversation_context_persists_across_turns(scripted) -> None:  # type: ignore[no-untyped-def]
    h = await scripted([
        ScriptedTurn(text="", tool_calls=[("web_search", {"query": "cheapest laptop 32GB RAM"})]),
        ScriptedTurn(text="The cheapest is the Model A."),
        ScriptedTurn(text="Of those, only Model B has OLED."),
    ])
    await h.say("Find the cheapest laptop with 32GB RAM.")
    await h.say("Only show me ones with OLED.")
    second_call = h.services.llm.calls[2]["messages"]
    contents = " ".join(str(m.get("content", "")) for m in second_call)
    assert "Find the cheapest laptop with 32GB RAM." in contents   # prior request
    assert "cheapest laptop 32GB RAM" in contents                   # prior tool result
    assert second_call[-1] == {"role": "user", "content": "Only show me ones with OLED."}


async def test_relevant_memory_reaches_the_prompt(scripted) -> None:  # type: ignore[no-untyped-def]
    h = await scripted([ScriptedTurn(text="Opening VS Code.")])
    h.services.memory.add("User: I prefer VS Code for editing code", "preference")
    await h.say("open my usual code editor and my project")
    system_prompt = h.services.llm.calls[0]["messages"][0]["content"]
    assert "VS Code" in system_prompt and "Relevant memories" in system_prompt
    assert "Your voice: replies are spoken by the HUD" in system_prompt  # knows its real voice setup


async def test_malformed_tool_arguments_are_reported_to_the_model(scripted) -> None:  # type: ignore[no-untyped-def]
    h = await scripted([
        ScriptedTurn(raw_tool_calls=[ToolCall("bad1", "fs_list", "{not json")]),
        ScriptedTurn(text="Sorry — let me put that differently.", tool_calls=[("fs_list", {"path": "~"})]),
        ScriptedTurn(text="Done."),
    ])
    await h.say("list my home folder files")
    results = [json.loads(m["content"]) for m in tool_messages(h)]
    assert results[0]["ok"] is False and "Invalid arguments" in results[0]["error"]
    assert h.responses()[-1].endswith("Done.")


async def test_provider_rejecting_a_tool_call_is_retried(scripted) -> None:  # type: ignore[no-untyped-def]
    h = await scripted([
        ScriptedTurn(error=LLMError("bad tool call", kind="tool_use_failed", retryable=True)),
        ScriptedTurn(text="Recovered."),
    ])
    await h.say("what is on my calendar and my files")
    assert h.responses()[-1] == "Recovered."


async def test_llm_outage_is_explained_truthfully(scripted) -> None:  # type: ignore[no-untyped-def]
    h = await scripted([ScriptedTurn(error=LLMError("401", kind="auth"))])
    await h.say("explain quantum entanglement")
    assert "API key" in h.responses()[-1]
    assert S.ERROR in [e.state for e in h.of_type("assistant_state")]
    assert h.session.state.state in {S.STANDBY, S.LISTENING}


async def test_step_limit_prevents_infinite_loops(scripted) -> None:  # type: ignore[no-untyped-def]
    loop_forever = [ScriptedTurn(tool_calls=[("system_status", {})]) for _ in range(40)]
    h = await scripted(loop_forever + [ScriptedTurn(text="I stopped after too many steps.")])
    h.services.settings.tasks.max_agent_steps = 3
    await h.say("keep checking the system forever")
    assert len([c for c in h.services.llm.calls if "tools" in c]) <= 5
    assert h.session.state.state in {S.STANDBY, S.LISTENING}


# ── Interruption ────────────────────────────────────────────────────────────


async def test_stop_cancels_the_current_turn(scripted) -> None:  # type: ignore[no-untyped-def]
    long_answer = "This is a very long explanation. " * 200
    h = await scripted([ScriptedTurn(text=long_answer)])
    await h.orchestrator.on_user_input(h.session, "tell me everything about stars")
    await asyncio.sleep(0.05)
    assert h.session.busy
    await h.say("stop")
    assert not h.session.busy
    ends = h.of_type("response_end")
    assert any(e.interrupted for e in ends)
    assert h.of_type("speech_stop")
    assert ends[-1].text == "Stopped." or ends[-1].text == "Stopping."


async def test_new_request_supersedes_old_one(scripted) -> None:  # type: ignore[no-untyped-def]
    h = await scripted([ScriptedTurn(text="Slow answer. " * 300), ScriptedTurn(text="Fresh answer.")])
    await h.orchestrator.on_user_input(h.session, "tell me a long story")
    await asyncio.sleep(0.05)
    await h.say("actually, what's the capital of France?")
    ends = h.of_type("response_end")
    assert ends[0].interrupted and ends[-1].text == "Fresh answer."


async def test_barge_in_mutes_speech_but_background_tasks_continue(harness: Harness) -> None:
    await harness.say("research OLED laptops with 32GB of RAM")
    harness.session.voice_output = True
    await harness.orchestrator.on_barge_in(harness.session, "barge_in")
    assert harness.of_type("speech_stop")
    await asyncio.sleep(0.5)
    tasks = harness.services.tasks.recent(5)
    assert tasks and tasks[0].status.value in {"running", "completed"}  # not cancelled by the barge-in


# ── Voice: wake word, follow-ups, echo ──────────────────────────────────────


async def test_wake_word_gating_and_conversation_window(harness: Harness) -> None:
    harness.session.conversation_open_until = 0
    await harness.orchestrator.on_user_input(harness.session, "what time is it", "voice")
    assert harness.of_type("transcription")[-1].ignored  # no wake word, no open conversation
    await harness.say("Jarvis, what time is it?", source="voice")
    assert harness.responses()[-1].startswith("It's")
    await harness.say("and what's the date", source="voice")  # follow-up inside the window
    assert harness.responses()[-1].startswith("It's")
    assert len(harness.responses()) == 2


async def test_bare_wake_word_is_acknowledged(harness: Harness) -> None:
    await harness.say("Hey Jarvis.", source="voice")
    assert harness.responses()[-1] in {"Yes, sir?", "At your service.", "Sir?", "I'm listening.", "Go ahead."}


async def test_own_speech_picked_up_by_mic_is_ignored(harness: Harness) -> None:
    harness.session.spoke("The weather in Kraków is eighteen degrees and partly cloudy.")
    harness.session.open_conversation(30)
    await harness.say("the weather in Kraków is eighteen degrees and partly cloudy", source="voice")
    assert harness.of_type("transcription")[-1].ignored
    assert harness.responses() == []


# ── Confirmation through conversation ───────────────────────────────────────


async def test_voice_yes_approves_and_other_input_declines(harness: Harness) -> None:
    target = harness.home / "draft.txt"
    target.write_text("x")
    harness.services.memory.add("User: I like tea", "preference")
    await harness.orchestrator.on_user_input(harness.session, "clear my memories")
    await asyncio.sleep(0.1)
    assert harness.session.state.state is S.WAITING_FOR_CONFIRMATION
    request = harness.of_type("confirmation_request")[-1]
    assert "erase 1" in request.summary
    await harness.say("what time is it")  # not a yes → treated as no, then handled as a new request
    assert harness.services.memory.count() == 1
    await harness.orchestrator.on_user_input(harness.session, "clear my memories")
    await asyncio.sleep(0.1)
    await harness.say("yes, go ahead")
    await harness.settle()
    assert harness.services.memory.count() == 0


# ── Memory commands ─────────────────────────────────────────────────────────


async def test_remember_recall_forget_cycle(harness: Harness) -> None:
    await harness.say("Remember that I prefer VS Code.")
    await harness.say("What do you remember about me?")
    assert "you prefer VS Code" in harness.responses()[-1]
    await harness.say("Forget that.")
    assert harness.responses()[-1] == "Forgotten."
    assert harness.services.memory.count() == 0


# ── Tasks ───────────────────────────────────────────────────────────────────


async def test_what_are_you_working_on_and_cancel(harness: Harness) -> None:
    started = asyncio.Event()

    async def slow(handle):  # type: ignore[no-untyped-def]
        await handle.step("Crunching numbers", 0.3)
        started.set()
        await asyncio.sleep(30)
        return "done"

    await harness.services.tasks.create("agent", "Crunch the quarterly numbers", slow, session_id=harness.session.id)
    await asyncio.wait_for(started.wait(), 2)
    await harness.say("What are you working on?")
    assert "Crunch the quarterly numbers" in harness.responses()[-1] and "30%" in harness.responses()[-1]
    assert harness.session.state.state is S.BACKGROUND_TASK
    await harness.say("cancel the task")
    await asyncio.sleep(0.1)
    assert harness.services.tasks.active() == []
    assert harness.of_type("task_cancelled")
