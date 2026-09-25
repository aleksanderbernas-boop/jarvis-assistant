"""Long-term memory tools — the user stays in control of what JARVIS remembers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from jarvis.api.schemas import MemoryUpdatedEvent, RiskLevel
from jarvis.tools.base import RiskAssessment, Tool, ToolContext, ToolError, ToolResult


async def _announce(ctx: ToolContext, action: str, memory_id: str | None, content: str | None) -> None:
    await ctx.services.bus.publish(MemoryUpdatedEvent(
        session_id=ctx.session_id, action=action, memory_id=memory_id, content=content,  # type: ignore[arg-type]
        total=ctx.services.memory.count()))
    session = ctx.services.sessions.get(ctx.session_id) if ctx.session_id else None
    if session and memory_id and action in {"added", "updated"}:
        session.last_memory_id = memory_id


class RememberInput(BaseModel):
    content: str = Field(min_length=3, max_length=1000, description="The fact/preference, phrased in third person, "
                                                                     "e.g. 'Prefers VS Code for Python work'")
    kind: Literal["preference", "fact"] = "fact"
    key: str | None = Field(None, description="Optional stable key, e.g. 'preferred_editor', so updates replace it")


class MemoryRemember(Tool):
    name = "memory_remember"
    description = ("Save a durable preference or fact about the user to long-term memory. Only for things worth "
                   "remembering across conversations — never secrets or passwords.")
    display_name = "Updating memory"
    category = "memory"
    Input = RememberInput

    def validate(self, args: RememberInput, ctx: ToolContext) -> None:
        lowered = args.content.lower()
        if any(word in lowered for word in ("password", "api key", "apikey", "secret key", "pin code", "gsk_", "sk-")):
            raise ToolError("that looks like a secret — I don't store credentials in memory", kind="refused")

    async def execute(self, args: RememberInput, ctx: ToolContext) -> ToolResult:
        memory, created = ctx.services.memory.add(args.content, args.kind, key=args.key, source="user_explicit")
        await _announce(ctx, "added" if created else "updated", memory.id, memory.content)
        return ToolResult.success(("Remembered: " if created else "Updated memory: ") + memory.content,
                                  {"id": memory.id, "created": created}, verified=ctx.services.memory.get(memory.id)
                                  is not None)


class RecallInput(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(5, ge=1, le=20)


class MemoryRecall(Tool):
    name = "memory_recall"
    description = "Search long-term memory (preferences, facts, past research and tasks)."
    display_name = "Searching memory"
    category = "memory"
    Input = RecallInput
    parallel_safe = True

    async def execute(self, args: RecallInput, ctx: ToolContext) -> ToolResult:
        hits = ctx.services.memory.search(args.query, limit=args.limit, min_score=0.05)
        if not hits:
            return ToolResult.success("Nothing relevant in memory", {"memories": []})
        return ToolResult.success(f"{len(hits)} relevant memories", {"memories": [
            {"id": h.memory.id, "kind": h.memory.kind, "content": h.memory.content,
             "updated": h.memory.updated_at[:10]} for h in hits]})


class ListInput(BaseModel):
    kind: Literal["preference", "fact", "episodic"] | None = None


class MemoryList(Tool):
    name = "memory_list"
    description = "List what JARVIS remembers about the user."
    display_name = "Reviewing memory"
    category = "memory"
    Input = ListInput
    parallel_safe = True

    async def execute(self, args: ListInput, ctx: ToolContext) -> ToolResult:
        memories = ctx.services.memory.entries(args.kind, limit=100)
        return ToolResult.success(f"{len(memories)} memories", {"memories": [
            {"id": m.id, "kind": m.kind, "content": m.content[:300]} for m in memories]})


class ForgetInput(BaseModel):
    memory_id: str | None = Field(None, description="Exact id from memory_list/recall")
    query: str | None = Field(None, description="What to forget, e.g. 'prefers VS Code'")


class MemoryForget(Tool):
    name = "memory_forget"
    description = "Delete a specific memory by id, or the single memory that best matches a description."
    display_name = "Updating memory"
    category = "memory"
    Input = ForgetInput

    async def execute(self, args: ForgetInput, ctx: ToolContext) -> ToolResult:
        store = ctx.services.memory
        if args.memory_id:
            target = store.get(args.memory_id)
            if not target:
                raise ToolError("no memory with that id", kind="not_found")
        elif args.query:
            hits = store.search(args.query, limit=3, min_score=0.05, touch=False)
            if not hits:
                raise ToolError(f"I have no memory matching '{args.query}'", kind="not_found")
            if len(hits) > 1 and hits[1].score > hits[0].score * 0.8:
                return ToolResult.failure("several memories match — which one?", kind="ambiguous", data={
                    "candidates": [{"id": h.memory.id, "content": h.memory.content} for h in hits]})
            target = hits[0].memory
        else:
            raise ToolError("give a memory_id or a query", kind="invalid_arguments")
        store.delete(target.id)
        await _announce(ctx, "deleted", target.id, target.content)
        return ToolResult.success(f"Forgot: {target.content}", {"id": target.id},
                                  verified=store.get(target.id) is None)


class ClearInput(BaseModel):
    kind: Literal["preference", "fact", "episodic"] | None = Field(None, description="Omit to clear everything")


class MemoryClear(Tool):
    name = "memory_clear"
    description = "Erase all long-term memories (or all of one kind). Requires confirmation."
    display_name = "Clearing memory"
    category = "memory"
    Input = ClearInput
    risk = RiskLevel.HIGH

    async def assess(self, args: ClearInput, ctx: ToolContext) -> RiskAssessment:
        count = len(ctx.services.memory.entries(args.kind, limit=100_000))
        scope = f"{args.kind} memories" if args.kind else "memories"
        return RiskAssessment(RiskLevel.HIGH, f"That will permanently erase {count} {scope}. Proceed?",
                              needs_confirmation=True)

    async def execute(self, args: ClearInput, ctx: ToolContext) -> ToolResult:
        removed = ctx.services.memory.clear(args.kind)
        await _announce(ctx, "cleared", None, None)
        return ToolResult.success(f"Erased {removed} memories", {"removed": removed})


TOOLS = [MemoryRemember, MemoryRecall, MemoryList, MemoryForget, MemoryClear]
