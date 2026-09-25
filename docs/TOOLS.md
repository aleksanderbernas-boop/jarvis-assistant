# Tools

Every capability J.A.R.V.I.S. has is a **tool**: a named, schema-validated, risk-classified unit of work.
The language model can act *only* by calling these tools through structured tool calling. It never
gets a shell.

**Risk levels**

| Level | Behaviour |
|---|---|
| `low` | Runs immediately (open an app, search, read a file, check the system). |
| `medium` | Changes something. Runs immediately unless the specific call warrants a confirmation, e.g. overwriting a file, moving more than 20 files or a folder, pressing Alt+F4, or typing a long text. `JARVIS_CONFIRM_MEDIUM_RISK=false` turns these contextual confirmations off. |
| `high` | **Always** asks for explicit confirmation (deleting files, clearing memory, force-closing apps). No tool or setting can bypass it. Silence or an unclear answer counts as "no". |

**Gate**: the tool is disabled unless you enable the matching permission in `.env`
(`web` → `JARVIS_ALLOW_WEB`, `vision` → `JARVIS_ALLOW_VISION`, `input` → `JARVIS_ALLOW_INPUT_CONTROL`,
`clipboard` → `JARVIS_ALLOW_CLIPBOARD`).
**Data**: `remote` means the tool sends data off your machine (for example a search query, or a
screenshot sent to the vision model).

Every medium- and high-risk call, including declined ones, is recorded in `data/audit.log`.

### apps

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `close_app` | medium | — | local | Close an application gracefully (it may prompt to save). force=true kills it. |
| `focus_app` | low | — | local | Bring an application's window to the front. |
| `list_apps` | low | — | local | List installed applications, optionally filtered by a search term. |
| `open_app` | low | — | local | Launch an application (or bring it to the front if it's already running) and verify it started. |
| `running_apps` | low | — | local | List applications that currently have open windows. |

### audio

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `audio_devices` | low | — | local | List audio input/output devices. |
| `volume_control` | low | — | local | Get or change the system volume, or mute/unmute. |

### clipboard

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `clipboard_read` | low | clipboard | local | Read the text currently on the clipboard. |
| `clipboard_write` | low | clipboard | local | Put text on the clipboard. |

### diagnostics

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `diagnostics` | low | — | local | Full self-diagnostic: backend, language model, voice, tools, memory, connections, tasks, errors. |
| `list_tools` | low | — | local | List JARVIS's tools/capabilities and whether each is enabled. |
| `recent_errors` | low | — | local | Recent warnings and errors from JARVIS's logs — use to explain why something failed. |

### display

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `display_info` | low | — | local | Monitor count and resolution, plus current brightness where available. |
| `set_brightness` | low | — | local | Set the built-in display's brightness (laptops; most external monitors don't support this). |

### files

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `fs_copy` | medium | — | local | Copy one or many files/folders into a destination folder. |
| `fs_delete` | high | — | local | Delete files or folders. Items go to the Recycle Bin when possible. Always requires the user's explicit confirmation — identify the exact items first. |
| `fs_edit` | medium | — | local | Edit a text file by replacing exact text. A backup is kept. |
| `fs_info` | low | — | local | Details about a file or folder: size, item count, dates, whether it exists. |
| `fs_list` | low | — | local | List a folder's contents (names, types, sizes, modified times). |
| `fs_mkdir` | medium | — | local | Create a folder (and any missing parents). |
| `fs_move` | medium | — | local | Move (or rename) one or many files/folders into a destination folder. Use this for organising files; pass all files in one call. |
| `fs_read` | low | — | local | Read a text, PDF or Word document (for answering questions about it or summarising it). |
| `fs_rename` | medium | — | local | Rename a file or folder in place. |
| `fs_search` | low | — | local | Find files by name pattern, text content and/or modification date under a folder. |
| `fs_write` | medium | — | local | Create a text file, or overwrite/append to one. Overwrites keep a backup copy. |
| `open_file` | low | — | local | Open a file or folder with its default application (or reveal it in the file manager). |

### input

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `mouse_action` | medium | input | local | Move, click, double-click or scroll the mouse at screen coordinates. |
| `press_keys` | medium | input | local | Press a key or keyboard shortcut in the focused window. |
| `type_text` | medium | input | local | Type text into the currently focused window. |

### memory

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `memory_clear` | high | — | local | Erase all long-term memories (or all of one kind). Requires confirmation. |
| `memory_forget` | low | — | local | Delete a specific memory by id, or the single memory that best matches a description. |
| `memory_list` | low | — | local | List what JARVIS remembers about the user. |
| `memory_recall` | low | — | local | Search long-term memory (preferences, facts, past research and tasks). |
| `memory_remember` | low | — | local | Save a durable preference or fact about the user to long-term memory. Only for things worth remembering across conversations — never secrets or passwords. |

### reminders

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `reminder_cancel` | low | — | local | Cancel a pending reminder by id (use reminder_list first). |
| `reminder_create` | low | — | local | Set a reminder or timer that JARVIS will announce at the given time. |
| `reminder_list` | low | — | local | List pending reminders and timers. |

### screen

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `describe_screen` | low | vision | remote | Take a screenshot and analyse it with the vision model: what's on screen, read visible text, identify UI controls. Sends the image to the model provider. |
| `take_screenshot` | low | — | local | Capture the screen (or the active window) and save it locally as a PNG. |

### system

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `network_status` | low | — | local | Check internet connectivity and current network throughput. |
| `os_info` | low | — | local | Operating system, hardware and machine information. |
| `system_status` | low | — | local | Current CPU, RAM, disk, GPU, battery, network and uptime. |
| `top_processes` | low | — | local | Which applications/processes use the most memory or CPU (grouped by app). |

### tasks

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `task_cancel` | low | — | local | Cancel a running background task. |
| `task_list` | low | — | local | List active and recent background tasks with progress and results. |
| `task_start` | low | — | local | Start long-running work in the background (in-depth research, organising many files, multi-step jobs). The conversation continues while it runs; the user is notified when done. |

### web

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `open_url` | low | — | local | Open a URL (or a web search) in the user's browser so they can see it. |
| `weather` | low | web | remote | Live current weather and forecast (temperature, conditions, chance of rain). |
| `web_fetch` | low | web | remote | Open a web page and extract its readable text (to read an article, docs, a product page). |
| `web_search` | low | web | remote | Search the web for current information. Returns titles, URLs and snippets. |

### windows

| Tool | Risk | Gate | Data | What it does |
|---|---|---|---|---|
| `list_windows` | low | — | local | List open windows with their titles, apps, handles, and which one is focused. |
| `window_control` | low | — | local | Focus, minimise, maximise, restore, or move/resize a window. |

## Adding a tool

1. Create a class in any module under `jarvis/tools/builtin/` (or a new module there):

```python
from pydantic import BaseModel, Field

from jarvis.api.schemas import RiskLevel
from jarvis.tools.base import RetryPolicy, Tool, ToolContext, ToolError, ToolResult


class PlaySongInput(BaseModel):
    query: str = Field(description="Song or artist to play")


class PlaySong(Tool):
    name = "play_song"
    description = "Play a song in Spotify."          # the model reads this
    display_name = "Starting music"                  # the HUD shows this
    category = "audio"                               # used to pick relevant tools per request
    Input = PlaySongInput                            # validated before execute() runs
    risk = RiskLevel.LOW
    timeout_s = 20.0
    retry = RetryPolicy(max_attempts=2)              # retried only on TransientToolError
    data_boundary = "remote"

    async def execute(self, args: PlaySongInput, ctx: ToolContext) -> ToolResult:
        ...                                          # do the work; raise ToolError on failure
        return ToolResult.success(f"Playing {args.query}")

    async def verify(self, args, result, ctx) -> bool | None:
        ...                                          # optional: confirm the effect independently
```

2. Add it to that module's `TOOLS` list.

That's it. The registry discovers it, the model receives its JSON schema, and the executor enforces
validation, permissions, confirmation, timeout, retries, verification, logging, audit and HUD events.

Optional hooks:

- `available(services)`: return `(False, reason)` when the tool can't run on this machine.
- `assess(args, ctx)`: context-dependent risk (for example "this call touches 248 files").
- `validate(args, ctx)`: semantic checks beyond the schema.
- `parallel_safe = True`: read-only tools that may run concurrently.
- `permission = "web" | "vision" | "input" | "clipboard"`: gate the tool behind a setting.

OS-specific behaviour belongs in `jarvis/platform/` (`PlatformAdapter`), not in tools. Tools call
`ctx.platform`, which is the Windows, Linux, macOS or simulated (mock-mode) implementation.
