"""Deterministic natural-language time parsing for reminders, timers and deadlines.

Handles the common forms quickly without an LLM call:
    "in 30 minutes", "in an hour and a half", "in 1h 20m", "at 8 pm", "at 20:15",
    "tomorrow morning", "tomorrow at 9", "tonight", "this evening", "at noon".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "couple": 2, "few": 3,
}
_UNIT_SECONDS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600,
                 "hour": 3600, "d": 86400, "day": 86400}
_PART_OF_DAY = {"morning": 9, "afternoon": 14, "evening": 19, "tonight": 20, "night": 21}

# Digits may be glued to short units ("1h 20m"); number words need a full unit word ("an hour").
_DURATION = re.compile(
    r"(?:(?P<num>\d+(?:[.,]\d+)?)\s*(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)"
    r"|(?P<wnum>a couple of|a few|[a-z\-]+)\s+(?P<wunit>seconds?|secs?|minutes?|mins?|hours?|hrs?|days?))\b"
)
_CLOCK = re.compile(r"\b(?:at\s+)?(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?(?=\s|$|[,.!?])")


@dataclass
class ParsedTime:
    when: datetime
    matched: str       # the text that described the time
    remainder: str     # the rest of the sentence (e.g. the reminder text)


def _to_number(token: str) -> float | None:
    token = token.strip().lower().replace(",", ".")
    if token in {"a couple of"}:
        return 2
    if token in {"a few"}:
        return 3
    try:
        return float(token)
    except ValueError:
        return _NUMBER_WORDS.get(token)


def parse_duration(text: str) -> tuple[timedelta, str] | None:
    """Parse '10 minutes', '1h 30m', 'an hour and a half'. Returns (duration, matched text)."""
    lowered = text.lower()
    total = 0.0
    spans: list[tuple[int, int]] = []
    for match in _DURATION.finditer(lowered):
        value = _to_number(match.group("num") or match.group("wnum"))
        unit = (match.group("unit") or match.group("wunit")).rstrip("s") or "s"
        if value is None:
            continue
        seconds = _UNIT_SECONDS.get(unit) or _UNIT_SECONDS.get(unit[:3]) or _UNIT_SECONDS.get(unit[:1])
        if not seconds:
            continue
        total += value * seconds
        spans.append(match.span())
    if not spans:
        return None
    end = spans[-1][1]
    tail = lowered[end:]
    half = re.match(r"\s+and\s+a\s+half", tail)
    if half:
        last = _DURATION.search(lowered[spans[-1][0]:end])
        last_unit = ((last.group("unit") or last.group("wunit")) if last else "m").rstrip("s")
        total += 0.5 * (_UNIT_SECONDS.get(last_unit) or _UNIT_SECONDS.get(last_unit[:3]) or 60)
        end += half.end()
    return timedelta(seconds=total), text[spans[0][0]:end]


def _clock_time(match: re.Match[str]) -> tuple[int, int] | None:
    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    ampm = (match.group("ampm") or "").replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def parse_when(text: str, now: datetime | None = None) -> ParsedTime | None:
    """Find a point in time in `text`. `now` must be timezone-aware local time."""
    now = now or datetime.now().astimezone()
    lowered = text.lower()

    relative = re.search(r"\bin\s+(.+?)(?=\s+(?:to|that|about|for)\s|$)", lowered)
    if relative:
        parsed = parse_duration(relative.group(1))
        if parsed and parsed[0].total_seconds() > 0:
            start = relative.start()
            matched_end = relative.start(1) + len(parsed[1])
            remainder = (text[:start] + text[matched_end:]).strip()
            return ParsedTime(now + parsed[0], text[start:matched_end], remainder)

    day_offset = 0
    day_match = re.search(r"\b(tomorrow|tonight|today|this (?:morning|afternoon|evening))\b", lowered)
    if day_match and day_match.group(1) == "tomorrow":
        day_offset = 1
    part = re.search(r"\b(morning|afternoon|evening|tonight|night|noon|midday|midnight)\b", lowered)
    clock = None
    for candidate in _CLOCK.finditer(lowered):
        preceding = lowered[max(0, candidate.start() - 4):candidate.start()]
        has_at = candidate.group(0).strip().startswith("at")
        if has_at or candidate.group("ampm") or candidate.group("m") or "at " in preceding:
            clock = candidate
            break

    hour_minute: tuple[int, int] | None = None
    spans: list[tuple[int, int]] = []
    if clock:
        hour_minute = _clock_time(clock)
        spans.append(clock.span())
        if hour_minute and not clock.group("ampm") and hour_minute[0] < 12 and part and part.group(1) in {
            "afternoon", "evening", "tonight", "night"
        }:
            hour_minute = (hour_minute[0] + 12, hour_minute[1])
    elif part:
        word = part.group(1)
        hour_minute = {"noon": (12, 0), "midday": (12, 0), "midnight": (0, 0)}.get(word, (_PART_OF_DAY.get(word, 9), 0))
        if word == "midnight":
            day_offset = max(day_offset, 1)
    if part:
        spans.append(part.span())
    if day_match:
        spans.append(day_match.span())
    if hour_minute is None:
        if day_match and day_match.group(1) == "tomorrow":
            hour_minute = (9, 0)
        else:
            return None

    when = now.replace(hour=hour_minute[0], minute=hour_minute[1], second=0, microsecond=0) + timedelta(days=day_offset)
    if when <= now and day_offset == 0:
        when += timedelta(days=1)
    start = min(s for s, _ in spans)
    end = max(e for _, e in spans)
    prefix = re.search(r"\b(at|by|before|on)\s*$", lowered[:start])
    if prefix:
        start = prefix.start()
    remainder = re.sub(r"\s{2,}", " ", (text[:start] + text[end:])).strip()
    return ParsedTime(when, text[start:end].strip(), remainder)


def humanize_delta(delta: timedelta) -> str:
    seconds = int(round(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " and ".join(parts)


def humanize_time(when: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    clock = when.strftime("%H:%M")
    if when.date() == now.date():
        return f"at {clock}"
    if when.date() == (now + timedelta(days=1)).date():
        return f"tomorrow at {clock}"
    return f"on {when.strftime('%A %d %B')} at {clock}"
