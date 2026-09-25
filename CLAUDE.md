# J.A.R.V.I.S. — development notes

- Python 3.11+ backend (`jarvis/`), vanilla-JS HUD with no build step (`web/`).
- Checks before committing: `python -m pytest -q`, `ruff check jarvis tests`, `mypy jarvis`.
- Develop against mock mode (`python -m jarvis serve --mock`): simulated computer, fake web, scripted LLM.
- New capabilities are tools: subclass `jarvis.tools.base.Tool` in `jarvis/tools/builtin/` and add them to
  that module's `TOOLS` list (see `docs/TOOLS.md`). OS-specific code goes in `jarvis/platform/`, never in tools.
- Never add a tool that runs model-provided shell strings. Subprocesses take fixed argument lists, and
  PowerShell gets dynamic values only through environment variables.
- HIGH-risk tools must stay behind the executor's confirmation gate. Results must be truthful: set `verified`
  from an independent check, and `simulated` when nothing real happened.
- Every HUD update comes from a typed event in `jarvis/api/schemas.py`. Don't fake activity or telemetry.
- Secrets live only in `.env` (gitignored). Never commit keys, log them, or send them to the browser.
