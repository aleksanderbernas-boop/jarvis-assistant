"""Linux implementation (X11/Wayland desktop tools where present)."""

from __future__ import annotations

import configparser
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from jarvis.platform.base import (
    AppInfo,
    DisplayInfo,
    LaunchMethod,
    NotSupported,
    PlatformAdapter,
    PlatformError,
    WindowInfo,
    run_command,
    spawn_detached,
)

_DESKTOP_DIRS = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path.home() / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    Path("/var/lib/snapd/desktop/applications"),
]


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


class LinuxPlatform(PlatformAdapter):
    name = "linux"

    def _discover_apps(self) -> list[AppInfo]:
        apps: dict[str, AppInfo] = {}
        for directory in _DESKTOP_DIRS:
            if not directory.is_dir():
                continue
            for path in directory.glob("*.desktop"):
                parser = configparser.ConfigParser(interpolation=None, strict=False)
                try:
                    parser.read(path, encoding="utf-8")
                    entry = parser["Desktop Entry"]
                except (configparser.Error, KeyError, UnicodeDecodeError):
                    continue
                if entry.get("NoDisplay", "false").lower() == "true" or entry.get("Type") != "Application":
                    continue
                name = entry.get("Name")
                if name:
                    apps.setdefault(name.lower(), AppInfo(name, str(path), "desktop", "desktop_file"))
        return sorted(apps.values(), key=lambda a: a.name.lower())

    def launch_methods(self, app: AppInfo) -> list[LaunchMethod]:
        methods: list[LaunchMethod] = []
        path = Path(app.target)
        if app.kind == "desktop" and _have("gtk-launch"):
            methods.append(LaunchMethod("gtk_launch", lambda: spawn_detached(["gtk-launch", path.stem])))
        if app.kind == "desktop":
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.read(path, encoding="utf-8")
            exec_line = parser["Desktop Entry"].get("Exec", "")
            argv = [a for a in shlex.split(re.sub(r"%[a-zA-Z]", "", exec_line)) if a]
            if argv:
                methods.append(LaunchMethod("desktop_exec", lambda: spawn_detached(argv)))
        exe = shutil.which(app.name.lower().replace(" ", "-")) or shutil.which(app.name.lower().replace(" ", ""))
        if exe:
            methods.append(LaunchMethod("path_lookup", lambda: spawn_detached([exe])))
        return methods

    def close_process_windows(self, pids: set[int]) -> int:
        if not _have("wmctrl"):
            raise NotSupported("graceful close needs wmctrl")
        count = 0
        for window in self.windows():
            if window.pid in pids:
                run_command(["wmctrl", "-ic", window.handle])
                count += 1
        return count

    def windows(self) -> list[WindowInfo]:
        if not _have("wmctrl"):
            raise NotSupported("window management on Linux needs wmctrl (sudo apt install wmctrl)")
        active = ""
        if _have("xdotool"):
            try:
                active = hex(int(run_command(["xdotool", "getactivewindow"]).strip()))
            except (PlatformError, ValueError):
                active = ""
        windows = []
        for line in run_command(["wmctrl", "-lpG"]).splitlines():
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            handle, _desk, pid, x, y, w, h, _host, title = parts
            windows.append(
                WindowInfo(handle=handle, title=title, pid=int(pid) or None,
                           focused=bool(active) and int(handle, 16) == int(active, 16),
                           rect=(int(x), int(y), int(w), int(h)))
            )
        return windows

    def active_window(self) -> WindowInfo | None:
        try:
            return next((w for w in self.windows() if w.focused), None)
        except NotSupported:
            return None

    def window_action(self, handle: str, action: str, rect: tuple[int, int, int, int] | None = None) -> None:
        if action == "focus":
            run_command(["wmctrl", "-ia", handle])
        elif action == "maximize":
            run_command(["wmctrl", "-ir", handle, "-b", "add,maximized_vert,maximized_horz"])
        elif action == "restore":
            run_command(["wmctrl", "-ir", handle, "-b", "remove,maximized_vert,maximized_horz"])
        elif action == "minimize":
            run_command(["xdotool", "windowminimize", str(int(handle, 16))])
        elif action == "move_resize" and rect:
            run_command(["wmctrl", "-ir", handle, "-e", f"0,{rect[0]},{rect[1]},{rect[2]},{rect[3]}"])
        elif action == "close":
            run_command(["wmctrl", "-ic", handle])
        else:
            raise PlatformError(f"unsupported window action '{action}'")

    def clipboard_read(self) -> str:
        if _have("wl-paste"):
            return run_command(["wl-paste", "--no-newline"])
        if _have("xclip"):
            return run_command(["xclip", "-selection", "clipboard", "-o"])
        raise NotSupported("clipboard needs wl-clipboard or xclip")

    def clipboard_write(self, text: str) -> None:
        if _have("wl-copy"):
            run_command(["wl-copy"], input_text=text)
        elif _have("xclip"):
            run_command(["xclip", "-selection", "clipboard"], input_text=text)
        else:
            raise NotSupported("clipboard needs wl-clipboard or xclip")

    def display_info(self) -> DisplayInfo:
        if not _have("xrandr"):
            raise NotSupported("display info needs xrandr")
        out = run_command(["xrandr", "--listmonitors"])
        monitors = re.findall(r"(\d+)/\d+x(\d+)/\d+", out)
        first = monitors[0] if monitors else ("0", "0")
        return DisplayInfo(monitors=len(monitors), primary_width=int(first[0]), primary_height=int(first[1]))

    def brightness_get(self) -> int:
        if not _have("brightnessctl"):
            raise NotSupported("brightness control needs brightnessctl")
        current = int(run_command(["brightnessctl", "get"]).strip())
        maximum = int(run_command(["brightnessctl", "max"]).strip())
        return round(current * 100 / maximum)

    def brightness_set(self, percent: int) -> None:
        if not _have("brightnessctl"):
            raise NotSupported("brightness control needs brightnessctl")
        run_command(["brightnessctl", "set", f"{int(percent)}%"])

    def volume_get(self) -> dict[str, Any]:
        if not _have("pactl"):
            raise NotSupported("volume control needs PulseAudio/PipeWire (pactl)")
        vol = re.search(r"(\d+)%", run_command(["pactl", "get-sink-volume", "@DEFAULT_SINK@"]))
        mute = "yes" in run_command(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
        return {"percent": int(vol.group(1)) if vol else None, "muted": mute}

    def volume_set(self, percent: int) -> None:
        run_command(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{max(0, min(100, percent))}%"])

    def volume_step(self, up: bool, steps: int = 5) -> None:
        run_command(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{'+' if up else '-'}{steps * 2}%"])

    def set_mute(self, muted: bool | None) -> None:
        state = "toggle" if muted is None else ("1" if muted else "0")
        run_command(["pactl", "set-sink-mute", "@DEFAULT_SINK@", state])

    def audio_devices(self) -> list[dict[str, Any]]:
        if not _have("pactl"):
            raise NotSupported("listing audio devices needs pactl")
        devices = []
        for kind, cmd in (("output", "sinks"), ("input", "sources")):
            for line in run_command(["pactl", "list", "short", cmd]).splitlines():
                parts = line.split("\t")
                if len(parts) > 1 and not parts[1].endswith(".monitor"):
                    devices.append({"name": parts[1], "type": kind})
        return devices

    def open_path(self, path: Path) -> None:
        if not _have("xdg-open"):
            raise NotSupported("opening files needs xdg-open")
        spawn_detached(["xdg-open", str(path)])

    def trash(self, path: Path) -> bool:
        if super().trash(path):
            return True
        if _have("gio"):
            run_command(["gio", "trash", str(path)])
            return True
        return False
