"""Natural phrasing for deterministic responses — varied, brief, never obsequious."""

from __future__ import annotations

import random
import re
from datetime import datetime

_PHRASES: dict[str, list[str]] = {
    "wake_ack": ["Yes, {h}?", "At your service.", "{H}?", "I'm listening.", "Go ahead."],
    "thanks": ["You're welcome.", "Always a pleasure.", "Anytime, {h}.", "Happy to help."],
    "opening": ["Opening {app}.", "Launching {app}.", "Bringing up {app}."],
    "stopped": ["Stopped.", "Stopping."],
    "nothing_to_stop": ["Nothing's running at the moment."],
    "research_start": ["I'll look into it and let you know when the report's ready.",
                       "I'll take care of it. You'll hear from me when it's done.",
                       "On it — I'll research that in the background."],
    "research_continue": ["Picking up where we left off. I'll report back when it's done."],
    "agent_start": ["I'll handle that in the background.", "Consider it in hand. I'll report back when it's done."],
    "remembered": ["I'll remember that.", "Noted.", "Committed to memory."],
    "memory_updated": ["Noted — I've updated what I had on that."],
    "checking": ["Checking.", "One moment."],
}


def phrase(key: str, honorific: str = "sir", **values: str) -> str:
    template = random.choice(_PHRASES[key])
    return template.format(h=honorific, H=honorific.capitalize(), **values)


def greeting(honorific: str, now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening"
    return random.choice([f"Good {part}, {honorific}.", f"Good {part}. What can I do for you?",
                          f"Good {part}, {honorific}. How can I help?"])


def ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def spoken_size_mb(mb: float) -> str:
    return f"{mb / 1024:.1f} gigabytes" if mb >= 1024 else f"{mb:.0f} megabytes"


_SECOND_PERSON = [(r"\bI am\b", "you are"), (r"\bI'm\b", "you're"), (r"\bI was\b", "you were"),
                  (r"\bI\b", "you"), (r"\bmy\b", "your"), (r"\bme\b", "you"), (r"\bmine\b", "yours"),
                  (r"\bmyself\b", "yourself")]


def spoken_memory(content: str) -> str:
    """'User: I prefer VS Code' → 'you prefer VS Code' when reading memories back."""
    if not content.startswith("User: "):
        return content
    text = content[len("User: "):]
    for pattern, replacement in _SECOND_PERSON:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text
