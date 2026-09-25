"""Tool definitions.

Every capability JARVIS has is a `Tool`: a named, described, schema-validated,
risk-classified unit of work with a timeout, a retry policy and an optional
verification step. The LLM can only act through tools — never through raw
shell strings.

Adding a tool:
    1. Subclass `Tool`, define `Input` (a pydantic model) and `execute()`.
    2. Add it to the `TOOLS` list of a module in `jarvis/tools/builtin/`.
That's all — the registry discovers it, the LLM receives its schema, and the
executor enforces permissions, confirmation, timeouts and logging.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

from jarvis.api.schemas import RiskLevel
from jarvis.core.cancellation import CancelToken

if TYPE_CHECKING:
    from jarvis.config import Settings
    from jarvis.core.services import Services
    from jarvis.platform.base import PlatformAdapter

MAX_RESULT_CHARS = 6000


class ToolError(Exception):
    """An expected, explainable failure. `message` is shown to the model and the user."""

    def __init__(self, message: str, kind: str = "error", retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class TransientToolError(ToolError):
    def __init__(self, message: str, kind: str = "transient") -> None:
        super().__init__(message, kind=kind, retryable=True)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_s: float = 0.5
    max_backoff_s: float = 4.0


@dataclass
class RiskAssessment:
    level: RiskLevel
    summary: str = ""
    details: list[str] = field(default_factory=list)
    needs_confirmation: bool = False


class ToolResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool
    summary: str
    data: Any = None
    verified: bool | None = None
    simulated: bool = False
    error: str | None = None
    error_kind: str | None = None

    @classmethod
    def success(cls, summary: str, data: Any = None, *, verified: bool | None = None,
                simulated: bool = False) -> ToolResult:
        return cls(ok=True, summary=summary, data=data, verified=verified, simulated=simulated)

    @classmethod
    def failure(cls, error: str, kind: str = "error", data: Any = None) -> ToolResult:
        return cls(ok=False, summary=error, error=error, error_kind=kind, data=data)

    def to_llm(self, max_chars: int = MAX_RESULT_CHARS) -> str:
        payload: dict[str, Any] = {"ok": self.ok, "summary": self.summary}
        if self.data is not None:
            payload["data"] = self.data
        if self.verified is not None:
            payload["verified"] = self.verified
        if self.simulated:
            payload["simulated"] = True
        if self.error:
            payload["error"] = self.error
        text = json.dumps(payload, default=str, ensure_ascii=False)
        if len(text) > max_chars:
            payload["data"] = json.dumps(self.data, default=str, ensure_ascii=False)[: max_chars - 400] + "…[truncated]"
            text = json.dumps(payload, default=str, ensure_ascii=False)
        return text


@dataclass
class ToolContext:
    services: Services
    session_id: str | None
    task_id: str | None = None
    cancel: CancelToken = field(default_factory=CancelToken)
    call_id: str = ""

    @property
    def settings(self) -> Settings:
        return self.services.settings

    @property
    def platform(self) -> PlatformAdapter:
        return self.services.platform

    @property
    def simulated(self) -> bool:
        return self.services.settings.mock

    async def progress(self, text: str) -> None:
        """Report a real intermediate step to the HUD activity stream."""
        await self.services.bus.activity_event(text, kind="tool", session_id=self.session_id, ref=self.task_id)


class NoInput(BaseModel):
    pass


class Tool:
    name: ClassVar[str]
    description: ClassVar[str]
    display_name: ClassVar[str]            # e.g. "Searching the web" — shown on the HUD
    category: ClassVar[str]
    Input: ClassVar[type[BaseModel]] = NoInput
    risk: ClassVar[RiskLevel] = RiskLevel.LOW
    timeout_s: ClassVar[float] = 30.0
    retry: ClassVar[RetryPolicy] = RetryPolicy()
    permission: ClassVar[str | None] = None   # web | vision | input | clipboard (see executor)
    data_boundary: ClassVar[str] = "local"    # local | remote — does data leave the machine?
    parallel_safe: ClassVar[bool] = False     # read-only and safe to run concurrently

    def available(self, services: Services) -> tuple[bool, str]:
        """Whether this tool can run here (OS support, optional deps, configuration)."""
        return True, ""

    async def assess(self, args: Any, ctx: ToolContext) -> RiskAssessment:
        """Classify the risk of this specific call. Override for context-dependent risk."""
        return RiskAssessment(level=self.risk)

    def validate(self, args: Any, ctx: ToolContext) -> None:
        """Extra semantic validation beyond the schema. Raise ToolError to reject."""

    async def execute(self, args: Any, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    async def verify(self, args: Any, result: ToolResult, ctx: ToolContext) -> bool | None:
        """Independently confirm the effect happened. None = not verifiable."""
        return None

    @classmethod
    def schema(cls) -> dict[str, Any]:
        params = cls.Input.model_json_schema()
        params.pop("title", None)
        for prop in params.get("properties", {}).values():
            prop.pop("title", None)
        params.setdefault("properties", {})
        return {
            "type": "function",
            "function": {"name": cls.name, "description": cls.description, "parameters": params},
        }
