"""OS abstraction layer.

All operating-system specific behaviour lives behind `PlatformAdapter`. Tools
only ever talk to this interface, so Windows, Linux and macOS support can
evolve independently — and mock mode swaps in a simulated computer.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil


class PlatformError(Exception):
    """An OS operation failed. The message is safe to show the user."""


class NotSupported(PlatformError):
    """The operation is not available on this OS / without an optional dependency."""


@dataclass
class AppInfo:
    name: str
    target: str
    kind: str  # appid | shortcut | exe | uri | desktop | bundle | mock
    source: str = ""


@dataclass
class WindowInfo:
    handle: str
    title: str
    app: str = ""
    pid: int | None = None
    minimized: bool = False
    maximized: bool = False
    focused: bool = False
    rect: tuple[int, int, int, int] | None = None  # x, y, width, height

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "title": self.title,
            "app": self.app,
            "pid": self.pid,
            "minimized": self.minimized,
            "maximized": self.maximized,
            "focused": self.focused,
            "rect": list(self.rect) if self.rect else None,
        }


@dataclass
class ProcessInfo:
    pid: int
    name: str
    memory_mb: float
    cpu_percent: float
    exe: str = ""


@dataclass
class LaunchMethod:
    name: str
    run: Callable[[], None]


@dataclass
class DisplayInfo:
    monitors: int
    primary_width: int
    primary_height: int
    details: dict[str, Any] = field(default_factory=dict)


def run_command(args: list[str], *, timeout: float = 10.0, env: dict[str, str] | None = None,
                input_text: str | None = None) -> str:
    """Run a fixed executable with an argument list — never through a shell."""
    if not args or not shutil.which(args[0]) and not Path(args[0]).is_file():
        raise NotSupported(f"'{args[0] if args else '?'}' is not available on this system")
    merged_env = {**os.environ, **(env or {})}
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged_env,
            input=input_text,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise PlatformError(f"{Path(args[0]).name} timed out") from exc
    except OSError as exc:
        raise PlatformError(f"could not run {Path(args[0]).name}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise PlatformError(f"{Path(args[0]).name} failed: {detail[-1] if detail else completed.returncode}")
    return completed.stdout


def spawn_detached(args: list[str]) -> None:
    """Start a GUI program without waiting for it and without a shell."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=os.name != "nt",
            close_fds=True,
        )
    except OSError as exc:
        raise PlatformError(f"could not start {Path(args[0]).name}: {exc}") from exc


class PlatformAdapter:
    """Default implementations use cross-platform libraries; OS subclasses override."""

    name = "generic"
    simulated = False
    _APPS_TTL_S = 600.0

    def __init__(self) -> None:
        self._apps_cache: list[AppInfo] | None = None
        self._apps_cached_at = 0.0

    # ── Applications ────────────────────────────────────────────────────────
    def installed_apps(self, refresh: bool = False) -> list[AppInfo]:
        if refresh or self._apps_cache is None or time.time() - self._apps_cached_at > self._APPS_TTL_S:
            self._apps_cache = self._discover_apps()
            self._apps_cached_at = time.time()
        return self._apps_cache

    def _discover_apps(self) -> list[AppInfo]:
        return []

    def launch_methods(self, app: AppInfo) -> list[LaunchMethod]:
        raise NotSupported("launching applications is not supported on this OS yet")

    def default_browser(self) -> str | None:
        """Display name of the default web browser, if the OS exposes it."""
        return None

    def processes(self) -> list[ProcessInfo]:
        procs: list[ProcessInfo] = []
        for proc in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent", "exe"]):
            info = proc.info
            mem = info.get("memory_info")
            procs.append(
                ProcessInfo(
                    pid=info["pid"],
                    name=info.get("name") or "",
                    memory_mb=(mem.rss / 1_048_576) if mem else 0.0,
                    cpu_percent=info.get("cpu_percent") or 0.0,
                    exe=info.get("exe") or "",
                )
            )
        return procs

    def all_pids(self) -> set[int]:
        return set(psutil.pids())

    def app_pids(self, tokens: list[str]) -> set[int]:
        """PIDs whose process name matches any of the app-name tokens."""
        pids = set()
        for proc in psutil.process_iter(["name"]):
            pname = (proc.info.get("name") or "").lower().removesuffix(".exe")
            if any(t == pname or (len(t) >= 4 and t in pname) for t in tokens):
                pids.add(proc.pid)
        return pids

    def end_processes(self, pids: set[int], force: bool) -> None:
        for pid in pids:
            try:
                proc = psutil.Process(pid)
                proc.kill() if force else proc.terminate()
            except psutil.Error:
                continue

    def wait_exit(self, pids: set[int], timeout: float) -> set[int]:
        """Wait for processes to exit; return the PIDs still alive."""
        procs = []
        for pid in pids:
            try:
                procs.append(psutil.Process(pid))
            except psutil.Error:
                continue
        _gone, alive = psutil.wait_procs(procs, timeout=timeout)
        return {p.pid for p in alive}

    def close_process_windows(self, pids: set[int]) -> int:
        """Ask the windows of these processes to close gracefully. Returns count asked."""
        raise NotSupported("graceful window close is not supported on this OS")

    # ── Windows ─────────────────────────────────────────────────────────────
    def windows(self) -> list[WindowInfo]:
        raise NotSupported("window management is not supported on this OS yet")

    def active_window(self) -> WindowInfo | None:
        return None

    def window_action(self, handle: str, action: str, rect: tuple[int, int, int, int] | None = None) -> None:
        raise NotSupported("window management is not supported on this OS yet")

    # ── Clipboard ───────────────────────────────────────────────────────────
    def clipboard_read(self) -> str:
        raise NotSupported("clipboard access is not supported on this OS yet")

    def clipboard_write(self, text: str) -> None:
        raise NotSupported("clipboard access is not supported on this OS yet")

    # ── Screen ──────────────────────────────────────────────────────────────
    def screenshot(self, active_window: bool = False) -> bytes:
        """Return PNG bytes."""
        try:
            from PIL import ImageGrab
        except ImportError as exc:
            raise NotSupported("screenshots need Pillow: pip install pillow") from exc
        bbox = None
        if active_window:
            win = self.active_window()
            if win and win.rect:
                x, y, w, h = win.rect
                bbox = (x, y, x + w, y + h)
        try:
            image = ImageGrab.grab(bbox=bbox, all_screens=bbox is None)
        except Exception as exc:  # Pillow raises OSError / various backend errors
            raise PlatformError(f"screen capture failed: {exc}") from exc
        import io

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def display_info(self) -> DisplayInfo:
        raise NotSupported("display information is not supported on this OS yet")

    def brightness_get(self) -> int:
        raise NotSupported("brightness control is not supported on this OS/display")

    def brightness_set(self, percent: int) -> None:
        raise NotSupported("brightness control is not supported on this OS/display")

    # ── Audio ───────────────────────────────────────────────────────────────
    def volume_get(self) -> dict[str, Any]:
        raise NotSupported("volume control is not supported on this OS yet")

    def volume_set(self, percent: int) -> None:
        raise NotSupported("volume control is not supported on this OS yet")

    def volume_step(self, up: bool, steps: int = 5) -> None:
        raise NotSupported("volume control is not supported on this OS yet")

    def set_mute(self, muted: bool | None) -> None:
        raise NotSupported("mute control is not supported on this OS yet")

    def audio_devices(self) -> list[dict[str, Any]]:
        raise NotSupported("listing audio devices is not supported on this OS yet")

    # ── Keyboard / mouse (pyautogui, opt-in) ────────────────────────────────
    def _pyautogui(self) -> Any:
        try:
            import pyautogui
        except ImportError as exc:
            raise NotSupported("keyboard/mouse control needs pyautogui: pip install pyautogui") from exc
        pyautogui.FAILSAFE = True  # slam the mouse into a corner to abort
        return pyautogui

    def type_text(self, text: str, interval: float = 0.01) -> None:
        self._pyautogui().write(text, interval=interval)

    def press_keys(self, keys: list[str]) -> None:
        gui = self._pyautogui()
        if len(keys) == 1:
            gui.press(keys[0])
        else:
            gui.hotkey(*keys)

    def mouse(self, action: str, x: int | None, y: int | None, button: str, amount: int) -> None:
        gui = self._pyautogui()
        if action == "move" and x is not None and y is not None:
            gui.moveTo(x, y, duration=0.2)
        elif action == "click":
            gui.click(x=x, y=y, button=button)
        elif action == "double_click":
            gui.doubleClick(x=x, y=y, button=button)
        elif action == "scroll":
            gui.scroll(amount, x=x, y=y)
        else:
            raise PlatformError(f"unknown mouse action '{action}'")

    # ── Files / URLs ────────────────────────────────────────────────────────
    def open_path(self, path: Path) -> None:
        raise NotSupported("opening files is not supported on this OS yet")

    def reveal_path(self, path: Path) -> None:
        self.open_path(path.parent)

    def open_url(self, url: str) -> None:
        if not webbrowser.open(url):
            raise PlatformError("no web browser could be opened")

    def trash(self, path: Path) -> bool:
        """Move to the recycle bin. Returns False if only permanent deletion is possible."""
        try:
            from send2trash import send2trash
        except ImportError:
            return False
        send2trash(str(path))
        return True

    # ── Async helpers ───────────────────────────────────────────────────────
    async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a blocking OS call off the event loop."""
        return await asyncio.to_thread(func, *args, **kwargs)
