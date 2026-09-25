"""Modular prompt architecture.

The system prompt is assembled from independent sections so each concern can
be edited on its own:

    IDENTITY · PERSONALITY · SAFETY RULES · TOOL USE · USER PROFILE · MEMORY ·
    CURRENT TASKS · SYSTEM CONTEXT · CONVERSATION SUMMARY · OUTPUT REQUIREMENTS
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from jarvis.api.schemas import ResponseMode

_DIR = Path(__file__).parent


@cache
def load_section(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


@cache
def _modes() -> dict[str, str]:
    modes: dict[str, str] = {}
    current = None
    for line in load_section("modes").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            modes[current] = ""
        elif current:
            modes[current] += line + "\n"
    return {k: v.strip() for k, v in modes.items()}


@dataclass
class PromptContext:
    honorific: str = "sir"
    user_name: str = ""
    memories: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    system_context: list[str] = field(default_factory=list)
    conversation_summary: str = ""
    mode: ResponseMode = ResponseMode.CONVERSATIONAL
    voice: bool = False
    extra: list[str] = field(default_factory=list)


def build_system_prompt(ctx: PromptContext) -> str:
    sections = [
        load_section("identity"),
        load_section("personality").replace("{honorific}", ctx.honorific or "sir"),
        load_section("safety"),
        load_section("tool_use"),
    ]
    profile = [f"Preferred form of address: {ctx.honorific}."]
    if ctx.user_name:
        profile.append(f"User's name: {ctx.user_name}.")
    sections.append("# User profile\n" + " ".join(profile))
    if ctx.memories:
        sections.append(
            "# Relevant memories (retrieved from long-term memory; may be outdated)\n"
            + "\n".join(f"- {m}" for m in ctx.memories)
        )
    if ctx.tasks:
        sections.append("# Background tasks\n" + "\n".join(f"- {t}" for t in ctx.tasks))
    if ctx.system_context:
        sections.append("# Current context\n" + "\n".join(f"- {c}" for c in ctx.system_context))
    if ctx.conversation_summary:
        sections.append("# Earlier in this conversation (summary)\n" + ctx.conversation_summary)
    sections.extend(ctx.extra)
    sections.append(load_section("output_voice" if ctx.voice else "output_text"))
    sections.append(_modes().get(ctx.mode.value, ""))
    return "\n\n".join(s for s in sections if s)
