"""macOS implementation (basic): apps, clipboard, screenshots, volume, files."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from jarvis.platform.base import (
    AppInfo,
    LaunchMethod,
    PlatformAdapter,
    WindowInfo,
    run_command,
    spawn_detached,
)

_APP_DIRS = [Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications"]


def _osascript(script: str, env: dict[str, str] | None = None) -> str:
    return run_command(["osascript", "-e", script], env=env).strip()


class MacPlatform(PlatformAdapter):
    name = "macos"

    def _discover_apps(self) -> list[AppInfo]:
        apps: dict[str, AppInfo] = {}
        for directory in _APP_DIRS:
            if directory.is_dir():
                for bundle in directory.glob("*.app"):
                    apps.setdefault(bundle.stem.lower(), AppInfo(bundle.stem, str(bundle), "bundle", "applications"))
        return sorted(apps.values(), key=lambda a: a.name.lower())

    def launch_methods(self, app: AppInfo) -> list[LaunchMethod]:
        return [LaunchMethod("open", lambda: spawn_detached(["open", app.target]))]

    def active_window(self) -> WindowInfo | None:
        name = _osascript('tell application "System Events" to get name of first process whose frontmost is true')
        return WindowInfo(handle=name, title=name, app=name, focused=True) if name else None

    def clipboard_read(self) -> str:
        return run_command(["pbpaste"])

    def clipboard_write(self, text: str) -> None:
        run_command(["pbcopy"], input_text=text)

    def screenshot(self, active_window: bool = False) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "shot.png"
            run_command(["screencapture", "-x", str(out)])
            return out.read_bytes()

    def volume_get(self) -> dict[str, Any]:
        out = _osascript("get volume settings")
        vol = re.search(r"output volume:(\d+)", out)
        return {"percent": int(vol.group(1)) if vol else None, "muted": "output muted:true" in out}

    def volume_set(self, percent: int) -> None:
        _osascript(f"set volume output volume {max(0, min(100, int(percent)))}")

    def volume_step(self, up: bool, steps: int = 5) -> None:
        current = self.volume_get().get("percent") or 0
        self.volume_set(current + (steps * 2 if up else -steps * 2))

    def set_mute(self, muted: bool | None) -> None:
        if muted is None:
            muted = not self.volume_get().get("muted", False)
        _osascript(f"set volume output muted {'true' if muted else 'false'}")

    def open_path(self, path: Path) -> None:
        spawn_detached(["open", str(path)])

    def reveal_path(self, path: Path) -> None:
        spawn_detached(["open", "-R", str(path)])
