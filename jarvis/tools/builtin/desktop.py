"""Windows, keyboard/mouse, clipboard, audio and display control."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from jarvis.api.schemas import RiskLevel
from jarvis.tools.base import RiskAssessment, Tool, ToolContext, ToolError, ToolResult

# ── Windows ──────────────────────────────────────────────────────────────────


class ListWindows(Tool):
    name = "list_windows"
    description = "List open windows with their titles, apps, handles, and which one is focused."
    display_name = "Checking open windows"
    category = "windows"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        windows = await ctx.platform.call(ctx.platform.windows)
        return ToolResult.success(f"{len(windows)} open windows", {"windows": [w.to_dict() for w in windows[:60]]},
                                  simulated=ctx.platform.simulated)


class WindowInput(BaseModel):
    title_contains: str | None = Field(None, description="Part of the window title or app name")
    handle: str | None = Field(None, description="Exact handle from list_windows (preferred when known)")
    action: Literal["focus", "minimize", "maximize", "restore", "move_resize"]
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None


class WindowControl(Tool):
    name = "window_control"
    description = "Focus, minimise, maximise, restore, or move/resize a window."
    display_name = "Arranging windows"
    category = "windows"
    Input = WindowInput

    async def execute(self, args: WindowInput, ctx: ToolContext) -> ToolResult:
        windows = await ctx.platform.call(ctx.platform.windows)
        if args.handle:
            matches = [w for w in windows if w.handle == args.handle]
        elif args.title_contains:
            needle = args.title_contains.lower()
            matches = [w for w in windows if needle in w.title.lower() or needle == w.app.lower()]
        else:
            raise ToolError("give either a handle or part of the window title", kind="invalid_arguments")
        if not matches:
            raise ToolError("no matching window is open", kind="not_found")
        if len(matches) > 1 and not args.handle:
            return ToolResult.failure("several windows match — which one?", kind="ambiguous",
                                      data={"candidates": [{"handle": w.handle, "title": w.title} for w in matches[:8]]})
        window = matches[0]
        rect = None
        if args.action == "move_resize":
            if None in (args.x, args.y, args.width, args.height):
                raise ToolError("move_resize needs x, y, width and height", kind="invalid_arguments")
            rect = (args.x, args.y, args.width, args.height)
        await ctx.platform.call(ctx.platform.window_action, window.handle, args.action, rect)
        return ToolResult.success(f"{args.action.replace('_', '/')} applied to '{window.title}'",
                                  {"handle": window.handle}, simulated=ctx.platform.simulated)

    async def verify(self, args: WindowInput, result: ToolResult, ctx: ToolContext) -> bool | None:
        if args.action != "focus" or result.simulated:
            return None
        active = await ctx.platform.call(ctx.platform.active_window)
        return bool(active and active.handle == result.data["handle"])


# ── Keyboard / mouse (opt-in) ────────────────────────────────────────────────


class TypeInput(BaseModel):
    text: str = Field(max_length=5000)


class TypeText(Tool):
    name = "type_text"
    description = "Type text into the currently focused window."
    display_name = "Typing"
    category = "input"
    permission = "input"
    Input = TypeInput
    risk = RiskLevel.MEDIUM

    async def assess(self, args: TypeInput, ctx: ToolContext) -> RiskAssessment:
        active = await ctx.platform.call(ctx.platform.active_window)
        target = active.title if active else "the focused window"
        return RiskAssessment(RiskLevel.MEDIUM, f"Type {len(args.text)} characters into '{target}'?",
                              [args.text[:200]], needs_confirmation=len(args.text) > 200 or "\n" in args.text)

    async def execute(self, args: TypeInput, ctx: ToolContext) -> ToolResult:
        await ctx.platform.call(ctx.platform.type_text, args.text)
        return ToolResult.success(f"Typed {len(args.text)} characters", simulated=ctx.platform.simulated)


_DANGEROUS_COMBOS = {("alt", "f4"), ("ctrl", "alt", "delete"), ("win", "l"), ("ctrl", "w"), ("ctrl", "shift", "w")}


class KeysInput(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=4, description="e.g. ['ctrl','c'] or ['enter']")


class PressKeys(Tool):
    name = "press_keys"
    description = "Press a key or keyboard shortcut in the focused window."
    display_name = "Pressing keys"
    category = "input"
    permission = "input"
    Input = KeysInput
    risk = RiskLevel.MEDIUM

    async def assess(self, args: KeysInput, ctx: ToolContext) -> RiskAssessment:
        combo = tuple(k.lower() for k in args.keys)
        if combo in _DANGEROUS_COMBOS:
            return RiskAssessment(RiskLevel.MEDIUM, f"Press {'+'.join(args.keys)}? It may close or lock something.",
                                  needs_confirmation=True)
        return RiskAssessment(RiskLevel.MEDIUM)

    async def execute(self, args: KeysInput, ctx: ToolContext) -> ToolResult:
        await ctx.platform.call(ctx.platform.press_keys, [k.lower() for k in args.keys])
        return ToolResult.success(f"Pressed {'+'.join(args.keys)}", simulated=ctx.platform.simulated)


class MouseInput(BaseModel):
    action: Literal["move", "click", "double_click", "scroll"]
    x: int | None = None
    y: int | None = None
    button: Literal["left", "right", "middle"] = "left"
    amount: int = Field(0, description="Scroll amount; positive = up")


class MouseAction(Tool):
    name = "mouse_action"
    description = "Move, click, double-click or scroll the mouse at screen coordinates."
    display_name = "Using the mouse"
    category = "input"
    permission = "input"
    Input = MouseInput
    risk = RiskLevel.MEDIUM

    async def execute(self, args: MouseInput, ctx: ToolContext) -> ToolResult:
        await ctx.platform.call(ctx.platform.mouse, args.action, args.x, args.y, args.button, args.amount)
        return ToolResult.success(f"Mouse {args.action.replace('_', ' ')} done", simulated=ctx.platform.simulated)


# ── Clipboard ────────────────────────────────────────────────────────────────


class ClipboardRead(Tool):
    name = "clipboard_read"
    description = "Read the text currently on the clipboard."
    display_name = "Reading clipboard"
    category = "clipboard"
    permission = "clipboard"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        text = await ctx.platform.call(ctx.platform.clipboard_read)
        if not text:
            return ToolResult.success("The clipboard is empty (or holds non-text content)", {"text": ""})
        return ToolResult.success(f"Clipboard holds {len(text)} characters", {"text": text[:8000]},
                                  simulated=ctx.platform.simulated)


class ClipboardWriteInput(BaseModel):
    text: str = Field(max_length=100_000)


class ClipboardWrite(Tool):
    name = "clipboard_write"
    description = "Put text on the clipboard."
    display_name = "Copying to clipboard"
    category = "clipboard"
    permission = "clipboard"
    Input = ClipboardWriteInput

    async def execute(self, args: ClipboardWriteInput, ctx: ToolContext) -> ToolResult:
        await ctx.platform.call(ctx.platform.clipboard_write, args.text)
        return ToolResult.success(f"Copied {len(args.text)} characters to the clipboard",
                                  simulated=ctx.platform.simulated)

    async def verify(self, args: ClipboardWriteInput, result: ToolResult, ctx: ToolContext) -> bool | None:
        current = await ctx.platform.call(ctx.platform.clipboard_read)
        return current.replace("\r\n", "\n").strip() == args.text.replace("\r\n", "\n").strip()


# ── Audio ────────────────────────────────────────────────────────────────────


class VolumeInput(BaseModel):
    action: Literal["get", "set", "up", "down", "mute", "unmute", "toggle_mute"]
    percent: int | None = Field(None, ge=0, le=100, description="Target volume for 'set'")
    steps: int = Field(5, ge=1, le=25, description="Size of up/down change (each step ≈ 2%)")


class VolumeControl(Tool):
    name = "volume_control"
    description = "Get or change the system volume, or mute/unmute."
    display_name = "Adjusting volume"
    category = "audio"
    Input = VolumeInput

    async def execute(self, args: VolumeInput, ctx: ToolContext) -> ToolResult:
        platform = ctx.platform
        if args.action == "get":
            state = await platform.call(platform.volume_get)
            return ToolResult.success(f"Volume is {state.get('percent')}%{' (muted)' if state.get('muted') else ''}",
                                      state, simulated=platform.simulated)
        if args.action == "set":
            if args.percent is None:
                raise ToolError("'set' needs a percent", kind="invalid_arguments")
            await platform.call(platform.volume_set, args.percent)
        elif args.action in {"up", "down"}:
            await platform.call(platform.volume_step, args.action == "up", args.steps)
        else:
            await platform.call(platform.set_mute, {"mute": True, "unmute": False, "toggle_mute": None}[args.action])
        try:
            state = await platform.call(platform.volume_get)
        except Exception:
            state = None  # precise read-back unavailable (e.g. no pycaw) — report unverified
        summary = {"set": f"Volume set to {args.percent}%", "up": "Volume up", "down": "Volume down",
                   "mute": "Muted", "unmute": "Unmuted", "toggle_mute": "Mute toggled"}[args.action]
        if state and state.get("percent") is not None:
            summary += f" (now {state['percent']}%{', muted' if state.get('muted') else ''})"
        verified = None
        if state and args.action == "set":
            verified = abs((state.get("percent") or 0) - (args.percent or 0)) <= 2
        elif state and args.action in {"mute", "unmute"}:
            verified = bool(state.get("muted")) == (args.action == "mute")
        return ToolResult.success(summary, state, verified=verified, simulated=platform.simulated)


class AudioDevices(Tool):
    name = "audio_devices"
    description = "List audio input/output devices."
    display_name = "Checking audio devices"
    category = "audio"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        devices = await ctx.platform.call(ctx.platform.audio_devices)
        return ToolResult.success(f"{len(devices)} audio devices", {"devices": devices},
                                  simulated=ctx.platform.simulated)


# ── Display ──────────────────────────────────────────────────────────────────


class DisplayInfoTool(Tool):
    name = "display_info"
    description = "Monitor count and resolution, plus current brightness where available."
    display_name = "Checking displays"
    category = "display"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        info = await ctx.platform.call(ctx.platform.display_info)
        data = {"monitors": info.monitors, "primary_resolution": f"{info.primary_width}x{info.primary_height}",
                **info.details}
        try:
            data["brightness_percent"] = await ctx.platform.call(ctx.platform.brightness_get)
        except Exception:
            data["brightness_percent"] = None
        return ToolResult.success(f"{info.monitors} display(s), primary {data['primary_resolution']}", data,
                                  simulated=ctx.platform.simulated)


class BrightnessInput(BaseModel):
    percent: int = Field(ge=5, le=100)


class SetBrightness(Tool):
    name = "set_brightness"
    description = "Set the built-in display's brightness (laptops; most external monitors don't support this)."
    display_name = "Adjusting brightness"
    category = "display"
    Input = BrightnessInput

    async def execute(self, args: BrightnessInput, ctx: ToolContext) -> ToolResult:
        await ctx.platform.call(ctx.platform.brightness_set, args.percent)
        return ToolResult.success(f"Brightness set to {args.percent}%", simulated=ctx.platform.simulated)

    async def verify(self, args: BrightnessInput, result: ToolResult, ctx: ToolContext) -> bool | None:
        current = await ctx.platform.call(ctx.platform.brightness_get)
        return abs(current - args.percent) <= 3


TOOLS = [ListWindows, WindowControl, TypeText, PressKeys, MouseAction, ClipboardRead, ClipboardWrite,
         VolumeControl, AudioDevices, DisplayInfoTool, SetBrightness]
