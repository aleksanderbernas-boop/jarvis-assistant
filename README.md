# J.A.R.V.I.S.

An agentic, voice-first assistant layer for your computer, with a holographic HUD.

It is not a chatbot page. JARVIS listens for its wake word, answers by voice while it thinks, and
acts on your computer through a controlled set of tools. It can launch and close apps, arrange
windows, find, read and organise files, research the web with citations, report real system
telemetry, remember your preferences, and run background tasks you can check on or cancel. It
reports only what actually happened.

```
 face   → HUD (web/)                      voice → mic VAD + barge-in + streaming TTS
 brain  → orchestrator + LLM (Groq)       hands → 52 tools behind a permission/confirmation gate
 memory → SQLite long-term + episodic     executive function → planner, task manager, reminders
```

## Quick start (Windows)

1. Install **Python 3.11+** from python.org (tick "Add python.exe to PATH").
2. Double-click `scripts\start.bat`.
   - The first run creates `.venv`, installs everything, creates `.env` and opens it in Notepad.
3. In `.env`, set your key:
   ```
   GROQ_API_KEY=gsk_...
   ```
   Save, then run `scripts\start.bat` again. Your browser opens the HUD.
4. Click **ENGAGE** (this lets the page play audio and use the microphone). Then say
   **"Hey JARVIS"**, or type in the command line.

macOS / Linux: `./scripts/start.sh`.

Manual install:

```bash
python -m venv .venv && .venv\Scripts\activate      # source .venv/bin/activate on macOS/Linux
pip install -e ".[desktop,windows,dev]"
copy .env.example .env                               # cp on macOS/Linux, then edit it
python -m jarvis serve --open
```

## Commands

| Task | Command |
|---|---|
| Run (live) | `python -m jarvis serve --open` |
| Run safely with simulated tools, fake web and scripted LLM | `python -m jarvis serve --mock --open` |
| Boot checks and diagnostics | `python -m jarvis check` |
| List models your key can use | `python -m jarvis models` |
| Tests | `python -m pytest` |
| Lint | `ruff check jarvis tests` |
| Type check | `mypy jarvis` |

The HUD is served at <http://127.0.0.1:8765>.

## Things to say

| Say | What happens |
|---|---|
| "Jarvis." | "Yes, sir?" Follow-ups for the next 15 s need no wake word. |
| "What's using most of my memory?" | Measures processes, grouped by app: "Chrome is using the most memory — about 2.1 gigabytes across 23 processes." |
| "Open VS Code." / "Fire up my browser." | Launches the app, then verifies a new process or window actually appeared. |
| "Open Chrome, search for today's weather, and tell me if I need an umbrella." | Multi-step: the LLM chains tools and answers once. |
| "Research the best OLED laptops under 6000 zł." | Background research. You keep talking; it reports back with sources. |
| "Only Lenovo." / "Compare the first two." | Follow-ups use the conversation and earlier results. |
| "What are you working on?" / "Cancel the task." | Task status with progress, ETA and elapsed time; cancellation. |
| "Organise my screenshots from this month." | The planner runs a multi-step, verified file job in the background. Bulk moves ask first. |
| "Delete the old project folder." | Identifies the exact folder, then: "That will move 'old-project' (248 files, 1.2 GB) to the Recycle Bin. Proceed?" |
| "Remember I prefer VS Code." / "Forget that." / "What do you remember about me?" | Long-term memory under your control. |
| "Remind me in 30 minutes to stretch." / "Set a timer for 10 minutes." | Persistent reminders, announced by voice. |
| "What's on my screen?" | Screenshot plus vision model (needs `JARVIS_ALLOW_VISION=true`). |
| "Stop." | Speech and the current action stop immediately. Talking over JARVIS also cuts it off. |
| "Diagnostics." | Honest status of the LLM, voice, tools, memory, connections, tasks and recent errors. |

## Configuration

All configuration lives in `.env` (see [.env.example](.env.example); it is gitignored). Never put
keys in source code.

| Variable | Purpose |
|---|---|
| `GROQ_API_KEY` | **Required for live mode.** Reasoning (Llama / GPT-OSS on Groq) and Whisper speech-to-text. |
| `JARVIS_MODE` | `live` or `mock` (simulated computer, fake web, scripted LLM; nothing is modified). |
| `JARVIS_LLM_PROVIDER` | `groq` (default), `openai`, or `openai_compatible` (Ollama, LM Studio, OpenRouter via `JARVIS_LLM_BASE_URL` + `JARVIS_LLM_API_KEY`). |
| `JARVIS_MODEL_FAST/GENERAL/REASONING/VISION` | Model per job. Unavailable models are replaced automatically at boot. |
| `JARVIS_STT_PROVIDER` | `groq` (Whisper, best), `browser` (Web Speech API), `openai`, `none`. |
| `JARVIS_TTS_PROVIDER` | `browser` (free, instant, British voice preferred), `elevenlabs`, `openai`, `groq`. |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | For a premium voice. |
| `JARVIS_WAKE_WORDS`, `JARVIS_REQUIRE_WAKE_WORD`, `JARVIS_CONVERSATION_WINDOW_S` | Wake behaviour. |
| `JARVIS_HONORIFIC`, `JARVIS_USER_NAME` | How JARVIS addresses you. |
| `JARVIS_LOCATION_NAME`, `JARVIS_LATITUDE`, `JARVIS_LONGITUDE` | Weather panel, map and weather answers. |
| `JARVIS_ALLOWED_PATHS` | Folders JARVIS may touch (`;`-separated). Default: your home folder. |
| `JARVIS_ALLOW_WEB` / `_VISION` / `_INPUT_CONTROL` / `_CLIPBOARD` | Permission gates. Vision and keyboard/mouse are **off** by default. |
| `JARVIS_CONFIRM_MEDIUM_RISK` | Ask before bulk moves and overwrites (high-risk actions always ask). |
| `JARVIS_SEARCH_PROVIDER` | `duckduckgo` (no key), `tavily` + `TAVILY_API_KEY`, `brave` + `BRAVE_API_KEY`. |
| `JARVIS_DISABLED_TOOLS` | Comma-separated tool names to switch off. |

Optional extras (installed by `start.bat`): Pillow (screenshots/vision), pyautogui (keyboard/mouse),
send2trash (Recycle Bin), pypdf and python-docx (reading documents), pycaw (exact volume on Windows).
Everything degrades gracefully without them, and tools report what's missing.

## Data and privacy

- Local: `data/jarvis.db` (memory, sessions, tasks, reminders), `data/logs/`, `data/audit.log`,
  `data/backups/` (copies taken before overwrites/edits), `data/screenshots/`.
- Remote: your requests and relevant context go to the LLM provider. Search queries go to the
  search provider. Voice audio goes to Whisper (or stays in the browser with `browser` STT).
  Screenshots are sent **only** when vision is enabled and you ask about the screen. The SETTINGS
  view marks which tools send data off the machine.
- API keys stay on the server and are redacted from every log.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): layers, state machine, turn lifecycle,
  interruption, memory, tasks, prompts, events, security, observability.
- [docs/TOOLS.md](docs/TOOLS.md): every tool with its risk level and data boundary, plus how to add
  your own.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Top bar says **DEGRADED** / "Connecting services ✗" | `GROQ_API_KEY` missing or invalid. Run `python -m jarvis check`. |
| "The language model provider is rate-limiting me" | Groq free-tier token-per-minute limits. Wait a moment, or use smaller models in `.env`. |
| It hears me but ignores me | Say "Jarvis" first. The voice panel shows "Heard … say 'Jarvis' first". |
| It keeps answering itself | Use headphones, or keep echo cancellation on. JARVIS already drops transcripts that match its own speech. |
| Microphone error | Allow microphone access for `127.0.0.1` in the browser, then press the mic button. |
| An app won't open | "Open X" searches the Start menu. Try the exact name shown in `list_apps`, or check SYSTEMS → recent errors. |
| Web search fails | DuckDuckGo sometimes rate-limits. Set `JARVIS_SEARCH_PROVIDER=tavily` or `brave` with a key. |
| Volume "set to N %" unverified | `pip install pycaw` for exact volume control on Windows. |
| Anything else | "JARVIS, diagnostics", the SYSTEMS view, or `data/logs/jarvis.log`. |
