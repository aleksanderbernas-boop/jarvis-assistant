"""Self-diagnostics: what JARVIS can do, what it's doing, and why things failed."""

from __future__ import annotations

from pydantic import BaseModel

from jarvis.logging_setup import recent_errors
from jarvis.tools.base import Tool, ToolContext, ToolResult


class Diagnostics(Tool):
    name = "diagnostics"
    description = "Full self-diagnostic: backend, language model, voice, tools, memory, connections, tasks, errors."
    display_name = "Running diagnostics"
    category = "diagnostics"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        report = await ctx.services.diagnostics()
        problems = [k for k, v in report["components"].items() if not v["ok"]]
        summary = "All systems nominal" if not problems else f"Issues with: {', '.join(problems)}"
        return ToolResult.success(summary, report)


class ListTools(Tool):
    name = "list_tools"
    description = "List JARVIS's tools/capabilities and whether each is enabled."
    display_name = "Listing capabilities"
    category = "diagnostics"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        rows = ctx.services.registry.describe(ctx.services)
        enabled = [r for r in rows if r["enabled"]]
        return ToolResult.success(f"{len(enabled)} of {len(rows)} tools enabled", {"tools": [
            {"name": r["name"], "category": r["category"], "enabled": r["enabled"], "reason": r["reason"] or None}
            for r in rows]})


class RecentErrors(Tool):
    name = "recent_errors"
    description = "Recent warnings and errors from JARVIS's logs — use to explain why something failed."
    display_name = "Checking recent errors"
    category = "diagnostics"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        errors = list(recent_errors)[-15:]
        return ToolResult.success(f"{len(errors)} recent warnings/errors", {"errors": errors})


TOOLS = [Diagnostics, ListTools, RecentErrors]
