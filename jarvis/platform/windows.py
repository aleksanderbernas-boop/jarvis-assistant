"""Windows implementation.

Uses only the standard library (ctypes → user32) and fixed PowerShell scripts.
Dynamic values are passed to PowerShell through environment variables, never
interpolated into the script text, so model output can't inject commands.
Optional extras (pycaw, Pillow, send2trash) are used when installed.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import tempfile
import time
from ctypes import wintypes
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

SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE, SW_SHOW = 6, 3, 9, 5
WM_CLOSE = 0x0010
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
VK_MENU = 0x12
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
KEYEVENTF_KEYUP = 0x0002
SM_CXSCREEN, SM_CYSCREEN, SM_CMONITORS = 0, 1, 80

# Commonly requested apps that are not always in the Start menu list.
_BUILTINS: list[AppInfo] = [
    AppInfo("File Explorer", "explorer.exe", "exe", "builtin"),
    AppInfo("Notepad", "notepad.exe", "exe", "builtin"),
    AppInfo("Calculator", "calc.exe", "exe", "builtin"),
    AppInfo("Task Manager", "taskmgr.exe", "exe", "builtin"),
    AppInfo("Settings", "ms-settings:", "uri", "builtin"),
    AppInfo("Paint", "mspaint.exe", "exe", "builtin"),
    AppInfo("Snipping Tool", "ms-screenclip:", "uri", "builtin"),
]


def _powershell(script: str, env: dict[str, str] | None = None, timeout: float = 15.0) -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise NotSupported("PowerShell is not available")
    return run_command(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
        env=env,
    )


class WindowsPlatform(PlatformAdapter):
    name = "windows"

    def __init__(self) -> None:
        super().__init__()
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        u = self._user32
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
        u.GetWindow.restype = wintypes.HWND
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetForegroundWindow.restype = wintypes.HWND
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        u.IsWindow.argtypes = [wintypes.HWND]

    # ── Applications ────────────────────────────────────────────────────────
    def _discover_apps(self) -> list[AppInfo]:
        apps: dict[str, AppInfo] = {}
        try:
            raw = _powershell("Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress", timeout=20)
            data = json.loads(raw) if raw.strip() else []
            for item in data if isinstance(data, list) else [data]:
                name, app_id = item.get("Name"), item.get("AppID")
                if name and app_id:
                    apps.setdefault(name.lower(), AppInfo(name, app_id, "appid", "start_apps"))
        except (PlatformError, json.JSONDecodeError):
            pass
        for root in self._start_menu_dirs():
            for path in root.rglob("*.lnk"):
                name = path.stem
                if "uninstall" in name.lower():
                    continue
                apps.setdefault(name.lower(), AppInfo(name, str(path), "shortcut", "start_menu"))
        for builtin in _BUILTINS:
            apps.setdefault(builtin.name.lower(), builtin)
        return sorted(apps.values(), key=lambda a: a.name.lower())

    @staticmethod
    def _start_menu_dirs() -> list[Path]:
        dirs = []
        for env in ("ProgramData", "APPDATA"):
            base = os.environ.get(env)
            if base:
                candidate = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                if candidate.is_dir():
                    dirs.append(candidate)
        return dirs

    def launch_methods(self, app: AppInfo) -> list[LaunchMethod]:
        methods: list[LaunchMethod] = []
        if app.kind == "appid":
            methods.append(LaunchMethod("apps_folder", lambda: spawn_detached(
                ["explorer.exe", f"shell:AppsFolder\\{app.target}"])))
        if app.kind in {"shortcut", "uri"}:
            methods.append(LaunchMethod("startfile", lambda: os.startfile(app.target)))  # type: ignore[attr-defined]
        if app.kind == "exe":
            exe = shutil.which(app.target) or app.target
            methods.append(LaunchMethod("executable", lambda: spawn_detached([exe])))
        # Fallback: a Start-menu shortcut with the same name, or an exe on PATH.
        for root in self._start_menu_dirs():
            for path in root.rglob(f"{app.name}.lnk"):
                methods.append(LaunchMethod("start_menu_shortcut", lambda p=path: os.startfile(str(p))))  # type: ignore[attr-defined,misc]
                break
        exe_guess = shutil.which(app.name.replace(" ", "").lower())
        if exe_guess:
            methods.append(LaunchMethod("path_lookup", lambda: spawn_detached([exe_guess])))
        return methods

    def default_browser(self) -> str | None:
        import winreg

        key_path = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:  # type: ignore[attr-defined]
                prog_id = str(winreg.QueryValueEx(key, "ProgId")[0]).lower()  # type: ignore[attr-defined]
        except OSError:
            return None
        for marker, name in (("chrome", "Google Chrome"), ("msedge", "Microsoft Edge"), ("firefox", "Firefox"),
                             ("brave", "Brave"), ("opera", "Opera"), ("vivaldi", "Vivaldi")):
            if marker in prog_id:
                return name
        return None

    def close_process_windows(self, pids: set[int]) -> int:
        count = 0
        for window in self._enum_windows():
            if window.pid in pids:
                self._user32.PostMessageW(int(window.handle), WM_CLOSE, 0, 0)
                count += 1
        return count

    # ── Windows ─────────────────────────────────────────────────────────────
    def _enum_windows(self) -> list[WindowInfo]:
        u = self._user32
        foreground = u.GetForegroundWindow()
        results: list[WindowInfo] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[attr-defined]
        def callback(hwnd: int, _lparam: int) -> bool:
            if not u.IsWindowVisible(hwnd) or u.GetWindow(hwnd, GW_OWNER):
                return True
            if u.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            length = u.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            u.GetWindowTextW(hwnd, buffer, length + 1)
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(rect))
            results.append(
                WindowInfo(
                    handle=str(hwnd),
                    title=buffer.value,
                    pid=pid.value,
                    minimized=bool(u.IsIconic(hwnd)),
                    maximized=bool(u.IsZoomed(hwnd)),
                    focused=hwnd == foreground,
                    rect=(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
                )
            )
            return True

        u.EnumWindows(callback, 0)
        return results

    def windows(self) -> list[WindowInfo]:
        import psutil

        windows = self._enum_windows()
        for window in windows:
            try:
                window.app = psutil.Process(window.pid or 0).name().removesuffix(".exe")
            except (psutil.Error, ValueError):
                window.app = ""
        return [w for w in windows if w.title not in {"Program Manager", "Windows Input Experience"}]

    def active_window(self) -> WindowInfo | None:
        return next((w for w in self.windows() if w.focused), None)

    def window_action(self, handle: str, action: str, rect: tuple[int, int, int, int] | None = None) -> None:
        u = self._user32
        hwnd = int(handle)
        if not u.IsWindow(hwnd):
            raise PlatformError("that window no longer exists")
        if action == "focus":
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, SW_RESTORE)
            # Windows blocks focus stealing unless we appear to have input; a
            # synthetic ALT press is the documented-safe workaround.
            u.keybd_event(VK_MENU, 0, 0, 0)
            u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            u.SetForegroundWindow(hwnd)
            time.sleep(0.15)
            if u.GetForegroundWindow() != hwnd:
                raise PlatformError("Windows refused to bring that window to the front")
        elif action == "minimize":
            u.ShowWindow(hwnd, SW_MINIMIZE)
        elif action == "maximize":
            u.ShowWindow(hwnd, SW_MAXIMIZE)
        elif action == "restore":
            u.ShowWindow(hwnd, SW_RESTORE)
        elif action == "move_resize":
            if not rect:
                raise PlatformError("move_resize needs x, y, width and height")
            u.ShowWindow(hwnd, SW_RESTORE)
            if not u.MoveWindow(hwnd, rect[0], rect[1], rect[2], rect[3], True):
                raise PlatformError("Windows refused to move that window")
        elif action == "close":
            u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        else:
            raise PlatformError(f"unknown window action '{action}'")

    # ── Clipboard ───────────────────────────────────────────────────────────
    def clipboard_read(self) -> str:
        return _powershell("Get-Clipboard -Raw").rstrip("\r\n")

    def clipboard_write(self, text: str) -> None:
        _powershell("Set-Clipboard -Value $env:JARVIS_CLIPBOARD", env={"JARVIS_CLIPBOARD": text})

    # ── Screen ──────────────────────────────────────────────────────────────
    def screenshot(self, active_window: bool = False) -> bytes:
        try:
            return super().screenshot(active_window)
        except NotSupported:
            pass
        # Pillow missing: capture the virtual screen with .NET instead.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "shot.png"
            _powershell(
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
                "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size);"
                "$bmp.Save($env:JARVIS_OUT,[System.Drawing.Imaging.ImageFormat]::Png)",
                env={"JARVIS_OUT": str(out)},
            )
            return out.read_bytes()

    def display_info(self) -> DisplayInfo:
        gsm = self._user32.GetSystemMetrics
        return DisplayInfo(
            monitors=gsm(SM_CMONITORS),
            primary_width=gsm(SM_CXSCREEN),
            primary_height=gsm(SM_CYSCREEN),
        )

    def brightness_get(self) -> int:
        out = _powershell(
            "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop).CurrentBrightness"
        ).strip().splitlines()
        if not out:
            raise NotSupported("this display doesn't expose brightness control (common on external monitors)")
        return int(out[0])

    def brightness_set(self, percent: int) -> None:
        _powershell(
            "$m=Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop;"
            "Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
            "-Arguments @{Brightness=[byte]$env:JARVIS_BRIGHTNESS;Timeout=[uint32]0} | Out-Null",
            env={"JARVIS_BRIGHTNESS": str(int(percent))},
        )

    # ── Audio ───────────────────────────────────────────────────────────────
    def _endpoint(self) -> Any:
        try:
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except ImportError as exc:
            raise NotSupported("precise volume control needs pycaw: pip install pycaw") from exc
        speakers = AudioUtilities.GetSpeakers()
        interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return interface.QueryInterface(IAudioEndpointVolume)

    def volume_get(self) -> dict[str, Any]:
        endpoint = self._endpoint()
        return {"percent": round(endpoint.GetMasterVolumeLevelScalar() * 100), "muted": bool(endpoint.GetMute())}

    def volume_set(self, percent: int) -> None:
        self._endpoint().SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100, None)

    def _tap(self, vk: int, times: int = 1) -> None:
        for _ in range(times):
            self._user32.keybd_event(vk, 0, 0, 0)
            self._user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def volume_step(self, up: bool, steps: int = 5) -> None:
        # Each media-key press moves the Windows volume by 2 %.
        self._tap(VK_VOLUME_UP if up else VK_VOLUME_DOWN, max(1, steps))

    def set_mute(self, muted: bool | None) -> None:
        try:
            endpoint = self._endpoint()
            endpoint.SetMute(int(not endpoint.GetMute()) if muted is None else int(muted), None)
        except NotSupported:
            if muted is not None:
                raise NotSupported("setting an explicit mute state needs pycaw; I can only toggle mute") from None
            self._tap(VK_VOLUME_MUTE)

    def audio_devices(self) -> list[dict[str, Any]]:
        try:
            from pycaw.pycaw import AudioUtilities

            return [{"name": d.FriendlyName, "id": d.id, "state": str(d.state)}
                    for d in AudioUtilities.GetAllDevices() if d.FriendlyName]
        except ImportError:
            raw = _powershell("Get-CimInstance Win32_SoundDevice | Select-Object Name,Status | ConvertTo-Json -Compress")
            data = json.loads(raw) if raw.strip() else []
            return [{"name": d.get("Name"), "status": d.get("Status")} for d in (data if isinstance(data, list) else [data])]

    # ── Files / URLs ────────────────────────────────────────────────────────
    def open_path(self, path: Path) -> None:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            raise PlatformError(f"Windows could not open it: {exc.strerror or exc}") from exc

    def reveal_path(self, path: Path) -> None:
        subprocess.Popen(["explorer.exe", f"/select,{path}"])

    def trash(self, path: Path) -> bool:
        if super().trash(path):
            return True
        method = "DeleteDirectory" if path.is_dir() else "DeleteFile"
        _powershell(
            "Add-Type -AssemblyName Microsoft.VisualBasic;"
            f"[Microsoft.VisualBasic.FileIO.FileSystem]::{method}($env:JARVIS_PATH,'OnlyErrorDialogs','SendToRecycleBin')",
            env={"JARVIS_PATH": str(path)},
        )
        return True
