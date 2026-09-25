"""Tool registry with automatic discovery of `jarvis.tools.builtin` modules."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING, Any

from jarvis.tools.base import Tool

if TYPE_CHECKING:
    from jarvis.core.services import Services

log = logging.getLogger(__name__)

# Keywords that pull a tool category into the model's tool list for a request.
# Keeping the list focused cuts prompt tokens (latency, cost, rate limits) and
# improves tool-choice accuracy.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "system": ("cpu", "ram", "memory usage", "using", "disk", "storage", "gpu", "battery", "uptime", "process",
               "slow", "performance", "system", "network", "internet connection", "wifi", "os ", "temperature"),
    "apps": ("open", "launch", "start", "close", "quit", "app", "application", "program", "running", "fire up",
             "spotify", "chrome", "code", "browser", "focus", "switch to"),
    "windows": ("window", "minimi", "maximi", "resize", "move", "focus", "switch to", "snap", "tile"),
    "input": ("type", "press", "click", "keyboard", "mouse", "scroll", "hotkey", "shortcut"),
    "clipboard": ("clipboard", "copy", "paste", "copied"),
    "screen": ("screen", "screenshot", "see", "looking at", "display", "visible", "window shows", "what's on"),
    "audio": ("volume", "mute", "unmute", "louder", "quieter", "sound", "speaker", "headphone", "microphone", "audio"),
    "display": ("brightness", "brighter", "dimmer", "monitor", "resolution", "display"),
    "files": ("file", "folder", "directory", "document", "docs", "pdf", "docx", "txt", "download", "desktop",
              "rename", "delete", "move", "copy", "organi", "save", "write", "read", "project", "screenshot",
              "photo", "picture", "note", "summari", "csv", "spreadsheet", "~", ":\\", "/"),
    "web": ("search", "look up", "google", "web", "internet", "online", "news", "latest", "current", "price",
            "research", "compare", "best", "review", "website", "url", "http", "weather", "umbrella", "rain",
            "forecast", "who is", "what is", "when", "today", "find"),
    "memory": ("remember", "forget", "memory", "memories", "prefer", "favourite", "favorite", "know about me",
               "my name", "recall"),
    "tasks": ("task", "background", "working on", "research", "organi", "cancel", "stop", "progress", "status",
              "continue", "compare", "plan", "before"),
    "reminders": ("remind", "reminder", "timer", "alarm", "schedule", "in ", "at ", "tomorrow", "tonight"),
    "diagnostics": ("diagnos", "tools", "capabilit", "why did", "fail", "error", "health", "what can you"),
}
ALWAYS_INCLUDED = {"memory", "tasks", "web"}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def load_builtin(self) -> None:
        from jarvis.tools import builtin

        for module_info in pkgutil.iter_modules(builtin.__path__):
            module = importlib.import_module(f"{builtin.__name__}.{module_info.name}")
            for tool_cls in getattr(module, "TOOLS", []):
                self.register(tool_cls())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def is_enabled(self, tool: Tool, services: Services) -> tuple[bool, str]:
        perms = services.settings.permissions
        if tool.name in perms.disabled_tools:
            return False, "disabled in configuration (JARVIS_DISABLED_TOOLS)"
        gate = {
            "web": (perms.allow_web, "JARVIS_ALLOW_WEB"),
            "vision": (perms.allow_vision, "JARVIS_ALLOW_VISION"),
            "input": (perms.allow_input_control, "JARVIS_ALLOW_INPUT_CONTROL"),
            "clipboard": (perms.allow_clipboard, "JARVIS_ALLOW_CLIPBOARD"),
        }.get(tool.permission or "")
        if gate and not gate[0]:
            return False, f"not permitted — enable with {gate[1]}=true"
        return tool.available(services)

    def enabled(self, services: Services) -> list[Tool]:
        return [t for t in self._tools.values() if self.is_enabled(t, services)[0]]

    def select(self, services: Services, text: str, sticky: set[str] | None = None) -> tuple[list[Tool], set[str]]:
        """Choose the tools relevant to `text` (plus categories used recently)."""
        lowered = f" {text.lower()} "
        categories = {cat for cat, words in CATEGORY_KEYWORDS.items() if any(w in lowered for w in words)}
        categories |= ALWAYS_INCLUDED | (sticky or set())
        tools = [t for t in self.enabled(services) if t.category in categories]
        return tools, categories

    def schemas(self, tools: list[Tool]) -> list[dict[str, Any]]:
        return [t.schema() for t in tools]

    def describe(self, services: Services) -> list[dict[str, Any]]:
        rows = []
        for tool in self._tools.values():
            enabled, reason = self.is_enabled(tool, services)
            rows.append(
                {
                    "name": tool.name,
                    "category": tool.category,
                    "description": tool.description,
                    "risk": tool.risk.value,
                    "data_boundary": tool.data_boundary,
                    "enabled": enabled,
                    "reason": reason,
                }
            )
        return rows
