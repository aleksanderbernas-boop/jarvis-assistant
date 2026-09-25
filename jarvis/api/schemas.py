"""Strongly-typed contract between the backend and the HUD.

Server → client: `Event` (discriminated on `type`).
Client → server: `ClientMessage` (discriminated on `type`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter


def utcnow() -> datetime:
    return datetime.now(UTC)


class AssistantState(StrEnum):
    STANDBY = "STANDBY"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    BACKGROUND_TASK = "BACKGROUND_TASK"
    ERROR = "ERROR"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ResponseMode(StrEnum):
    QUICK = "quick"
    CONVERSATIONAL = "conversational"
    TECHNICAL = "technical"
    OPERATIONAL = "operational"
    ALERT = "alert"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


# ── Shared models ────────────────────────────────────────────────────────────


class TaskStep(BaseModel):
    id: str
    description: str
    status: TaskStatus = TaskStatus.QUEUED
    depends_on: list[str] = Field(default_factory=list)
    result: str | None = None
    error: str | None = None


class TaskSnapshot(BaseModel):
    id: str
    kind: str
    description: str
    status: TaskStatus
    progress: float = 0.0
    current_step: str | None = None
    steps: list[TaskStep] = Field(default_factory=list)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    deadline: datetime | None = None
    eta_seconds: float | None = None
    error: str | None = None
    result: str | None = None
    session_id: str | None = None


class GpuMetrics(BaseModel):
    name: str
    utilization_percent: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    temperature_c: float | None = None


class SystemMetrics(BaseModel):
    cpu_percent: float
    ram_percent: float
    ram_used_gb: float
    ram_total_gb: float
    disk_percent: float
    disk_used_gb: float
    disk_total_gb: float
    gpu: GpuMetrics | None = None
    battery_percent: float | None = None
    battery_plugged: bool | None = None
    net_up_kbps: float = 0.0
    net_down_kbps: float = 0.0
    online: bool = True
    uptime_s: float = 0.0
    process_count: int = 0


# ── Events (server → client) ─────────────────────────────────────────────────


class _Event(BaseModel):
    timestamp: datetime = Field(default_factory=utcnow)
    session_id: str | None = None


class AssistantStateEvent(_Event):
    type: Literal["assistant_state"] = "assistant_state"
    state: AssistantState
    previous: AssistantState | None = None
    reason: str = ""


class TranscriptionEvent(_Event):
    type: Literal["transcription"] = "transcription"
    text: str
    final: bool = True
    wake_detected: bool = False
    ignored: bool = False
    stt_ms: float | None = None


class UserMessageEvent(_Event):
    type: Literal["user_message"] = "user_message"
    message_id: str
    text: str
    source: Literal["text", "voice"] = "text"


class ResponseStartEvent(_Event):
    type: Literal["response_start"] = "response_start"
    response_id: str
    mode: ResponseMode = ResponseMode.CONVERSATIONAL


class ResponseDeltaEvent(_Event):
    type: Literal["response_delta"] = "response_delta"
    response_id: str
    delta: str


class ResponseEndEvent(_Event):
    type: Literal["response_end"] = "response_end"
    response_id: str
    text: str
    interrupted: bool = False


class SpeakEvent(_Event):
    type: Literal["speak"] = "speak"
    response_id: str
    seq: int
    text: str


class SpeechStopEvent(_Event):
    type: Literal["speech_stop"] = "speech_stop"
    reason: str = "interrupted"


class ToolStartedEvent(_Event):
    type: Literal["tool_started"] = "tool_started"
    call_id: str
    tool: str
    display_name: str
    task_id: str | None = None


class ToolCompletedEvent(_Event):
    type: Literal["tool_completed"] = "tool_completed"
    call_id: str
    tool: str
    display_name: str
    duration_ms: float
    summary: str = ""
    verified: bool | None = None
    simulated: bool = False
    task_id: str | None = None


class ToolFailedEvent(_Event):
    type: Literal["tool_failed"] = "tool_failed"
    call_id: str
    tool: str
    display_name: str
    duration_ms: float
    error: str
    error_kind: str = "error"
    task_id: str | None = None


class TaskEvent(_Event):
    type: Literal["task_created", "task_progress", "task_completed", "task_failed", "task_cancelled"]
    task: TaskSnapshot


class MemoryUpdatedEvent(_Event):
    type: Literal["memory_updated"] = "memory_updated"
    action: Literal["added", "updated", "deleted", "cleared"]
    memory_id: str | None = None
    content: str | None = None
    total: int = 0


class SystemMetricsEvent(_Event):
    type: Literal["system_metrics"] = "system_metrics"
    metrics: SystemMetrics


class NotificationEvent(_Event):
    type: Literal["notification"] = "notification"
    level: Literal["info", "success", "warning", "error"] = "info"
    title: str
    message: str
    speak: bool = False


class ErrorEvent(_Event):
    type: Literal["error"] = "error"
    message: str
    code: str = "error"
    recoverable: bool = True


class ConfirmationRequestEvent(_Event):
    type: Literal["confirmation_request"] = "confirmation_request"
    confirmation_id: str
    tool: str
    risk: RiskLevel
    summary: str
    details: list[str] = Field(default_factory=list)
    expires_at: datetime


class ConfirmationResolvedEvent(_Event):
    type: Literal["confirmation_resolved"] = "confirmation_resolved"
    confirmation_id: str
    approved: bool
    reason: str = ""


class ActivityEvent(_Event):
    type: Literal["activity"] = "activity"
    text: str
    kind: Literal["state", "tool", "task", "memory", "system", "error", "info"] = "info"
    ref: str | None = None


class BootStage(BaseModel):
    key: str
    label: str
    status: Literal["pending", "ok", "warning", "failed"] = "pending"
    detail: str = ""


class BootEvent(_Event):
    type: Literal["boot_progress"] = "boot_progress"
    stages: list[BootStage]
    complete: bool = False


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    timestamp: datetime


class SessionSnapshotEvent(_Event):
    type: Literal["session_snapshot"] = "session_snapshot"
    state: AssistantState
    history: list[HistoryItem]
    tasks: list[TaskSnapshot]
    activity: list[ActivityEvent]
    pending_confirmation: ConfirmationRequestEvent | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    version: str = ""


Event = Annotated[
    AssistantStateEvent
    | TranscriptionEvent
    | UserMessageEvent
    | ResponseStartEvent
    | ResponseDeltaEvent
    | ResponseEndEvent
    | SpeakEvent
    | SpeechStopEvent
    | ToolStartedEvent
    | ToolCompletedEvent
    | ToolFailedEvent
    | TaskEvent
    | MemoryUpdatedEvent
    | SystemMetricsEvent
    | NotificationEvent
    | ErrorEvent
    | ConfirmationRequestEvent
    | ConfirmationResolvedEvent
    | ActivityEvent
    | BootEvent
    | SessionSnapshotEvent,
    Field(discriminator="type"),
]
EventAdapter: TypeAdapter[Event] = TypeAdapter(Event)


# ── Client messages (client → server) ────────────────────────────────────────


class HelloMessage(BaseModel):
    type: Literal["hello"] = "hello"
    session_id: str | None = None
    voice_output: bool = True
    mic_active: bool = False


class UserTextMessage(BaseModel):
    type: Literal["user_text"] = "user_text"
    text: str = Field(min_length=1, max_length=8000)
    source: Literal["text", "voice"] = "text"


class InterruptMessage(BaseModel):
    type: Literal["interrupt"] = "interrupt"
    reason: str = "barge_in"


class ConfirmMessage(BaseModel):
    type: Literal["confirm"] = "confirm"
    confirmation_id: str
    approved: bool


class CancelTaskMessage(BaseModel):
    type: Literal["cancel_task"] = "cancel_task"
    task_id: str


class TtsStateMessage(BaseModel):
    type: Literal["tts_state"] = "tts_state"
    playing: bool
    first_audio_ms: float | None = None


class ClientSettingsMessage(BaseModel):
    type: Literal["client_settings"] = "client_settings"
    voice_output: bool | None = None
    mic_active: bool | None = None
    mic_error: str | None = None


class PingMessage(BaseModel):
    type: Literal["ping"] = "ping"


ClientMessage = Annotated[
    HelloMessage
    | UserTextMessage
    | InterruptMessage
    | ConfirmMessage
    | CancelTaskMessage
    | TtsStateMessage
    | ClientSettingsMessage
    | PingMessage,
    Field(discriminator="type"),
]
ClientMessageAdapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
