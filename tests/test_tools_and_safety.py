"""Tool registry/executor behaviour, failure handling, confirmation gates and filesystem safety."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from jarvis.api.schemas import RiskLevel
from jarvis.llm.base import ToolCall
from jarvis.tools.base import RetryPolicy, RiskAssessment, Tool, ToolContext, ToolResult, TransientToolError
from tests.conftest import Harness


def call(tool_name: str, /, **args: Any) -> ToolCall:
    return ToolCall(id=f"t_{tool_name}", name=tool_name, arguments=json.dumps(args))


def ctx(h: Harness) -> ToolContext:
    return ToolContext(services=h.services, session_id=h.session.id)


async def run(h: Harness, tool_name: str, /, **args: Any) -> ToolResult:
    return await h.services.executor.execute(call(tool_name, **args), ctx(h))


def auto_answer(h: Harness, approved: bool) -> list[Any]:
    """Answer every confirmation request, and record what was asked."""
    asked: list[Any] = []

    async def on_request(pending: Any) -> None:
        asked.append(pending.event)
        asyncio.get_running_loop().call_soon(h.services.confirmations.resolve,
                                             pending.event.confirmation_id, approved)

    h.services.confirmations.on_request = on_request
    return asked


# ── Registry ─────────────────────────────────────────────────────────────────


async def test_registry_discovers_builtin_tools_with_valid_schemas(harness: Harness) -> None:
    tools = harness.services.registry.all()
    assert len(tools) >= 40
    names = {t.name for t in tools}
    assert {"open_app", "fs_delete", "web_search", "memory_remember", "task_start", "describe_screen"} <= names
    for tool in tools:
        schema = tool.schema()
        assert schema["function"]["name"] == tool.name
        assert schema["function"]["parameters"]["type"] == "object"
        assert tool.description and tool.display_name and tool.category


async def test_permission_gates_disable_tools(harness: Harness) -> None:
    registry, services = harness.services.registry, harness.services
    enabled = {t.name for t in registry.enabled(services)}
    assert "describe_screen" not in enabled        # vision is opt-in
    assert "type_text" not in enabled              # input control is opt-in
    result = await run(harness, "type_text", text="hello")
    assert not result.ok and result.error_kind == "permission_denied"


async def test_tool_selection_is_focused(harness: Harness) -> None:
    tools, categories = harness.services.registry.select(harness.services, "open spotify")
    assert "apps" in categories
    assert "files" not in categories
    assert any(t.name == "open_app" for t in tools)


# ── Executor failure modes ───────────────────────────────────────────────────


async def test_unknown_tool_and_malformed_arguments(harness: Harness) -> None:
    unknown = await harness.services.executor.execute(ToolCall("x", "does_not_exist", "{}"), ctx(harness))
    assert not unknown.ok and unknown.error_kind == "unknown_tool"
    bad_json = await harness.services.executor.execute(ToolCall("x", "fs_list", "{path: nope"), ctx(harness))
    assert not bad_json.ok and bad_json.error_kind == "invalid_arguments"
    bad_args = await run(harness, "volume_control", action="explode")
    assert not bad_args.ok and "action" in (bad_args.error or "")


class SlowInput(BaseModel):
    pass


class SlowTool(Tool):
    name = "slow_tool"
    description = "sleeps"
    display_name = "Sleeping"
    category = "system"
    timeout_s = 0.05

    async def execute(self, args: Any, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(1)
        return ToolResult.success("never")


class FlakyTool(Tool):
    name = "flaky_tool"
    description = "fails once"
    display_name = "Flaky"
    category = "system"
    retry = RetryPolicy(max_attempts=3, backoff_s=0.01)
    attempts: ClassVar[int] = 0

    async def execute(self, args: Any, ctx: ToolContext) -> ToolResult:
        FlakyTool.attempts += 1
        if FlakyTool.attempts < 2:
            raise TransientToolError("temporary glitch")
        return ToolResult.success("worked on retry")


class CrashTool(Tool):
    name = "crash_tool"
    description = "has a bug"
    display_name = "Crashing"
    category = "system"

    async def execute(self, args: Any, ctx: ToolContext) -> ToolResult:
        raise ZeroDivisionError


class DangerTool(Tool):
    """Claims LOW risk in its assessment but is declared HIGH — the executor must still confirm."""

    name = "danger_tool"
    description = "dangerous"
    display_name = "Danger"
    category = "system"
    risk = RiskLevel.HIGH
    ran: ClassVar[int] = 0

    async def assess(self, args: Any, ctx: ToolContext) -> RiskAssessment:
        return RiskAssessment(RiskLevel.LOW)

    async def execute(self, args: Any, ctx: ToolContext) -> ToolResult:
        DangerTool.ran += 1
        return ToolResult.success("did something dangerous")


async def test_timeout_retry_and_crash_containment(harness: Harness) -> None:
    for tool in (SlowTool(), FlakyTool(), CrashTool()):
        harness.services.registry.register(tool)
    timed_out = await run(harness, "slow_tool")
    assert not timed_out.ok and timed_out.error_kind == "timeout"
    flaky = await run(harness, "flaky_tool")
    assert flaky.ok and FlakyTool.attempts == 2
    crashed = await run(harness, "crash_tool")
    assert not crashed.ok and crashed.error_kind == "internal"
    failed_events = harness.of_type("tool_failed")
    assert {e.tool for e in failed_events} >= {"slow_tool", "crash_tool"}


async def test_high_risk_cannot_bypass_confirmation(harness: Harness) -> None:
    harness.services.registry.register(DangerTool())
    harness.services.settings.permissions.confirm_medium_risk = False  # must not matter for HIGH
    asked = auto_answer(harness, approved=False)
    result = await run(harness, "danger_tool")
    assert asked, "a HIGH-risk tool must always ask"
    assert not result.ok and result.error_kind == "declined" and DangerTool.ran == 0
    auto_answer(harness, approved=True)
    assert (await run(harness, "danger_tool")).ok and DangerTool.ran == 1


async def test_confirmation_timeout_counts_as_no(harness: Harness) -> None:
    harness.services.registry.register(DangerTool())
    harness.services.confirmations._timeout_s = 0.05
    before = DangerTool.ran
    result = await run(harness, "danger_tool")
    assert not result.ok and result.error_kind == "declined" and DangerTool.ran == before
    resolved = harness.of_type("confirmation_resolved")
    assert resolved and resolved[-1].reason == "timeout" and not resolved[-1].approved


async def test_audit_log_records_risky_actions(harness: Harness) -> None:
    auto_answer(harness, approved=False)
    target = harness.home / "keep.txt"
    target.write_text("important")
    await run(harness, "fs_delete", paths=[str(target)])
    audit = (harness.services.settings.data_dir / "audit.log").read_text().strip().splitlines()
    entry = json.loads(audit[-1])
    assert entry["tool"] == "fs_delete" and entry["confirmed"] is False and entry["risk"] == "high"


# ── Filesystem safety ────────────────────────────────────────────────────────


async def test_path_guard_blocks_outside_and_sensitive_paths(harness: Harness, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    assert (await run(harness, "fs_read", path=str(outside))).error_kind == "permission_denied"
    ssh = harness.home / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("KEY")
    assert (await run(harness, "fs_read", path=str(ssh / "id_rsa"))).error_kind == "permission_denied"
    (harness.home / ".env").write_text("GROQ_API_KEY=x")
    assert (await run(harness, "fs_read", path=str(harness.home / ".env"))).error_kind == "permission_denied"
    escape = harness.home / "link"
    escape.symlink_to(tmp_path)
    assert (await run(harness, "fs_list", path=str(escape))).error_kind == "permission_denied"


async def test_delete_requires_confirmation_and_really_deletes(harness: Harness, tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    trash.mkdir()
    # The simulated computer never touches disk; give this test a real recycle bin.
    harness.services.platform.trash = lambda p: bool(shutil.move(str(p), str(trash / p.name)))  # type: ignore[method-assign]
    folder = harness.home / "old-project"
    folder.mkdir()
    for i in range(3):
        (folder / f"f{i}.txt").write_text("x")
    asked = auto_answer(harness, approved=False)
    declined = await run(harness, "fs_delete", paths=[str(folder)])
    assert not declined.ok and folder.exists()
    assert "3 files" in asked[0].summary and asked[0].risk == RiskLevel.HIGH
    auto_answer(harness, approved=True)
    deleted = await run(harness, "fs_delete", paths=[str(folder)])
    assert deleted.ok and deleted.verified is True and not folder.exists() and (trash / "old-project").exists()


async def test_unconfirmed_effect_is_reported_as_unverified(harness: Harness) -> None:
    """If the OS call 'succeeds' but nothing changed, JARVIS must not claim success."""
    target = harness.home / "stubborn.txt"
    target.write_text("x")
    harness.services.platform.trash = lambda p: True  # type: ignore[method-assign]  # lies: does nothing
    auto_answer(harness, approved=True)
    result = await run(harness, "fs_delete", paths=[str(target)])
    assert result.verified is False and target.exists()


async def test_write_overwrite_keeps_backup(harness: Harness) -> None:
    target = harness.home / "notes.txt"
    created = await run(harness, "fs_write", path=str(target), content="v1")
    assert created.ok and created.verified is True
    exists = await run(harness, "fs_write", path=str(target), content="v2")
    assert not exists.ok and exists.error_kind == "exists"
    auto_answer(harness, approved=True)
    overwritten = await run(harness, "fs_write", path=str(target), content="v2", mode="overwrite")
    assert overwritten.ok and target.read_text() == "v2"
    assert Path(overwritten.data["backup"]).read_text() == "v1"


async def test_bulk_move_is_confirmed_and_verified(harness: Harness) -> None:
    shots = harness.home / "Screenshots"
    shots.mkdir()
    files = []
    for i in range(25):
        f = shots / f"Screenshot_{i}.png"
        f.write_bytes(b"png")
        files.append(str(f))
    asked = auto_answer(harness, approved=True)
    result = await run(harness, "fs_move", sources=files, destination=str(shots / "2026-09"))
    assert asked and "25 files" in asked[0].summary
    assert result.ok and result.verified is True
    assert len(list((shots / "2026-09").iterdir())) == 25


async def test_search_and_read(harness: Harness) -> None:
    (harness.home / "docs").mkdir()
    (harness.home / "docs" / "plan.md").write_text("Project Atlas launch plan")
    (harness.home / "docs" / "other.txt").write_text("nothing here")
    found = await run(harness, "fs_search", root=str(harness.home), content_contains="atlas")
    assert found.ok and [r["path"] for r in found.data["results"]] == [str(harness.home / "docs" / "plan.md")]
    read = await run(harness, "fs_read", path=str(harness.home / "docs" / "plan.md"))
    assert read.ok and "Atlas" in read.data["content"]


# ── Memory ───────────────────────────────────────────────────────────────────


async def test_memory_dedupes_retrieves_and_forgets(harness: Harness) -> None:
    store = harness.services.memory
    first, created = store.add("User prefers VS Code for Python work", "preference")
    again, created_again = store.add("User prefers VS Code for Python work.", "preference")
    assert created and not created_again and first.id == again.id
    store.add("User's project Atlas lives in D:/code/atlas", "fact")
    store.add("User's favourite football club is Wisła Kraków", "fact")
    hits = store.search("where is the atlas project")
    assert hits and "Atlas" in hits[0].memory.content
    context = store.context_for("open my editor")
    assert any("VS Code" in m.content for m in context)  # preferences are always in context
    forgot = await run(harness, "memory_forget", query="football club")
    assert forgot.ok and forgot.verified is True
    assert all("football" not in m.content for m in store.entries())


async def test_memory_refuses_secrets(harness: Harness) -> None:
    result = await run(harness, "memory_remember", content="My password is hunter2", kind="fact")
    assert not result.ok and result.error_kind == "refused"


# ── Tool memory ──────────────────────────────────────────────────────────────


async def test_tool_stats_prefer_methods_that_work(harness: Harness) -> None:
    stats = harness.services.tool_stats
    for _ in range(3):
        stats.record("open_app", "apps_folder", False)
        stats.record("open_app", "startfile", True)
    assert stats.rank("open_app", ["apps_folder", "startfile", "path_lookup"])[0] == "startfile"


async def test_open_app_verifies_and_reports_failures(harness: Harness) -> None:
    ok = await run(harness, "open_app", name="spotify")
    assert ok.ok and ok.verified is True and ok.simulated
    harness.services.platform.fail_launch.add("Discord")  # type: ignore[attr-defined]
    failed = await run(harness, "open_app", name="discord")
    assert not failed.ok and "failed" in (failed.error or "")
    missing = await run(harness, "open_app", name="photoshop 1997")
    assert not missing.ok and missing.error_kind == "not_found"
