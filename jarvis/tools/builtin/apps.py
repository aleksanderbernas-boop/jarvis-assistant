"""Application control: find, launch (with verification), focus and close apps."""

from __future__ import annotations

import asyncio
import re
import time
from difflib import SequenceMatcher

from pydantic import BaseModel, Field

from jarvis.api.schemas import RiskLevel
from jarvis.platform.base import AppInfo, NotSupported, PlatformAdapter, PlatformError, WindowInfo
from jarvis.tools.base import RiskAssessment, Tool, ToolContext, ToolError, ToolResult

ALIASES = {
    "vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
    "chrome": "google chrome", "edge": "microsoft edge", "firefox": "firefox", "explorer": "file explorer",
    "files": "file explorer", "file manager": "file explorer", "my files": "file explorer",
    "calc": "calculator", "cmd": "command prompt", "command line": "command prompt",
    "powershell": "windows powershell", "terminal": "terminal", "word": "word", "excel": "excel",
    "powerpoint": "powerpoint", "outlook": "outlook", "teams": "microsoft teams", "notes": "notepad",
    "task manager": "task manager", "settings": "settings", "photos": "photos", "pictures": "photos",
    "store": "microsoft store", "control panel": "control panel", "snipping tool": "snipping tool",
}
BROWSER_WORDS = {"browser", "my browser", "the browser", "web browser", "internet", "default browser"}
_KNOWN_BROWSERS = ["Google Chrome", "Microsoft Edge", "Firefox", "Brave", "Opera", "Safari", "Chromium"]
_NOISE = re.compile(r"\b(uninstall|help|readme|documentation|website|license|manual|release notes|updater?)\b", re.I)
_GENERIC_TOKENS = {"microsoft", "google", "windows", "the", "app", "application", "desktop", "mozilla"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s+#.]", " ", text.lower())).strip()


def app_tokens(name: str) -> list[str]:
    return [t for t in _norm(name).split() if len(t) >= 3 and t not in _GENERIC_TOKENS] or [_norm(name)]


def resolve_app(query: str, apps: list[AppInfo], default_browser: str | None = None) -> list[tuple[float, AppInfo]]:
    """Rank installed apps against a spoken name ("vs code", "my browser", "spotify")."""
    q = _norm(query)
    q = re.sub(r"^(the|my)\s+", "", q) if q not in BROWSER_WORDS else q
    if q in BROWSER_WORDS:
        names = {_norm(a.name): a for a in apps}
        for candidate in ([default_browser] if default_browser else []) + _KNOWN_BROWSERS:
            if candidate and _norm(candidate) in names:
                return [(1.0, names[_norm(candidate)])]
        return []
    target = ALIASES.get(q, q)
    scored: list[tuple[float, AppInfo]] = []
    for app in apps:
        n = _norm(app.name)
        if n in (q, target):
            score = 1.0
        elif n.startswith(target) or target in n.split():
            score = 0.9
        elif len(target) >= 3 and target in n:
            score = 0.85
        else:
            score = max(SequenceMatcher(None, target, n).ratio(), SequenceMatcher(None, q, n).ratio())
        if _NOISE.search(app.name):
            score -= 0.25
        if score >= 0.72:
            scored.append((score, app))
    scored.sort(key=lambda item: (-item[0], len(item[1].name)))
    return scored


def _windows_for(platform: PlatformAdapter, name: str) -> list[WindowInfo]:
    try:
        windows = platform.windows()
    except NotSupported:
        return []
    tokens = app_tokens(name)
    return [w for w in windows if any(t in w.title.lower() or t == w.app.lower() for t in tokens)]


async def wait_for_app(platform: PlatformAdapter, name: str, before_pids: set[int], wait_s: float = 8.0) -> bool:
    """Poll for evidence the app actually started: a new matching process or window."""
    deadline = time.monotonic() + wait_s
    tokens = app_tokens(name)
    while time.monotonic() < deadline:
        new_pids = await platform.call(platform.app_pids, tokens) - before_pids
        if new_pids or await asyncio.to_thread(_windows_for, platform, name):
            return True
        await asyncio.sleep(0.4)
    return False


class AppNameInput(BaseModel):
    name: str = Field(description="Application name as the user said it, e.g. 'Spotify', 'VS Code', 'my browser'")


class ListApps(Tool):
    name = "list_apps"
    description = "List installed applications, optionally filtered by a search term."
    display_name = "Checking installed applications"
    category = "apps"
    parallel_safe = True

    class Input(BaseModel):
        filter: str | None = None

    async def execute(self, args: Input, ctx: ToolContext) -> ToolResult:
        apps = await ctx.platform.call(ctx.platform.installed_apps)
        if args.filter:
            apps = [score_app[1] for score_app in resolve_app(args.filter, apps)] or [
                a for a in apps if args.filter.lower() in a.name.lower()]
        names = [a.name for a in apps]
        return ToolResult.success(f"{len(names)} applications", {"apps": names[:300]}, simulated=ctx.platform.simulated)


class OpenApp(Tool):
    name = "open_app"
    description = "Launch an application (or bring it to the front if it's already running) and verify it started."
    display_name = "Launching application"
    category = "apps"
    Input = AppNameInput
    timeout_s = 25.0

    async def execute(self, args: AppNameInput, ctx: ToolContext) -> ToolResult:
        platform = ctx.platform
        apps = await platform.call(platform.installed_apps)
        default_browser = await platform.call(platform.default_browser)
        matches = resolve_app(args.name, apps, default_browser)
        if not matches:
            close = sorted(apps, key=lambda a: SequenceMatcher(None, _norm(args.name), _norm(a.name)).ratio(),
                           reverse=True)[:3]
            raise ToolError(f"I couldn't find an installed application called '{args.name}'. "
                            f"Closest: {', '.join(a.name for a in close) or 'none'}.", kind="not_found")
        best_score, app = matches[0]
        if len(matches) > 1 and best_score < 0.9 and matches[1][0] >= best_score - 0.03:
            options = [m[1].name for m in matches[:4]]
            return ToolResult.failure(f"'{args.name}' is ambiguous: {', '.join(options)}", kind="ambiguous",
                                      data={"candidates": options})

        existing = await asyncio.to_thread(_windows_for, platform, app.name)
        if existing:
            try:
                await platform.call(platform.window_action, existing[0].handle, "focus")
                return ToolResult.success(f"{app.name} was already running; brought it to the front",
                                          {"app": app.name, "already_running": True}, verified=True,
                                          simulated=platform.simulated)
            except (PlatformError, NotSupported):
                pass  # fall through and launch normally

        before = await platform.call(platform.all_pids)
        methods = {m.name: m for m in platform.launch_methods(app)}
        if not methods:
            raise ToolError(f"I found {app.name} but have no way to launch it on this system", kind="not_supported")
        errors: list[str] = []
        for method_name in ctx.services.tool_stats.rank(self.name, list(methods)):
            try:
                await platform.call(methods[method_name].run)
            except (PlatformError, OSError) as exc:
                ctx.services.tool_stats.record(self.name, method_name, False)
                errors.append(f"{method_name}: {exc}")
                continue
            verified = True if platform.simulated else await wait_for_app(platform, app.name, before)
            ctx.services.tool_stats.record(self.name, method_name, verified)
            summary = (f"{app.name} is open" if verified
                       else f"{app.name} was launched, but I couldn't confirm a window or process yet")
            return ToolResult.success(summary, {"app": app.name, "method": method_name}, verified=verified,
                                      simulated=platform.simulated)
        raise ToolError(f"Every launch method for {app.name} failed — {'; '.join(errors)}", kind="launch_failed")


class CloseAppInput(AppNameInput):
    force: bool = Field(False, description="Force-kill (may lose unsaved work). Only if the user asked.")


class CloseApp(Tool):
    name = "close_app"
    description = "Close an application gracefully (it may prompt to save). force=true kills it."
    display_name = "Closing application"
    category = "apps"
    Input = CloseAppInput
    risk = RiskLevel.MEDIUM
    timeout_s = 20.0

    async def assess(self, args: CloseAppInput, ctx: ToolContext) -> RiskAssessment:
        if args.force:
            return RiskAssessment(RiskLevel.HIGH, f"Force-closing {args.name} will discard any unsaved work. Proceed?",
                                  needs_confirmation=True)
        return RiskAssessment(RiskLevel.MEDIUM)

    async def execute(self, args: CloseAppInput, ctx: ToolContext) -> ToolResult:
        platform = ctx.platform
        name = ALIASES.get(_norm(args.name), args.name)
        pids = await platform.call(platform.app_pids, app_tokens(name))
        if not pids:
            raise ToolError(f"{args.name} doesn't appear to be running", kind="not_running")
        if args.force:
            await platform.call(platform.end_processes, pids, True)
        else:
            try:
                asked = await platform.call(platform.close_process_windows, pids)
            except NotSupported:
                await platform.call(platform.end_processes, pids, False)
                asked = len(pids)
            if not asked:
                raise ToolError(f"{args.name} has no windows I can close; use force to end it", kind="no_windows")
        alive = await platform.call(platform.wait_exit, pids, 5.0)
        if alive:
            return ToolResult.success(
                f"Asked {args.name} to close, but {len(alive)} of its processes are still running "
                "(it may be waiting for you to save, or keep running in the background)",
                {"still_running": len(alive)}, verified=False, simulated=platform.simulated)
        return ToolResult.success(f"{args.name} closed", {"closed_processes": len(pids)}, verified=True,
                                  simulated=platform.simulated)


class RunningApps(Tool):
    name = "running_apps"
    description = "List applications that currently have open windows."
    display_name = "Checking running applications"
    category = "apps"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        windows = await ctx.platform.call(ctx.platform.windows)
        apps: dict[str, list[str]] = {}
        for window in windows:
            apps.setdefault(window.app or window.title, []).append(window.title)
        return ToolResult.success(f"{len(apps)} applications with open windows",
                                  {"apps": [{"app": a, "windows": t[:5]} for a, t in apps.items()]},
                                  simulated=ctx.platform.simulated)


class FocusApp(Tool):
    name = "focus_app"
    description = "Bring an application's window to the front."
    display_name = "Switching window"
    category = "apps"
    Input = AppNameInput

    async def execute(self, args: AppNameInput, ctx: ToolContext) -> ToolResult:
        name = ALIASES.get(_norm(args.name), args.name)
        windows = await asyncio.to_thread(_windows_for, ctx.platform, name)
        if not windows:
            raise ToolError(f"{args.name} has no open window", kind="not_running")
        if len(windows) > 1:
            windows.sort(key=lambda w: w.minimized)
        await ctx.platform.call(ctx.platform.window_action, windows[0].handle, "focus")
        return ToolResult.success(f"Switched to {windows[0].title}", {"window": windows[0].title}, verified=True,
                                  simulated=ctx.platform.simulated)


TOOLS = [ListApps, OpenApp, CloseApp, RunningApps, FocusApp]
