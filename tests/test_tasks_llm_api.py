"""Background tasks, planner/agent, research agent, the streaming LLM client, and the HTTP/WebSocket API."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.api.schemas import TaskStatus, utcnow
from jarvis.app import create_app
from jarvis.llm.base import LLMError
from jarvis.llm.mock import ScriptedTurn
from jarvis.llm.openai_compat import OpenAICompatibleProvider
from tests.conftest import Harness, build_harness

# ── Task manager ─────────────────────────────────────────────────────────────


async def test_task_lifecycle_progress_and_persistence(harness: Harness) -> None:
    async def job(handle):  # type: ignore[no-untyped-def]
        await handle.step("Step one", 0.5)
        return "all done"

    task = await harness.services.tasks.create("agent", "Demo job", job, session_id=harness.session.id)
    for _ in range(100):
        if harness.services.tasks.get(task.id).status.is_terminal:  # type: ignore[union-attr]
            break
        await asyncio.sleep(0.01)
    final = harness.services.tasks.get(task.id)
    assert final and final.status is TaskStatus.COMPLETED and final.result == "all done" and final.progress == 1.0
    kinds = [e.type for e in harness.drain() if e.type.startswith("task_")]
    assert kinds[0] == "task_created" and kinds[-1] == "task_completed"
    assert harness.services.tasks.recent(1)[0].id == task.id  # persisted to SQLite


async def test_failed_task_is_announced_truthfully(harness: Harness) -> None:
    async def job(handle):  # type: ignore[no-untyped-def]
        raise RuntimeError("disk not found")

    await harness.services.tasks.create("agent", "Back up photos", job, session_id=harness.session.id)
    await asyncio.sleep(0.2)
    assert any("failed: disk not found" in text for text in harness.responses())


async def test_deadline_risk_notifies_once(harness: Harness) -> None:
    async def job(handle):  # type: ignore[no-untyped-def]
        await handle.step("Working", 0.1)
        await asyncio.sleep(10)

    task = await harness.services.tasks.create("agent", "Slow report", job, session_id=harness.session.id,
                                               deadline=utcnow() + timedelta(seconds=2))
    await asyncio.sleep(0.3)
    snapshot = harness.services.tasks.get(task.id)
    assert snapshot
    snapshot.started_at = utcnow() - timedelta(minutes=5)  # 10% done after 5 min → will miss a 2 s deadline
    await harness.services.tasks.check_deadlines()
    await harness.services.tasks.check_deadlines()
    warnings = [n for n in harness.of_type("notification") if n.title == "Deadline at risk"]
    assert len(warnings) == 1
    harness.services.tasks.cancel(task.id)


async def test_restart_marks_orphaned_tasks_failed(tmp_path: Path) -> None:
    h = build_harness(tmp_path)
    h.services.db.execute(
        "INSERT INTO tasks(id, session_id, kind, description, status, snapshot, created_at, updated_at) "
        "VALUES ('t1', NULL, 'agent', 'x', 'running', ?, ?, ?)",
        (json.dumps({"id": "t1", "kind": "agent", "description": "x", "status": "running",
                     "created_at": utcnow().isoformat()}), utcnow().isoformat(), utcnow().isoformat()),
    )
    await h.services.boot()
    restored = h.services.tasks.get("t1")
    assert restored and restored.status is TaskStatus.FAILED and "restarted" in (restored.error or "")
    await h.services.shutdown()


# ── Planner / agent ──────────────────────────────────────────────────────────


async def test_agent_plans_executes_and_verifies(scripted) -> None:  # type: ignore[no-untyped-def]
    plan = {"goal": "tidy notes", "steps": [
        {"id": "s1", "description": "Create the archive folder", "depends_on": []},
        {"id": "s2", "description": "Confirm the folder exists", "depends_on": ["s1"]},
    ]}
    h = await scripted(
        [
            ScriptedTurn(tool_calls=[("fs_mkdir", {"path": "__HOME__/archive"})]),
            ScriptedTurn(tool_calls=[("report_step", {"status": "completed", "summary": "folder created",
                                                      "verified": True})]),
            ScriptedTurn(tool_calls=[("fs_info", {"path": "__HOME__/archive"})]),
            ScriptedTurn(tool_calls=[("report_step", {"status": "completed", "summary": "exists", "verified": True})]),
        ],
        completions=[json.dumps(plan), "Both steps completed: the archive folder exists."],
    )
    for turn in h.services.llm.turns:
        turn.tool_calls = [(n, {k: str(v).replace("__HOME__", str(h.home)) for k, v in a.items()} if n != "report_step"
                            else a) for n, a in turn.tool_calls]
    task = await h.services.launcher.start_agent("tidy my notes", session_id=h.session.id)
    for _ in range(300):
        if h.services.tasks.get(task.id).status.is_terminal:  # type: ignore[union-attr]
            break
        await asyncio.sleep(0.01)
    final = h.services.tasks.get(task.id)
    assert final and final.status is TaskStatus.COMPLETED, final.error if final else None
    assert (h.home / "archive").is_dir()
    assert [s.status for s in final.steps] == [TaskStatus.COMPLETED, TaskStatus.COMPLETED]
    assert "2 of 2" in (final.result or "")


async def test_agent_does_not_trust_unsupported_success_claims(scripted) -> None:  # type: ignore[no-untyped-def]
    plan = {"goal": "g", "steps": [{"id": "s1", "description": "Read a missing file", "depends_on": []}]}
    attempt = [
        ScriptedTurn(tool_calls=[("fs_read", {"path": "/definitely/not/allowed.txt"})]),
        ScriptedTurn(tool_calls=[("report_step", {"status": "completed", "summary": "read it"})]),
    ]
    h = await scripted(attempt + attempt, completions=[json.dumps(plan), "Nothing was completed."])
    task = await h.services.launcher.start_agent("read the file", session_id=h.session.id)
    for _ in range(300):
        if h.services.tasks.get(task.id).status.is_terminal:  # type: ignore[union-attr]
            break
        await asyncio.sleep(0.01)
    final = h.services.tasks.get(task.id)
    assert final and final.steps[0].status is TaskStatus.FAILED
    assert "claimed success" in (final.steps[0].error or "")


# ── Research ─────────────────────────────────────────────────────────────────


async def test_research_cites_sources_and_is_remembered(harness: Harness) -> None:
    await harness.say("research the best OLED laptops with 32GB RAM")
    for _ in range(300):
        if not harness.services.tasks.active():
            break
        await asyncio.sleep(0.01)
    task = harness.services.tasks.recent(1)[0]
    assert task.status is TaskStatus.COMPLETED
    assert "https://" in (task.result or "")
    assert harness.services.web.fetched  # type: ignore[attr-defined]  # sources were actually opened
    episodic = harness.services.memory.entries("episodic")
    assert episodic and "OLED laptops" in episodic[0].content
    assert harness.services.launcher.prior_research("OLED laptops 32GB RAM") is not None


# ── Streaming LLM client ─────────────────────────────────────────────────────


def sse(*events: dict) -> bytes:
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events) + b"data: [DONE]\n\n"


async def test_stream_parses_text_and_split_tool_calls() -> None:
    body = sse(
        {"choices": [{"delta": {"content": "On it. "}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "open_", "arguments": "{\"na"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "app", "arguments": "me\": \"Chrome\"}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    llm = OpenAICompatibleProvider("https://x/v1", "k", transport=transport)
    chunks = [c async for c in llm.stream_chat("m", [{"role": "user", "content": "hi"}])]
    assert "".join(c.text for c in chunks) == "On it. "
    call = chunks[-1].tool_calls[0]
    assert (call.id, call.name, call.parsed()[0]) == ("c1", "open_app", {"name": "Chrome"})
    await llm.close()


async def test_stream_retries_rate_limits_and_classifies_auth_errors() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, text="slow down")
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "ok"}}]}))

    llm = OpenAICompatibleProvider("https://x/v1", "k", transport=httpx.MockTransport(handler))
    text = "".join([c.text async for c in llm.stream_chat("m", [])])
    assert text == "ok" and attempts["n"] == 2
    await llm.close()

    denied = OpenAICompatibleProvider("https://x/v1", "bad",
                                      transport=httpx.MockTransport(lambda r: httpx.Response(401, text="no")))
    with pytest.raises(LLMError) as exc:
        [c async for c in denied.stream_chat("m", [])]
    assert exc.value.kind == "auth"
    await denied.close()


async def test_network_failure_is_classified() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    llm = OpenAICompatibleProvider("https://x/v1", "k", transport=httpx.MockTransport(boom), max_retries=1)
    with pytest.raises(LLMError) as exc:
        await llm.complete("m", [])
    assert exc.value.kind == "network"
    await llm.close()


# ── HTTP / WebSocket API ─────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path: Path):  # type: ignore[no-untyped-def]
    h = build_harness(tmp_path)
    app = create_app(h.services.settings, services=h.services)
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        test_client.token = app.state.token  # type: ignore[attr-defined]
        yield test_client


LOCAL = {"host": "127.0.0.1:8765"}


def test_websocket_rejects_foreign_host(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/ws?token={client.token}") as ws:  # type: ignore[attr-defined]
        ws.receive_text()  # Host: testserver is not local → closed before hello


def test_api_requires_token_and_local_host(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/diagnostics").status_code == 401
    headers = {"X-Jarvis-Token": client.token}  # type: ignore[attr-defined]
    assert client.get("/api/diagnostics", headers=headers).status_code == 200
    assert client.get("/api/tools", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 403
    page = client.get("/")
    assert client.token in page.text  # type: ignore[attr-defined]
    assert "GROQ" not in page.text and "api_key" not in page.text.lower()


def test_websocket_conversation_roundtrip(client: TestClient) -> None:
    with client.websocket_connect(f"/ws?token={client.token}", headers=LOCAL) as ws:  # type: ignore[attr-defined]
        ws.send_text(json.dumps({"type": "hello"}))
        snapshot = json.loads(ws.receive_text())
        assert snapshot["type"] == "session_snapshot" and snapshot["config"]["boot_complete"]
        assert "api_key" not in json.dumps(snapshot["config"]).lower()
        ws.send_text(json.dumps({"type": "user_text", "text": "what time is it"}))
        seen = []
        for _ in range(50):
            event = json.loads(ws.receive_text())
            seen.append(event["type"])
            if event["type"] == "response_end":
                assert event["text"].startswith("It's")
                break
        assert "assistant_state" in seen and "speak" in seen
        ws.send_text(json.dumps({"type": "nonsense"}))
        for _ in range(20):
            event = json.loads(ws.receive_text())
            if event["type"] == "error":
                assert event["code"] == "invalid_message"
                break


def test_websocket_rejects_bad_token_and_session_restores(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws?token=wrong", headers=LOCAL) as ws:
        ws.receive_text()
    with client.websocket_connect(f"/ws?token={client.token}", headers=LOCAL) as ws:  # type: ignore[attr-defined]
        ws.send_text(json.dumps({"type": "hello"}))
        session_id = json.loads(ws.receive_text())["session_id"]
        ws.send_text(json.dumps({"type": "user_text", "text": "what's the date"}))
        while json.loads(ws.receive_text())["type"] != "response_end":
            pass
    with client.websocket_connect(f"/ws?token={client.token}", headers=LOCAL) as ws:  # type: ignore[attr-defined]
        ws.send_text(json.dumps({"type": "hello", "session_id": session_id}))
        restored = json.loads(ws.receive_text())
        assert restored["session_id"] == session_id
        assert [h["text"] for h in restored["history"]][0] == "what's the date"
