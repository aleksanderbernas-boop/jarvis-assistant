"""A simulated computer for mock mode and tests. Nothing real is touched."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvis.platform.base import (
    AppInfo,
    DisplayInfo,
    LaunchMethod,
    PlatformAdapter,
    PlatformError,
    ProcessInfo,
    WindowInfo,
)

_MOCK_APPS = [
    "Google Chrome", "Microsoft Edge", "Firefox", "Spotify", "Visual Studio Code", "Notepad",
    "File Explorer", "Calculator", "Photos", "Settings", "Windows Terminal", "Discord", "Word", "Excel",
    "OneDrive", "Task Manager",
]


class MockPlatform(PlatformAdapter):
    name = "mock"
    simulated = True

    def __init__(self, fail_launch: set[str] | None = None) -> None:
        super().__init__()
        self.running: dict[str, int] = {"File Explorer": 1000}
        self.focused: str | None = "File Explorer"
        self.clipboard = ""
        self.volume = 40
        self.muted = False
        self.brightness = 70
        self.opened: list[str] = []
        self.typed: list[str] = []
        self.trashed: list[str] = []
        self.fail_launch = fail_launch or set()
        self._next_pid = 2000

    def _discover_apps(self) -> list[AppInfo]:
        return [AppInfo(name=n, target=n, kind="mock", source="simulated") for n in _MOCK_APPS]

    def launch_methods(self, app: AppInfo) -> list[LaunchMethod]:
        def primary() -> None:
            if app.name in self.fail_launch:
                raise PlatformError(f"simulated launch failure for {app.name}")
            self._next_pid += 1
            self.running.setdefault(app.name, self._next_pid)
            self.focused = app.name

        return [LaunchMethod("simulated", primary)]

    def processes(self) -> list[ProcessInfo]:
        sizes = {"Google Chrome": 2100.0, "Visual Studio Code": 900.0, "Spotify": 350.0}
        procs = [
            ProcessInfo(pid=pid, name=f"{name.lower().replace(' ', '')}.exe", memory_mb=sizes.get(name, 120.0),
                        cpu_percent=3.0, exe=name)
            for name, pid in self.running.items()
        ]
        procs.append(ProcessInfo(pid=4, name="System", memory_mb=80.0, cpu_percent=1.0))
        return procs

    def all_pids(self) -> set[int]:
        return set(self.running.values())

    def app_pids(self, tokens: list[str]) -> set[int]:
        return {pid for name, pid in self.running.items() if any(t in name.lower() for t in tokens)}

    def end_processes(self, pids: set[int], force: bool) -> None:
        self.close_process_windows(pids)

    def wait_exit(self, pids: set[int], timeout: float) -> set[int]:
        return pids & set(self.running.values())

    def close_process_windows(self, pids: set[int]) -> int:
        closed = [name for name, pid in self.running.items() if pid in pids]
        for name in closed:
            del self.running[name]
        return len(closed)

    def windows(self) -> list[WindowInfo]:
        return [
            WindowInfo(handle=str(pid), title=name, app=name, pid=pid, focused=name == self.focused,
                       rect=(0, 0, 1280, 800))
            for name, pid in self.running.items()
        ]

    def active_window(self) -> WindowInfo | None:
        return next((w for w in self.windows() if w.focused), None)

    def window_action(self, handle: str, action: str, rect: tuple[int, int, int, int] | None = None) -> None:
        match = next((w for w in self.windows() if w.handle == handle), None)
        if not match:
            raise PlatformError("window not found")
        if action in {"focus", "restore", "maximize"}:
            self.focused = match.app

    def clipboard_read(self) -> str:
        return self.clipboard

    def clipboard_write(self, text: str) -> None:
        self.clipboard = text

    def screenshot(self, active_window: bool = False) -> bytes:
        # A valid 1x1 PNG.
        return bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201e2d5a5"
            "d20000000049454e44ae426082"
        )

    def display_info(self) -> DisplayInfo:
        return DisplayInfo(monitors=1, primary_width=1920, primary_height=1080, details={"simulated": True})

    def brightness_get(self) -> int:
        return self.brightness

    def brightness_set(self, percent: int) -> None:
        self.brightness = percent

    def volume_get(self) -> dict[str, Any]:
        return {"percent": self.volume, "muted": self.muted}

    def volume_set(self, percent: int) -> None:
        self.volume = percent

    def volume_step(self, up: bool, steps: int = 5) -> None:
        self.volume = max(0, min(100, self.volume + (steps * 2 if up else -steps * 2)))

    def set_mute(self, muted: bool | None) -> None:
        self.muted = (not self.muted) if muted is None else muted

    def audio_devices(self) -> list[dict[str, Any]]:
        return [{"name": "Simulated Speakers", "type": "output", "default": True},
                {"name": "Simulated Microphone", "type": "input", "default": True}]

    def type_text(self, text: str, interval: float = 0.01) -> None:
        self.typed.append(text)

    def press_keys(self, keys: list[str]) -> None:
        self.typed.append("+".join(keys))

    def mouse(self, action: str, x: int | None, y: int | None, button: str, amount: int) -> None:
        return None

    def open_path(self, path: Path) -> None:
        self.opened.append(str(path))

    def open_url(self, url: str) -> None:
        self.opened.append(url)

    def trash(self, path: Path) -> bool:
        self.trashed.append(str(path))
        return True
