"""Planner + agent executor for multi-step goals.

    PLAN   — the reasoning model produces a dependency graph of steps (validated JSON)
    ACT    — each ready step runs a bounded tool loop; independent steps run in parallel
    VERIFY — every step must report its outcome through the structured `report_step` tool
    UPDATE — progress, checkpoints and failures are recorded on the task
    REPORT — a concise, truthful summary of what was actually done
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from jarvis.api.schemas import TaskStatus, TaskStep
from jarvis.core.toolloop import ToolLoop
from jarvis.llm.base import LLMError, complete_structured
from jarvis.llm.models import ModelTier
from jarvis.llm.prompts import load_section
from jarvis.tools.base import ToolContext

if TYPE_CHECKING:
    from jarvis.core.services import Services
    from jarvis.tasks.manager import TaskHandle

log = logging.getLogger(__name__)

MAX_PARALLEL_STEPS = 2


class PlanStep(BaseModel):
    id: str = Field(description="short id like s1")
    description: str = Field(description="one concrete, checkable action")
    depends_on: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    goal: str
    steps: list[PlanStep] = Field(min_length=1, max_length=10)


REPORT_STEP_TOOL = {
    "type": "function",
    "function": {
        "name": "report_step",
        "description": "Call exactly once when this step is finished (or cannot be finished).",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["completed", "failed"]},
                "summary": {"type": "string", "description": "What was actually done / found, factually"},
                "verified": {"type": "boolean", "description": "Whether the effect was confirmed by a tool result"},
            },
            "required": ["status", "summary"],
        },
    },
}


class StepReport(BaseModel):
    status: Literal["completed", "failed"]
    summary: str
    verified: bool = False


PLANNER_PROMPT = """You are the planning module of J.A.R.V.I.S. Break the user's goal into 2-8 concrete steps
that can be carried out with these tools:
{tools}
Rules: each step is one checkable action; use depends_on for ordering (steps without dependencies may run in
parallel); include a verification step when changes are made (e.g. confirm files arrived); never plan to
delete data unless the goal explicitly asks; don't invent facts about the user's files — plan to inspect first."""


class AgentRunner:
    def __init__(self, services: Services) -> None:
        self._services = services

    async def run(self, handle: TaskHandle, goal: str, session_id: str | None, context: str = "") -> str:
        services = self._services
        tools = [t for t in services.registry.enabled(services) if t.category not in {"tasks", "input"}]
        tool_list = "\n".join(f"- {t.name}: {t.description.split('.')[0]}" for t in tools)

        await handle.step("Planning", 0.03)
        try:
            plan = await complete_structured(
                services.llm, services.models.model_for(ModelTier.REASONING),
                [{"role": "system", "content": PLANNER_PROMPT.format(tools=tool_list)},
                 {"role": "user", "content": f"Goal: {goal}\n{context}".strip()}],
                Plan,
            )
        except LLMError as exc:
            raise RuntimeError(f"I couldn't produce a plan: {exc.user_message()}") from exc
        steps = self._sanitize(plan)
        await handle.set_steps([TaskStep(id=s.id, description=s.description, depends_on=s.depends_on) for s in steps])

        results: dict[str, StepReport] = {}
        pending = {s.id: s for s in steps}
        semaphore = asyncio.Semaphore(MAX_PARALLEL_STEPS)

        async def execute(step: PlanStep) -> None:
            async with semaphore:
                await handle.update_step(step.id, TaskStatus.RUNNING)
                await handle.step(step.description)
                report = await self._run_step(handle, goal, step, results, tools, session_id)
                if report.status == "failed":
                    # One retry with the failure as context — then give up honestly.
                    await handle.step(f"Retrying: {step.description}")
                    report = await self._run_step(handle, goal, step, results, tools, session_id,
                                                  previous_failure=report.summary)
                results[step.id] = report
                await handle.update_step(
                    step.id,
                    TaskStatus.COMPLETED if report.status == "completed" else TaskStatus.FAILED,
                    result=report.summary if report.status == "completed" else None,
                    error=report.summary if report.status == "failed" else None,
                )

        while pending:
            handle.cancel.raise_if_cancelled()
            failed = {sid for sid, r in results.items() if r.status == "failed"}
            blocked = [s for s in pending.values() if set(s.depends_on) & failed]
            for step in blocked:
                results[step.id] = StepReport(status="failed", summary="skipped — a step it depends on failed")
                await handle.update_step(step.id, TaskStatus.CANCELLED, error="skipped: dependency failed")
                pending.pop(step.id)
            ready = [s for s in pending.values() if all(d in results for d in s.depends_on)]
            if not ready:
                break
            for step in ready:
                pending.pop(step.id)
            await asyncio.gather(*(execute(s) for s in ready))

        await handle.step("Summarising results", 0.97)
        return await self._final_report(goal, steps, results)

    @staticmethod
    def _sanitize(plan: Plan) -> list[PlanStep]:
        ids = {s.id for s in plan.steps}
        ordered: list[PlanStep] = []
        for step in plan.steps:
            # Drop unknown / forward-cyclic dependencies: a dependency must refer to an earlier step.
            earlier = {s.id for s in ordered}
            step.depends_on = [d for d in step.depends_on if d in ids and d in earlier]
            ordered.append(step)
        return ordered

    async def _run_step(self, handle: TaskHandle, goal: str, step: PlanStep, results: dict[str, StepReport],
                        tools: list, session_id: str | None, previous_failure: str | None = None) -> StepReport:
        services = self._services
        prior = "\n".join(f"- {sid}: {r.status}: {r.summary}" for sid, r in results.items()) or "none yet"
        system = "\n\n".join([
            load_section("identity"), load_section("safety"),
            f"You are executing ONE step of a background plan.\nOverall goal: {goal}\n"
            f"Current step ({step.id}): {step.description}\nResults of earlier steps:\n{prior}\n"
            "Use tools to do this step and verify its effect. Then call report_step exactly once. "
            "Never claim success without tool evidence.",
        ])
        user = "Carry out the current step."
        if previous_failure:
            user += f" The previous attempt failed: {previous_failure}. Try a different approach if one exists."
        ctx = ToolContext(services=services, session_id=session_id, task_id=handle.id, cancel=handle.cancel.child())
        try:
            outcome = await ToolLoop(services).run(
                model=services.models.model_for(ModelTier.GENERAL),
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                tools=tools,
                ctx=ctx,
                max_steps=min(8, services.settings.tasks.max_agent_steps),
                max_tool_calls=services.settings.tasks.max_tool_calls_per_turn,
                finish_tool=REPORT_STEP_TOOL,
            )
        except LLMError as exc:
            return StepReport(status="failed", summary=exc.user_message())
        if outcome.finish_args is not None:
            try:
                report = StepReport.model_validate(outcome.finish_args)
            except ValueError:
                report = StepReport(status="failed", summary="the step reported an invalid result")
            # A "completed" claim with no successful tool evidence is not trusted.
            evidence = outcome.results[:-1]  # the last result is report_step itself
            if report.status == "completed" and evidence and not any(r.ok for _, r in evidence):
                report = StepReport(status="failed", summary=f"claimed success but every tool call failed: {report.summary}")
            return report
        return StepReport(status="failed", summary=outcome.text[:400] or "the step ended without reporting a result")

    async def _final_report(self, goal: str, steps: list[PlanStep], results: dict[str, StepReport]) -> str:
        lines = []
        for step in steps:
            report = results.get(step.id)
            mark = "✓" if report and report.status == "completed" else "✗"
            lines.append(f"{mark} {step.description} — {report.summary if report else 'not run'}")
        done = sum(1 for r in results.values() if r.status == "completed")
        header = f"{done} of {len(steps)} steps completed for: {goal}"
        try:
            summary = await self._services.llm.complete(
                self._services.models.model_for(ModelTier.FAST),
                [{"role": "system", "content": "Summarise this task outcome for the user in 2-3 truthful sentences. "
                                               "Mention failures plainly."},
                 {"role": "user", "content": header + "\n" + "\n".join(lines)}],
                max_tokens=300,
            )
        except LLMError:
            summary = header + "."
        return summary.strip() + "\n\n" + header + "\n" + "\n".join(lines)
