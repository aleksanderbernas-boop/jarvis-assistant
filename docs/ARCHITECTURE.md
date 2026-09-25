# Architecture

## Starting point (repository audit)

The repository started empty. The only existing assets were the HUD design (the screenshot of the
J.A.R.V.I.S. interface) and a Groq API key. So this implementation:

- builds the HUD to match that design, feeding every panel from real backend data;
- uses Groq for reasoning (OpenAI-compatible chat with tool calling) and speech-to-text (Whisper),
  with a provider abstraction so OpenAI, OpenRouter, Ollama or LM Studio can be swapped in;
- targets Windows first, with OS-specific code isolated behind `PlatformAdapter`.

## Layers

```
 USER ─ voice ─► HUD mic (VAD, barge-in) ─► /api/voice/transcribe (Whisper) ─┐
      └ text ──► HUD command line ────────► WebSocket  user_text ───────────┤
                                                                             ▼
                                   Orchestrator.on_user_input
                     echo guard → wake word → pending confirmation? → IntentRouter
                                                                             │
             ┌──────────────────────────┬────────────────────────────────────┼─────────────────────┐
             ▼                          ▼                                    ▼                     ▼
   deterministic handler       background task (research /           LLM tool loop           STOP / cancel
   (time, open app, mute,      multi-step agent) via TaskLauncher    (ContextBuilder →      (cancel turn,
    remember, timer…)          → TaskManager (semaphore, progress,    ToolLoop → executor)    mute speech)
             │                   deadlines, cancellation)                   │
             └──────────────► ToolExecutor ◄───────────────────────────────┘
                 validate → permission → risk assess → confirm → timeout/retry → verify → audit
                                              │
                               PlatformAdapter (windows | linux | macos | mock)
                                              │
            ResponseStream: response_start/delta/end + sentence-level `speak` events
                                              │
             EventBus ─► WebSocket ─► HUD (panels, captions, TTS queue, activity, tasks)
```

The model never executes anything itself. Its only way to act is a structured tool call, and every
call goes through `ToolExecutor`.

## Module map

| Path | Responsibility |
|---|---|
| `jarvis/app.py` | FastAPI app: HUD page, REST, WebSocket, voice endpoints, security middleware |
| `jarvis/config.py` | All settings from environment/`.env`; secrets held as `SecretStr` |
| `jarvis/logging_setup.py` | JSON logs with session/task context and secret redaction |
| `jarvis/api/schemas.py` | Typed event and client-message schemas (pydantic, discriminated unions) |
| `jarvis/core/orchestrator.py` | The brain: input handling, routing, turns, interruption, announcements |
| `jarvis/core/router.py` | Deterministic intent routing and normalisation |
| `jarvis/core/state.py` | Assistant state machine with an explicit transition table |
| `jarvis/core/toolloop.py` | Bounded OBSERVE → ACT → VERIFY loop around tool calling |
| `jarvis/core/context.py` | Prompt assembly, memory retrieval, history window and summarisation |
| `jarvis/core/response.py` | Streams responses to the HUD and sentence-by-sentence to TTS |
| `jarvis/core/confirmation.py` | Confirmation gate; a timeout counts as "no" |
| `jarvis/core/services.py` | Service container, boot sequence, diagnostics |
| `jarvis/core/session.py` | Sessions shared by voice and text, persisted and restored |
| `jarvis/llm/` | Provider interface, streaming OpenAI-compatible client, mock/scripted LLMs, model strategy, modular prompts, sentence chunker |
| `jarvis/tools/` | Tool base classes, registry, executor, path safety, tool statistics, built-in tools |
| `jarvis/platform/` | OS adapters: Windows (ctypes/user32 plus fixed PowerShell scripts), Linux, macOS, mock |
| `jarvis/memory/store.py` | Long-term and episodic memory with BM25 relevance, dedup and confidence |
| `jarvis/tasks/` | Task manager, planner/agent executor, reminders, launcher |
| `jarvis/web/` | Search providers, SSRF-safe fetching, readable-text extraction, research agent, weather |
| `jarvis/voice/` | Server-side STT/TTS, wake-word detection |
| `jarvis/telemetry/` | Real CPU/RAM/disk/GPU/battery/network metrics and process ranking |
| `web/` | The HUD (vanilla ES modules, no build step) |

## Assistant state machine

States: `STANDBY, LISTENING, THINKING, PLANNING, EXECUTING, SPEAKING, INTERRUPTED,
WAITING_FOR_CONFIRMATION, BACKGROUND_TASK, ERROR`. Each session has exactly one current state.
Transitions are validated against the table in `core/state.py`. An illegal transition caused by a
race (for example an interrupt arriving mid-step) is logged, and the state recovers through
`STANDBY` rather than getting stuck. Every change is published as an `assistant_state` event.

The idle state depends on what is actually true:
- `BACKGROUND_TASK` if tasks are running;
- otherwise `LISTENING` if a microphone is live;
- otherwise `STANDBY`.

`SPEAKING` lasts until the HUD reports that audio playback has ended.

## Conversation turn

1. **Echo guard**: a voice transcript that mostly matches what JARVIS just said is discarded.
2. **Wake word**: voice input needs "Jarvis" / "Hey Jarvis". Matching tolerates common transcription
   errors ("Jervis", "Jarvice"…). Follow-ups within `JARVIS_CONVERSATION_WINDOW_S` of the last reply
   don't need it. A bare "Jarvis" gets "Yes, sir?".
3. **Pending confirmation**: a clear yes/no answers it. Anything else declines it and is handled as
   a new request.
4. **Routing**: "stop" cancels the current turn immediately. A new request supersedes a running one.
5. **Execution**: a deterministic handler, a background task, or the LLM tool loop.
6. **Response**: text streams to the HUD. Complete sentences are sent as `speak` events as soon as
   they exist, so sentence 1 is being spoken while sentence 2 is still being generated.
7. **Settle**: the state returns to idle, queued announcements are delivered, and history is
   compacted if it has grown.

## Interruption and cancellation

| Trigger | Effect |
|---|---|
| User starts talking while JARVIS speaks (client VAD) | Audio stops instantly in the browser, and `interrupt` is sent. The server mutes the rest of the response. The turn itself continues silently, so a cough can't abort a file operation. |
| "Stop" / "Cancel" / "Never mind" | Speech stops, the turn's `CancelToken` and asyncio task are cancelled (LLM stream and running tools), and pending confirmations are declined. |
| New request during a turn | The old turn is cancelled and the new one starts. |
| "Cancel the task" / HUD ✕ | The background task is cancelled. Tasks are never cancelled by a plain "stop". |

## Tools

See [TOOLS.md](TOOLS.md). Key properties:
- Pydantic input schemas are exported to the model as JSON Schema.
- Risk is assessed per call, not just per tool.
- `HIGH` risk always requires confirmation, enforced in the executor.
- Results carry `verified` (independent check), `simulated` and a truthful `summary`.
- `ToolStats` records which built-in launch method works and reorders them. Only the order of
  predefined safe methods is learned, never new behaviour.
- Only tool categories relevant to the request (plus recently used ones) are sent to the model. This
  keeps prompts small, which matters for Groq's per-minute token limits.

## Memory

| Kind | Where | Notes |
|---|---|---|
| Short-term | `Session.history` | Recent messages including tool calls/results. Older turns are summarised by the fast model. |
| Working | `TaskManager` | Live task state, steps, progress, results. |
| Long-term | `memories` table: `preference`, `fact` | Explicit ("remember…") or model-initiated via `memory_remember`. Deduplicated (key or ≥75 % token overlap). Confidence and timestamps are stored. Secrets are refused. |
| Episodic | `memories` table: `episodic` | Completed research and tasks, so "continue the laptop research from yesterday" resumes rather than restarts. |

Retrieval per request: high-confidence preferences plus the top BM25 matches (weighted by
confidence and a gentle recency decay). The whole database is never dumped into a prompt. Users can
list, forget one, "forget that", or clear everything (high-risk, confirmed) by voice or in the
SETTINGS view.

## Background work

- `TaskManager`: a semaphore bounds concurrency (default 3). Each task has progress, steps, ETA,
  optional deadline, cancellation and a persisted record. A deadline monitor warns once if a task
  is unlikely to finish in time. Tasks interrupted by a restart are marked failed, honestly.
- `AgentRunner` (planner): the reasoning model produces a validated `Plan` (a dependency graph).
  Ready steps run in parallel (bounded). Each step is a bounded tool loop that must finish by calling
  `report_step`. A "completed" claim with no successful tool evidence is rejected. A failed step is
  retried once, and its dependents are skipped.
- `ResearchAgent`: generates queries, searches in parallel, ranks sources for authority and domain
  diversity, reads pages, extracts relevant passages, then the reasoning model writes a cited report
  noting conflicts and uncertainty. The result is stored as episodic memory.
- `ReminderService`: persisted reminders and timers. Missed reminders are announced as missed.
  Nothing recurs unless explicitly asked.

## Prompt architecture

`jarvis/llm/prompts/*.md` holds independent sections: identity, personality, safety, tool use,
spoken vs on-screen output, and response modes (quick / conversational / technical / operational /
alert). `build_system_prompt` assembles them with the user profile, retrieved memories, active
tasks, live context (time, OS, active window, location) and the conversation summary.

## Model strategy

| Tier | Default (Groq) | Used for |
|---|---|---|
| fast | `llama-3.1-8b-instant` | Query planning, summaries, history compaction |
| general | `llama-3.3-70b-versatile` | Conversation with tool use, agent steps |
| reasoning | `openai/gpt-oss-120b` | Planning, research synthesis, technical questions |
| vision | `meta-llama/llama-4-scout-17b-16e-instruct` | Screen analysis |

At boot the provider's model list is checked. A configured model that isn't offered is replaced by
a known-good alternative, and this is reported as a boot warning. Simple commands skip the LLM
entirely.

## Event protocol

Server → HUD (`jarvis/api/schemas.py`): `session_snapshot`, `boot_progress`, `assistant_state`,
`transcription`, `user_message`, `response_start`, `response_delta`, `response_end`, `speak`,
`speech_stop`, `tool_started`, `tool_completed`, `tool_failed`, `task_created`, `task_progress`,
`task_completed`, `task_failed`, `task_cancelled`, `memory_updated`, `system_metrics`,
`notification`, `error`, `confirmation_request`, `confirmation_resolved`, `activity`.

HUD → server: `hello`, `user_text`, `interrupt`, `confirm`, `cancel_task`, `tts_state`,
`client_settings`, `ping`.

Every activity line the HUD shows corresponds to a real backend event.

## Security model

- The server binds to `127.0.0.1`. The Host header must be local (blocks DNS rebinding), and the
  Origin must match (blocks other websites from driving the assistant).
- A random access token is generated per run and embedded only in the HUD page served by this
  process. It is required for every API and WebSocket call.
- API keys live only in `.env` or the environment, are held as `SecretStr`, are redacted from all
  logs (configured values and common key shapes), and never reach the browser. STT and TTS calls go
  through the server.
- No shell tool exists. Subprocesses use fixed executables with argument lists. PowerShell scripts
  are constants, and dynamic values are passed via environment variables, never interpolated.
- The filesystem is restricted to `JARVIS_ALLOWED_PATHS`. Symlinks are resolved before checking.
  Credential stores (`.ssh`, `.env`, keys, browser login databases) and JARVIS's own data are off
  limits. Overwrites and edits keep backups, and deletes go to the Recycle Bin.
- Web fetches refuse private-network addresses, including via redirects (SSRF).
- The WebSocket is rate limited. Tool calls are budgeted per turn and agents have step limits.
- An audit log records every medium- and high-risk action and every declined confirmation.

## Observability

- `data/logs/jarvis.log`: JSON lines with timestamp, session, task, tool, duration and outcome.
- `GET /api/diagnostics`, the SYSTEMS view, and "JARVIS, diagnostics": component health, latency
  (STT, first token, first sentence, first audio, TTS, total), per-tool latency, success counts,
  task completion rate, recent warnings/errors, and learned tool-method stats.
- `python -m jarvis check`: the same boot checks from the command line.
