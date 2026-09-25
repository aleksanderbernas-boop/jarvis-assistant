"""Lightweight intent routing.

Simple, unambiguous commands ("what time is it", "open Chrome", "mute",
"remind me in 10 minutes to…") are handled deterministically — no LLM call,
near-zero latency. Research and multi-step jobs go to background agents.
Everything else goes to the LLM with a response mode and model tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from jarvis.api.schemas import ResponseMode
from jarvis.core.timeparse import parse_duration, parse_when
from jarvis.llm.models import ModelTier


class IntentKind(StrEnum):
    STOP = "stop"
    CANCEL_TASK = "cancel_task"
    WORKING_ON = "working_on"
    TIME = "time"
    DATE = "date"
    GREETING = "greeting"
    THANKS = "thanks"
    OPEN_APP = "open_app"
    CLOSE_APP = "close_app"
    SYSTEM_STATUS = "system_status"
    TOP_PROCESSES = "top_processes"
    REMEMBER = "remember"
    FORGET_LAST = "forget_last"
    FORGET = "forget"
    RECALL_ALL = "recall_all"
    CLEAR_MEMORY = "clear_memory"
    TIMER = "timer"
    REMINDER = "reminder"
    DIAGNOSTICS = "diagnostics"
    LIST_TOOLS = "list_tools"
    WEATHER = "weather"
    VOLUME = "volume"
    RESEARCH = "research"
    AGENT_TASK = "agent_task"
    LLM = "llm"


@dataclass
class Intent:
    kind: IntentKind
    text: str
    mode: ResponseMode = ResponseMode.CONVERSATIONAL
    tier: ModelTier = ModelTier.GENERAL
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def deterministic(self) -> bool:
        return self.kind not in {IntentKind.LLM}


_POLITE_PREFIX = re.compile(
    r"^(?:(?:hey |ok |okay )?jarvis[,.!]?\s+|(?:and|also|so|now|then|oh|ok|okay|right|alright|um|uh)[,.]?\s+|would you mind\s+|(?:could|can|would|will) you(?: please)?(?: kindly)?\s+|"
    r"please\s+|kindly\s+|would you mind\s+|i(?:'d| would) like you to\s+|i want you to\s+|i need you to\s+|"
    r"go ahead and\s+|let's\s+|let us\s+|quickly\s+|just\s+)+",
    re.IGNORECASE,
)
_POLITE_SUFFIX = re.compile(r"(?:[,\s]+(?:please|for me|thanks|thank you|jarvis|now|real quick))+\s*$", re.I)


def normalize(text: str) -> str:
    """Strip politeness and punctuation so "Could you open Chrome, please?" → "open chrome"."""
    cleaned = text.strip().strip("\"'")
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _POLITE_PREFIX.sub("", cleaned).strip()
        cleaned = _POLITE_SUFFIX.sub("", cleaned).strip()
        cleaned = cleaned.rstrip("?.! ").strip()
    return cleaned


_STOP = re.compile(
    r"^(stop|stop it|stop talking|stop that|be quiet|quiet|shut up|silence|enough|that's enough|that will do|"
    r"never ?mind|cancel|cancel that|forget it|hush|shh+|pause|hold on|wait|abort|halt)$", re.I)
_CANCEL_TASK = re.compile(
    r"^(?:stop|cancel|abort|kill|end|terminate|halt)\s+(?:the |that |this |all |my |your )?(?:(?:background|running)\s+)?"
    r"(?P<what>.*?\b(?:tasks?|research|job|jobs|work|download|search)\b.*)$", re.I)
_WORKING_ON = re.compile(
    r"(what are you (working on|doing|up to)|what('s| is) (running|in progress)|(any|active|current|running) tasks|"
    r"how('s| is) (the |my |that )?(research|task|job|work)( going)?|status of (the |my )?(tasks?|research)|"
    r"what tasks|are you (still )?working|progress (report|update))", re.I)
_TIME = re.compile(r"^(what('s| is) the time|what time is it|(tell me )?the time|current time|time)( now| right now)?$", re.I)
_DATE = re.compile(r"^(what('s| is) (the |today's )?date|what day is (it|today)|today's date|what's today)( today)?$", re.I)
_GREETING = re.compile(r"^(hi|hello|hey|hiya|good (morning|afternoon|evening)|morning|evening|yo|you there|are you there|"
                       r"jarvis)$", re.I)
_THANKS = re.compile(r"^(thanks|thank you|cheers|much appreciated|great|perfect|nice|good job|well done)( jarvis)?$", re.I)
_OPEN = re.compile(
    r"^(?:open|launch|start|run|fire up|bring up|boot up|load up|pull up|get|start up|open up)\s+(?:up\s+)?"
    r"(?P<app>.+?)(?:\s+(?:running|up|going|for me|please|app|application))*$", re.I)
_CLOSE = re.compile(r"^(?:close|quit|exit|kill|shut down|shut|terminate|end)\s+(?P<app>.+?)(?:\s+(?:app|application|down))?$",
                    re.I)
_NOT_APP = re.compile(
    r"(\band\b|,|\bthen\b|http|www\.|\.com|\bfile\b|\bfolder\b|\bdocument\b|\bwindow\b|\btab\b|\bpage\b|"
    r"[\\/]|\.(?:pdf|docx?|txt|xlsx?|pptx?|png|jpe?g|mp[34])\b|\bcomputer\b|\bpc\b|\bsystem\b|\blaptop\b|"
    r"\bsearch\b|\bwebsite\b|\bsite\b|\bproject\b|\bthe (?:first|second|last)\b|\bit\b|\bthat\b|\bthis\b)", re.I)
_SYSTEM_STATUS = re.compile(
    r"^((check |run a |give me a |show me the |show )?(system|systems) (status|check|report)|status report|"
    r"how('s| is) (my |the )?(system|computer|pc|machine)( doing| running)?|check (the |my )?system( status)?)$", re.I)
_TOP = re.compile(
    r"what('s| is)( using| eating| hogging| taking up)( (the most|most of|all|so much|up))?( (of )?(my|the))? "
    r"(?P<what>memory|ram|cpu|processor)", re.I)
_REMEMBER = re.compile(r"^(?:remember|note|keep in mind|don't forget)(?: that)?\s+(?P<fact>.{3,})$", re.I)
_PREFERENCE_WORDS = re.compile(r"\b(prefer|like|love|hate|don't like|favou?rite|always|never|usually|rather)\b", re.I)
_FORGET_LAST = re.compile(r"^forget (that|it|this|what i (just )?said)$", re.I)
_FORGET = re.compile(r"^forget (?:that |about |the fact that )?(?P<what>.{3,})$", re.I)
_RECALL = re.compile(
    r"^(what do you (remember|know) about me|what have you remembered|(list|show)( me)? (your|my|all) memories|"
    r"what('s| is) in your memory|what do you remember)$", re.I)
_CLEAR = re.compile(r"^(clear|delete|wipe|erase|reset)( all)?( of)? (my|your|the) memor(y|ies)$", re.I)
_TIMER = re.compile(r"^(?:set|start)?\s*(?:a|an)?\s*timer(?: for)?\s+(?P<dur>.+)$|^(?P<dur2>.+?)\s+timer$", re.I)
_REMIND = re.compile(r"^remind me\s+(?P<rest>.+)$", re.I)
_DIAG = re.compile(r"^((run |run a |perform |start )?(full |self[- ]?)?diagnostics?( check| report)?|system check|"
                   r"self[- ]?test|how are you (functioning|operating|doing technically))$", re.I)
_TOOLS = re.compile(r"^(what (tools|capabilities) do you have|what can you do|list (your )?(tools|capabilities)|"
                    r"what are your capabilities|help)$", re.I)
_WEATHER = re.compile(
    r"^(?:what(?:'s| is)|how(?:'s| is)|show(?: me)?|tell me|check|get|give me)?\s*(?:the )?"
    r"(?:weather|forecast|weather forecast)(?: like)?(?: (?:today|now|outside|right now|this week|tomorrow))?"
    r"(?: in (?P<place>[\w\s,.'-]+?))?(?: (?:today|now|tomorrow|this week))?$", re.I)
_VOLUME = re.compile(
    r"^(?:(?:turn|put|bring) (?:the )?(?:volume|sound|it) (?P<dir>up|down)|volume (?P<dir2>up|down)|"
    r"(?P<louder>louder|quieter|softer)|(?P<mute>mute|unmute)(?: (?:the )?(?:sound|volume|audio|computer))?|"
    r"(?:set|change) (?:the )?volume to (?P<pct>\d{1,3})(?: ?%| percent)?|volume (?P<pct2>\d{1,3})(?: ?%| percent)?)$",
    re.I)
_RESEARCH = re.compile(
    r"^(?:(?:do (?:some |a )?|conduct (?:some |a )?|start (?:some |a )?)?research(?:ing)?(?: on| into| about)?|"
    r"look into|investigate|dig into|find out everything about|do a deep dive (?:on|into))\s+(?P<topic>.{4,})$", re.I)
_CONTINUE_RESEARCH = re.compile(r"^(?:continue|resume|pick up|carry on with)\s+(?:the |my |our )?(?P<topic>.+?)"
                                r"\s*(?:research|investigation)(?: from .+)?$", re.I)
_AGENT = re.compile(
    r"^(?:organi[sz]e|clean up|tidy(?: up)?|sort out|declutter|archive|back up|consolidate|reorgani[sz]e|"
    r"rename all|group)\s+(?P<goal>.{4,})$", re.I)
_DEADLINE = re.compile(r"\s+(?:before|by|until|no later than)\s+(?P<when>(?:tomorrow |tonight |today )?"
                       r"(?:at )?\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?|tomorrow(?: morning| evening)?|tonight|noon|midnight)\s*$",
                       re.I)
_TECHNICAL = re.compile(r"\b(explain in detail|in depth|in-depth|deep dive|technical|step by step|how does|"
                        r"why does|architecture|algorithm|derive|prove|compare .+ (vs|versus|and) )", re.I)


def _extract_deadline(text: str) -> tuple[str, datetime | None]:
    match = _DEADLINE.search(text)
    if not match:
        return text, None
    parsed = parse_when(match.group("when"))
    if not parsed:
        return text, None
    return text[: match.start()].strip(), parsed.when


class IntentRouter:
    def route(self, raw: str) -> Intent:
        text = normalize(raw)
        lowered = text.lower()
        q, c, o = ResponseMode.QUICK, ResponseMode.CONVERSATIONAL, ResponseMode.OPERATIONAL

        if _STOP.match(lowered):
            return Intent(IntentKind.STOP, text, q)
        match = _CANCEL_TASK.match(lowered)
        if match:
            return Intent(IntentKind.CANCEL_TASK, text, q, params={"query": match.group("what")})
        if _WORKING_ON.search(lowered):
            return Intent(IntentKind.WORKING_ON, text, q)
        if _TIME.match(lowered):
            return Intent(IntentKind.TIME, text, q)
        if _DATE.match(lowered):
            return Intent(IntentKind.DATE, text, q)
        if _GREETING.match(lowered):
            return Intent(IntentKind.GREETING, text, q)
        if _THANKS.match(lowered):
            return Intent(IntentKind.THANKS, text, q)
        if _DIAG.match(lowered):
            return Intent(IntentKind.DIAGNOSTICS, text, q)
        if _TOOLS.match(lowered):
            return Intent(IntentKind.LIST_TOOLS, text, c)
        if _RECALL.match(lowered):
            return Intent(IntentKind.RECALL_ALL, text, c)
        if _CLEAR.match(lowered):
            return Intent(IntentKind.CLEAR_MEMORY, text, q)
        if _FORGET_LAST.match(lowered):
            return Intent(IntentKind.FORGET_LAST, text, q)
        match = _FORGET.match(text)
        if match:
            return Intent(IntentKind.FORGET, text, q, params={"query": match.group("what")})
        match = _REMEMBER.match(text)
        if match:
            fact = match.group("fact").strip()
            kind = "preference" if _PREFERENCE_WORDS.search(fact) else "fact"
            return Intent(IntentKind.REMEMBER, text, q, params={"content": fact, "kind": kind})
        match = _TIMER.match(lowered)
        if match:
            parsed = parse_duration(match.group("dur") or match.group("dur2") or "")
            if parsed and parsed[0].total_seconds() > 0:
                return Intent(IntentKind.TIMER, text, q, params={"seconds": parsed[0].total_seconds(),
                                                                 "label": parsed[1]})
        match = _REMIND.match(text)
        if match:
            found = parse_when(match.group("rest"))
            if found:
                reminder = re.sub(r"^(to|that|about)\s+", "", found.remainder, flags=re.I).strip() or "your reminder"
                return Intent(IntentKind.REMINDER, text, q, params={"when": found.when, "text": reminder})
        if _SYSTEM_STATUS.match(lowered):
            return Intent(IntentKind.SYSTEM_STATUS, text, q)
        match = _TOP.search(lowered)
        if match:
            sort_by = "cpu" if match.group("what") in {"cpu", "processor"} else "memory"
            return Intent(IntentKind.TOP_PROCESSES, text, q, params={"sort_by": sort_by})
        match = _WEATHER.match(lowered)
        if match and "umbrella" not in lowered:
            return Intent(IntentKind.WEATHER, text, q, params={"location": (match.group("place") or "").strip() or None})
        match = _VOLUME.match(lowered)
        if match:
            return Intent(IntentKind.VOLUME, text, q, params=self._volume_params(match))

        match = _CONTINUE_RESEARCH.match(lowered)
        if match:
            topic, deadline = _extract_deadline(match.group("topic"))
            return Intent(IntentKind.RESEARCH, text, o, ModelTier.REASONING,
                          params={"topic": topic, "deadline": deadline, "continue": True})
        match = _RESEARCH.match(text)
        if match:
            topic, deadline = _extract_deadline(match.group("topic"))
            return Intent(IntentKind.RESEARCH, text, o, ModelTier.REASONING,
                          params={"topic": topic, "deadline": deadline, "continue": False})
        match = _AGENT.match(text)
        if match:
            goal, deadline = _extract_deadline(text)
            return Intent(IntentKind.AGENT_TASK, text, o, ModelTier.REASONING,
                          params={"goal": goal, "deadline": deadline})

        match = _OPEN.match(lowered)
        if match and self._app_like(match.group("app")):
            return Intent(IntentKind.OPEN_APP, text, q, params={"name": match.group("app").strip()})
        match = _CLOSE.match(lowered)
        if match and self._app_like(match.group("app")):
            return Intent(IntentKind.CLOSE_APP, text, q, params={"name": match.group("app").strip()})

        if _TECHNICAL.search(lowered) or len(text) > 400:
            return Intent(IntentKind.LLM, text, ResponseMode.TECHNICAL, ModelTier.REASONING)
        if re.match(r"^(open|launch|close|move|copy|delete|rename|create|write|set|turn|play|search|find|show|"
                    r"take|switch|minimi[sz]e|maximi[sz]e|type|press|copy|paste|check)\b", lowered):
            return Intent(IntentKind.LLM, text, o)
        return Intent(IntentKind.LLM, text, c)

    @staticmethod
    def _app_like(name: str) -> bool:
        name = name.strip()
        return 0 < len(name.split()) <= 4 and not _NOT_APP.search(name)

    @staticmethod
    def _volume_params(match: re.Match[str]) -> dict[str, Any]:
        groups = match.groupdict()
        pct = groups.get("pct") or groups.get("pct2")
        if pct:
            return {"action": "set", "percent": min(100, int(pct))}
        if groups.get("mute"):
            return {"action": groups["mute"]}
        direction = groups.get("dir") or groups.get("dir2") or ("up" if groups.get("louder") == "louder" else "down")
        return {"action": direction}
