"""Wake-word detection on transcripts, tolerant of common speech-to-text errors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# Frequent mis-transcriptions of "Jarvis".
_KNOWN_VARIANTS = {
    "jarvis", "jarvis's", "jarvice", "jervis", "jarves", "jarvus", "jarviss", "jarv", "jarvi", "javis", "jaavis",
    "charvis", "harvis", "garvis", "jarbis", "jarwis", "dżarwis", "jarvisz", "travis", "jarvey", "jarvie",
}
_LEADERS = {"hey", "hi", "hello", "ok", "okay", "yo", "oi", "right", "so", "and", "um", "uh", "hej"}
_WORD = re.compile(r"[\w'’]+")


@dataclass
class WakeResult:
    detected: bool
    remainder: str = ""


def _similar(token: str, core: str) -> bool:
    if token in _KNOWN_VARIANTS and core == "jarvis":
        return True
    if token == core:
        return True
    if len(token) < 4:
        return False
    return SequenceMatcher(None, token, core).ratio() >= 0.8


class WakeWordDetector:
    def __init__(self, wake_words: list[str]) -> None:
        # The distinctive word of each phrase ("hey jarvis" → "jarvis").
        self._cores = {phrase.split()[-1].lower() for phrase in wake_words if phrase.strip()} or {"jarvis"}

    def _is_wake(self, token: str) -> bool:
        return any(_similar(token, core) for core in self._cores)

    def check(self, text: str) -> WakeResult:
        tokens = list(_WORD.finditer(text))
        lowered = [t.group(0).lower().replace("’", "'") for t in tokens]
        # "Jarvis, open Chrome" / "Hey Jarvis open Chrome"
        for index in range(min(3, len(tokens))):
            if self._is_wake(lowered[index]) and all(w in _LEADERS for w in lowered[:index]):
                remainder = text[tokens[index].end():].lstrip(" ,.!?:;-—")
                return WakeResult(True, remainder.strip())
        # "Open Chrome, Jarvis."
        if tokens and self._is_wake(lowered[-1]):
            remainder = text[: tokens[-1].start()].rstrip(" ,.!?:;-—")
            return WakeResult(True, remainder.strip())
        return WakeResult(False, text.strip())
