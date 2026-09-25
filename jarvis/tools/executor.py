"""The controlled path from a model's tool call to a real action.

    parse → lookup → permission → validate → assess risk → confirm (if needed)
    → execute (timeout + retry) → verify → log/audit → events → result
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from jarvis.api.schemas import RiskLevel, ToolCompletedEvent, ToolFailedEvent, ToolStartedEvent
from jarvis.core.cancellation import Cancelled
from jarvis.core.metrics import Stopwatch
from jarvis.llm.base import ToolCall
from jarvis.logging_setup import redactor
from jarvis.platform.base import NotSupported, PlatformError
from jarvis.tools.base import RiskAssessment, Tool, ToolContext, ToolError, ToolResult

if TYPE_CHECKING:
    from jarvis.core.services import Services

log = logging.getLogger(__name__)


def _summarize_args(args: dict[str, Any]) -> str:
    return redactor.redact(json.dumps(args, default=str, ensure_ascii=False)[:500])


class ToolExecutor:
    def __init__(self, services: Services, audit_path: Path) -> None:
        self._services = services
        self._audit_path = audit_path
        audit_path.parent.mkdir(parents=True, exist_ok=True)

    async def execute_calls(self, calls: list[ToolCall], ctx: ToolContext) -> list[ToolResult]:
        """Run calls in order; consecutive read-only calls run concurrently."""
        results: list[ToolResult] = []
        batch: list[ToolCall] = []

        async def flush() -> None:
            if batch:
                results.extend(await asyncio.gather(*(self.execute(c, ctx) for c in batch)))
                batch.clear()

        for call in calls:
            tool = self._services.registry.get(call.name)
            if tool is not None and tool.parallel_safe:
                batch.append(call)
                continue
            await flush()
            results.append(await self.execute(call, ctx))
        await flush()
        return results

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        services = self._services
        tool = services.registry.get(call.name)
        if tool is None:
            known = ", ".join(sorted(t.name for t in services.registry.enabled(services)))
            return ToolResult.failure(f"Unknown tool '{call.name}'. Available tools: {known}", kind="unknown_tool")

        enabled, reason = services.registry.is_enabled(tool, services)
        if not enabled:
            return ToolResult.failure(f"{tool.name} is unavailable: {reason}", kind="permission_denied")

        raw_args, parse_error = call.parsed()
        if parse_error or raw_args is None:
            return ToolResult.failure(f"Invalid arguments for {tool.name}: {parse_error}", kind="invalid_arguments")
        try:
            args = tool.Input.model_validate(raw_args)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
            return ToolResult.failure(f"Invalid arguments for {tool.name}: {problems}", kind="invalid_arguments")

        call_ctx = ToolContext(
            services=services, session_id=ctx.session_id, task_id=ctx.task_id, cancel=ctx.cancel,
            call_id=call.id or uuid.uuid4().hex[:10],
        )
        try:
            tool.validate(args, call_ctx)
            assessment = await tool.assess(args, call_ctx)
        except ToolError as exc:
            await self._audit(tool, raw_args, RiskLevel(tool.risk), None, False, str(exc), call_ctx)
            return ToolResult.failure(str(exc), kind=exc.kind)

        confirmed: bool | None = None
        if self._needs_confirmation(tool, assessment):
            confirmed = await services.confirmations.request(
                call_ctx.session_id,
                tool.name,
                assessment.level,
                assessment.summary or f"Run {tool.display_name.lower()}?",
                assessment.details,
                task_id=call_ctx.task_id,
            )
            if not confirmed:
                await self._audit(tool, raw_args, assessment.level, False, False, "declined", call_ctx)
                return ToolResult.failure(
                    "The user did not approve this action, so it was not performed.", kind="declined"
                )

        await services.bus.publish(
            ToolStartedEvent(session_id=call_ctx.session_id, call_id=call_ctx.call_id, tool=tool.name,
                             display_name=tool.display_name, task_id=call_ctx.task_id)
        )
        watch = Stopwatch()
        result = await self._run_with_policy(tool, args, call_ctx)
        if result.ok:
            try:
                verified = await asyncio.wait_for(tool.verify(args, result, call_ctx), timeout=15)
            except (TimeoutError, ToolError, PlatformError) as exc:
                log.warning("verification for %s failed: %s", tool.name, exc)
                verified = False
            if verified is not None:
                result.verified = verified
        if services.settings.mock and tool.risk is not RiskLevel.LOW:
            result.simulated = result.simulated or services.platform.simulated
        duration = watch.ms
        services.metrics.record_tool(tool.name, duration, result.ok)
        log.info(
            "tool %s %s in %.0fms",
            tool.name,
            "ok" if result.ok else f"failed ({result.error_kind})",
            duration,
            extra={"tool": tool.name, "duration_ms": round(duration, 1), "success": result.ok,
                   "error": result.error, "tool_args": _summarize_args(raw_args)},
        )
        if result.ok:
            await services.bus.publish(
                ToolCompletedEvent(session_id=call_ctx.session_id, call_id=call_ctx.call_id, tool=tool.name,
                                   display_name=tool.display_name, duration_ms=round(duration, 1),
                                   summary=result.summary[:200], verified=result.verified,
                                   simulated=result.simulated, task_id=call_ctx.task_id)
            )
        else:
            await services.bus.publish(
                ToolFailedEvent(session_id=call_ctx.session_id, call_id=call_ctx.call_id, tool=tool.name,
                                display_name=tool.display_name, duration_ms=round(duration, 1),
                                error=(result.error or "failed")[:300], error_kind=result.error_kind or "error",
                                task_id=call_ctx.task_id)
            )
        if assessment.level is not RiskLevel.LOW:
            await self._audit(tool, raw_args, assessment.level, confirmed, result.ok, result.error, call_ctx)
        return result

    def _needs_confirmation(self, tool: Tool, assessment: RiskAssessment) -> bool:
        # HIGH risk ALWAYS requires explicit confirmation — no tool or setting can opt out.
        if assessment.level is RiskLevel.HIGH or tool.risk is RiskLevel.HIGH:
            return True
        if assessment.level is RiskLevel.MEDIUM:
            return assessment.needs_confirmation and self._services.settings.permissions.confirm_medium_risk
        return False

    async def _run_with_policy(self, tool: Tool, args: Any, ctx: ToolContext) -> ToolResult:
        policy = tool.retry
        attempt = 0
        while True:
            attempt += 1
            try:
                ctx.cancel.raise_if_cancelled()
                return await asyncio.wait_for(tool.execute(args, ctx), timeout=tool.timeout_s)
            except TimeoutError:
                error: ToolResult = ToolResult.failure(
                    f"{tool.display_name} timed out after {tool.timeout_s:g}s", kind="timeout")
                retryable = policy.max_attempts > 1
            except Cancelled:
                return ToolResult.failure("Cancelled before completion.", kind="cancelled")
            except NotSupported as exc:
                return ToolResult.failure(str(exc), kind="not_supported")
            except PermissionError as exc:
                return ToolResult.failure(f"Permission denied by the operating system: {exc}", kind="permission_denied")
            except ToolError as exc:
                error, retryable = ToolResult.failure(str(exc), kind=exc.kind), exc.retryable
            except PlatformError as exc:
                error, retryable = ToolResult.failure(str(exc), kind="platform_error"), False
            except FileNotFoundError as exc:
                return ToolResult.failure(f"Not found: {exc.filename or exc}", kind="not_found")
            except OSError as exc:
                error, retryable = ToolResult.failure(f"System error: {exc}", kind="os_error"), False
            except Exception as exc:  # a bug in a tool must not take down the assistant
                log.exception("tool %s crashed", tool.name)
                return ToolResult.failure(f"Internal error in {tool.name}: {type(exc).__name__}", kind="internal")
            if not retryable or attempt >= policy.max_attempts:
                return error
            delay = min(policy.max_backoff_s, policy.backoff_s * (2 ** (attempt - 1)))
            log.info("retrying %s in %.1fs (attempt %d/%d)", tool.name, delay, attempt + 1, policy.max_attempts)
            await asyncio.sleep(delay)

    async def _audit(self, tool: Tool, args: dict[str, Any], risk: RiskLevel, confirmed: bool | None,
                     ok: bool, error: str | None, ctx: ToolContext) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "session_id": ctx.session_id,
            "task_id": ctx.task_id,
            "tool": tool.name,
            "risk": risk.value,
            "args": _summarize_args(args),
            "confirmed": confirmed,
            "success": ok,
            "error": error,
            "simulated": self._services.settings.mock,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        await asyncio.to_thread(self._append_audit, line)

    def _append_audit(self, line: str) -> None:
        with self._audit_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
