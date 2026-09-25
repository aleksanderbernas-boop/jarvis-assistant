"""Background task manager.

Long-running work (research, multi-step agent jobs) runs here so the
conversation stays responsive. A semaphore bounds concurrency; every task has
progress, steps, an optional deadline, cancellation and a persisted record.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from jarvis.api.schemas import NotificationEvent, TaskEvent, TaskSnapshot, TaskStatus, TaskStep, utcnow
from jarvis.core.cancellation import Cancelled, CancelToken
from jarvis.core.events import EventBus
from jarvis.core.metrics import Metrics
from jarvis.logging_setup import session_id_var, task_id_var
from jarvis.storage.db import Database

log = logging.getLogger(__name__)

TaskRunner = Callable[["TaskHandle"], Awaitable[str | None]]
FinishedHook = Callable[[TaskSnapshot], Awaitable[None]]


class TaskHandle:
    """What a running task uses to report progress and observe cancellation."""

    def __init__(self, manager: TaskManager, snapshot: TaskSnapshot, cancel: CancelToken) -> None:
        self._manager = manager
        self.snapshot = snapshot
        self.cancel = cancel

    @property
    def id(self) -> str:
        return self.snapshot.id

    async def step(self, description: str, progress: float | None = None) -> None:
        self.cancel.raise_if_cancelled()
        self.snapshot.current_step = description
        if progress is not None:
            self.snapshot.progress = max(self.snapshot.progress, min(1.0, progress))
        await self._manager._emit("task_progress", self.snapshot)
        await self._manager._bus.activity_event(description, kind="task", session_id=self.snapshot.session_id,
                                                ref=self.id)

    async def set_steps(self, steps: list[TaskStep]) -> None:
        self.snapshot.steps = steps
        await self._manager._emit("task_progress", self.snapshot)

    async def update_step(self, step_id: str, status: TaskStatus, result: str | None = None,
                          error: str | None = None) -> None:
        for step in self.snapshot.steps:
            if step.id == step_id:
                step.status, step.result, step.error = status, result, error
        done = sum(1 for s in self.snapshot.steps if s.status.is_terminal)
        if self.snapshot.steps:
            self.snapshot.progress = max(self.snapshot.progress, done / len(self.snapshot.steps) * 0.95)
        await self._manager._emit("task_progress", self.snapshot)


class TaskManager:
    def __init__(self, bus: EventBus, db: Database, metrics: Metrics, max_concurrent: int = 3) -> None:
        self._bus = bus
        self._db = db
        self._metrics = metrics
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, TaskSnapshot] = {}
        self._running: dict[str, tuple[asyncio.Task[None], CancelToken]] = {}
        self._deadline_warned: set[str] = set()
        self._monitor: asyncio.Task[None] | None = None
        self.on_finished: FinishedHook | None = None

    # ── Lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        self._monitor = asyncio.create_task(self._deadline_loop(), name="task-deadline-monitor")
        # Tasks that were running when the app stopped can't resume; record that honestly.
        for row in self._db.query("SELECT snapshot FROM tasks WHERE status IN ('queued','running','waiting')"):
            snapshot = TaskSnapshot.model_validate_json(row["snapshot"])
            snapshot.status = TaskStatus.FAILED
            snapshot.error = "Interrupted because JARVIS was restarted."
            snapshot.finished_at = utcnow()
            self._persist(snapshot)

    async def shutdown(self) -> None:
        if self._monitor:
            self._monitor.cancel()
        for task_id in list(self._running):
            self.cancel(task_id, "shutdown")
        pending = [t for t, _ in self._running.values()]
        if pending:
            await asyncio.wait(pending, timeout=3)

    # ── Public API ──────────────────────────────────────────────────────────
    async def create(self, kind: str, description: str, runner: TaskRunner, *, session_id: str | None,
                     deadline: datetime | None = None) -> TaskSnapshot:
        snapshot = TaskSnapshot(
            id=uuid.uuid4().hex[:8],
            kind=kind,
            description=description,
            status=TaskStatus.QUEUED,
            created_at=utcnow(),
            deadline=deadline,
            session_id=session_id,
        )
        self._tasks[snapshot.id] = snapshot
        cancel = CancelToken()
        task = asyncio.create_task(self._run(snapshot, runner, cancel), name=f"task-{snapshot.id}")
        self._running[snapshot.id] = (task, cancel)
        await self._emit("task_created", snapshot)
        await self._bus.activity_event(f"Task started: {description}", kind="task", session_id=session_id,
                                       ref=snapshot.id)
        return snapshot

    def cancel(self, task_id: str, reason: str = "cancelled by user") -> bool:
        entry = self._running.get(task_id)
        if not entry:
            return False
        task, token = entry
        token.cancel(reason)
        task.cancel()
        return True

    def get(self, task_id: str) -> TaskSnapshot | None:
        if task_id in self._tasks:
            return self._tasks[task_id]
        row = self._db.query_one("SELECT snapshot FROM tasks WHERE id = ?", (task_id,))
        return TaskSnapshot.model_validate_json(row["snapshot"]) if row else None

    def active(self, session_id: str | None = None) -> list[TaskSnapshot]:
        return [t for t in self._tasks.values() if not t.status.is_terminal
                and (session_id is None or t.session_id in (None, session_id))]

    def recent(self, limit: int = 20) -> list[TaskSnapshot]:
        rows = self._db.query("SELECT snapshot FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        return [TaskSnapshot.model_validate_json(r["snapshot"]) for r in rows]

    def find_active(self, query: str) -> list[TaskSnapshot]:
        """Match active tasks by id or description words ("the laptop research")."""
        words = {w for w in query.lower().split() if len(w) > 2} - {"the", "that", "task", "this", "all"}
        active = self.active()
        if not words:
            return active
        return [t for t in active if t.id in query or words & set(f"{t.description} {t.kind}".lower().split())]

    def mark_waiting(self, task_id: str | None, waiting: bool) -> None:
        snapshot = self._tasks.get(task_id or "")
        if snapshot and not snapshot.status.is_terminal:
            snapshot.status = TaskStatus.WAITING if waiting else TaskStatus.RUNNING
            asyncio.get_running_loop().create_task(self._emit("task_progress", snapshot))

    # ── Internals ───────────────────────────────────────────────────────────
    async def _run(self, snapshot: TaskSnapshot, runner: TaskRunner, cancel: CancelToken) -> None:
        task_id_var.set(snapshot.id)
        session_id_var.set(snapshot.session_id)
        event = "task_failed"
        try:
            async with self._semaphore:
                snapshot.status = TaskStatus.RUNNING
                snapshot.started_at = utcnow()
                await self._emit("task_progress", snapshot)
                result = await runner(TaskHandle(self, snapshot, cancel))
            snapshot.status, snapshot.result, snapshot.progress = TaskStatus.COMPLETED, result, 1.0
            snapshot.current_step = None
            event = "task_completed"
        except (asyncio.CancelledError, Cancelled):
            snapshot.status = TaskStatus.CANCELLED
            snapshot.error = cancel.reason or "cancelled"
            event = "task_cancelled"
        except Exception as exc:
            log.exception("task %s failed", snapshot.id)
            snapshot.status = TaskStatus.FAILED
            snapshot.error = str(exc) or type(exc).__name__
        finally:
            snapshot.finished_at = utcnow()
            snapshot.eta_seconds = None
            self._running.pop(snapshot.id, None)
            self._metrics.incr(event)
            await self._emit(event, snapshot)
            if self.on_finished:
                with contextlib.suppress(Exception):
                    await self.on_finished(snapshot)

    def _update_eta(self, snapshot: TaskSnapshot) -> None:
        if snapshot.started_at and 0.05 < snapshot.progress < 1:
            elapsed = (utcnow() - snapshot.started_at).total_seconds()
            snapshot.eta_seconds = round(elapsed / snapshot.progress * (1 - snapshot.progress), 1)

    async def _emit(self, kind: str, snapshot: TaskSnapshot) -> None:
        self._update_eta(snapshot)
        self._persist(snapshot)
        await self._bus.publish(TaskEvent(type=kind, task=snapshot.model_copy(deep=True),  # type: ignore[arg-type]
                                          session_id=snapshot.session_id))

    def _persist(self, snapshot: TaskSnapshot) -> None:
        now = utcnow().isoformat()
        self._db.execute(
            "INSERT INTO tasks(id, session_id, kind, description, status, snapshot, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status = excluded.status, "
            "snapshot = excluded.snapshot, updated_at = excluded.updated_at",
            (snapshot.id, snapshot.session_id, snapshot.kind, snapshot.description, snapshot.status.value,
             snapshot.model_dump_json(), snapshot.created_at.isoformat(), now),
        )

    async def _deadline_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await self.check_deadlines()
            except Exception:
                log.exception("deadline check failed")

    async def check_deadlines(self) -> None:
        now = utcnow()
        for snapshot in self.active():
            if not snapshot.deadline or snapshot.id in self._deadline_warned:
                continue
            self._update_eta(snapshot)
            remaining = (snapshot.deadline - now).total_seconds()
            overdue = remaining <= 0
            at_risk = snapshot.eta_seconds is not None and snapshot.eta_seconds > remaining
            if overdue or at_risk:
                self._deadline_warned.add(snapshot.id)
                local = snapshot.deadline.astimezone().strftime("%H:%M")
                message = (
                    f"'{snapshot.description}' has passed its {local} deadline and is "
                    f"{snapshot.progress:.0%} complete." if overdue else
                    f"'{snapshot.description}' may not finish by {local}: it is {snapshot.progress:.0%} complete, "
                    f"roughly {timedelta(seconds=int(snapshot.eta_seconds or 0))} remaining."
                )
                await self._bus.publish(NotificationEvent(session_id=snapshot.session_id, level="warning",
                                                          title="Deadline at risk", message=message, speak=True))
