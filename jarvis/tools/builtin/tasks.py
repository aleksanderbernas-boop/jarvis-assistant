"""Background tasks and reminders."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from jarvis.core.timeparse import humanize_time, parse_when
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolResult


def _parse_time(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError:
        pass
    found = parse_when(text)
    if not found:
        raise ToolError(f"I couldn't understand the time '{text}'. Use e.g. 'in 30 minutes', 'at 20:00' "
                        "or an ISO timestamp.", kind="invalid_arguments")
    return found.when


class TaskStartInput(BaseModel):
    kind: Literal["research", "agent"] = Field(description="research = web research report; agent = multi-step "
                                                           "work on the computer (files, apps)")
    description: str = Field(min_length=3, max_length=1000, description="The full goal, with all constraints")
    continue_previous: bool = Field(False, description="Continue earlier research on this topic instead of restarting")
    deadline: str | None = Field(None, description="Optional deadline, e.g. 'at 20:00' or ISO time")


class TaskStart(Tool):
    name = "task_start"
    description = ("Start long-running work in the background (in-depth research, organising many files, "
                   "multi-step jobs). The conversation continues while it runs; the user is notified when done.")
    display_name = "Starting background task"
    category = "tasks"
    Input = TaskStartInput

    async def execute(self, args: TaskStartInput, ctx: ToolContext) -> ToolResult:
        if ctx.task_id:
            raise ToolError("background tasks can't start further background tasks", kind="refused")
        deadline = _parse_time(args.deadline) if args.deadline else None
        launcher = ctx.services.launcher
        if args.kind == "research":
            task = await launcher.start_research(args.description, session_id=ctx.session_id, deadline=deadline,
                                                 continue_previous=args.continue_previous)
        else:
            task = await launcher.start_agent(args.description, session_id=ctx.session_id, deadline=deadline)
        return ToolResult.success(f"Started background task {task.id}: {task.description}",
                                  {"task_id": task.id, "status": task.status.value})


class TaskList(Tool):
    name = "task_list"
    description = "List active and recent background tasks with progress and results."
    display_name = "Checking tasks"
    category = "tasks"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        tasks = ctx.services.tasks
        active = tasks.active()
        recent = [t for t in tasks.recent(10) if t.status.is_terminal]

        def row(t):  # type: ignore[no-untyped-def]
            return {"id": t.id, "description": t.description, "status": t.status.value,
                    "progress": f"{t.progress:.0%}", "current_step": t.current_step,
                    "eta_seconds": t.eta_seconds, "error": t.error,
                    "result_preview": (t.result or "")[:600] or None}

        return ToolResult.success(f"{len(active)} active task(s)",
                                  {"active": [row(t) for t in active], "recent": [row(t) for t in recent]})


class TaskCancelInput(BaseModel):
    task: str = Field(description="Task id, or words from its description ('the laptop research'), or 'all'")


class TaskCancel(Tool):
    name = "task_cancel"
    description = "Cancel a running background task."
    display_name = "Cancelling task"
    category = "tasks"
    Input = TaskCancelInput

    async def execute(self, args: TaskCancelInput, ctx: ToolContext) -> ToolResult:
        tasks = ctx.services.tasks
        matches = tasks.active() if args.task.strip().lower() == "all" else tasks.find_active(args.task)
        if not matches:
            raise ToolError("no active task matches that", kind="not_found")
        if len(matches) > 1 and args.task.strip().lower() != "all":
            return ToolResult.failure("several tasks match — which one?", kind="ambiguous",
                                      data={"candidates": [{"id": t.id, "description": t.description} for t in matches]})
        for task in matches:
            tasks.cancel(task.id)
        return ToolResult.success(f"Cancelled {len(matches)} task(s): " + "; ".join(t.description for t in matches),
                                  {"cancelled": [t.id for t in matches]})


class ReminderInput(BaseModel):
    text: str = Field(min_length=1, max_length=500, description="What to remind the user about")
    when: str = Field(description="'in 30 minutes', 'at 20:00', 'tomorrow morning', or an ISO timestamp")
    repeat: Literal["none", "daily", "weekdays", "weekly"] = Field(
        "none", description="Only if the user explicitly asked for a recurring reminder")


class ReminderCreate(Tool):
    name = "reminder_create"
    description = "Set a reminder or timer that JARVIS will announce at the given time."
    display_name = "Setting reminder"
    category = "reminders"
    Input = ReminderInput

    async def execute(self, args: ReminderInput, ctx: ToolContext) -> ToolResult:
        due = _parse_time(args.when)
        if due <= datetime.now().astimezone():
            raise ToolError("that time is in the past", kind="invalid_arguments")
        reminder = ctx.services.reminders.create(args.text, due, session_id=ctx.session_id, repeat=args.repeat)
        repeat = "" if args.repeat == "none" else f", repeating {args.repeat}"
        return ToolResult.success(f"Reminder set {humanize_time(due)}{repeat}: {reminder.text}", reminder.to_dict(),
                                  verified=True)


class ReminderList(Tool):
    name = "reminder_list"
    description = "List pending reminders and timers."
    display_name = "Checking reminders"
    category = "reminders"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        pending = ctx.services.reminders.pending()
        return ToolResult.success(f"{len(pending)} pending reminder(s)", {"reminders": [r.to_dict() for r in pending]})


class ReminderCancelInput(BaseModel):
    reminder_id: str


class ReminderCancel(Tool):
    name = "reminder_cancel"
    description = "Cancel a pending reminder by id (use reminder_list first)."
    display_name = "Cancelling reminder"
    category = "reminders"
    Input = ReminderCancelInput

    async def execute(self, args: ReminderCancelInput, ctx: ToolContext) -> ToolResult:
        if not ctx.services.reminders.cancel(args.reminder_id):
            raise ToolError("no pending reminder with that id", kind="not_found")
        return ToolResult.success("Reminder cancelled", verified=True)


TOOLS = [TaskStart, TaskList, TaskCancel, ReminderCreate, ReminderList, ReminderCancel]
