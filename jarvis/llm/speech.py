"""Turn a token stream into speakable sentences as early as possible.

LLM tokens → SentenceChunker → speech-safe sentences → TTS queue. Sentence 1
is spoken while sentence 2 is still being generated.
"""

from __future__ import annotations

import re

_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "approx", "no", "fig"}
_BOUNDARY = re.compile(r"([.!?…]+[\"')\]]*)(\s+)")
_SOFT_BOUNDARY = re.compile(r"([,;:—])(\s+)")
_SOFT_LIMIT = 220

_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)")
_URL = re.compile(r"https?://\S+")
_CITATION = re.compile(r"\[\d+(?:[,\s]*\d+)*\]")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F000-\U0001F02F]")


def speech_text(text: str) -> str:
    """Strip markdown, URLs and citation markers so TTS sounds natural."""
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _CITATION.sub("", text)
    text = _EMOJI.sub("", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    text = re.sub(r"^\s{0,3}(#{1,6}|[-*•]|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    text = text.replace("|", ", ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,")


class SentenceChunker:
    """Accumulates streamed text and yields complete, speakable sentences."""

    def __init__(self) -> None:
        self._buffer = ""
        self._in_code = False
        self.code_skipped = False

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        out: list[str] = []
        while True:
            fence = self._buffer.find("```")
            if self._in_code:
                if fence == -1:
                    # Keep only a tail in case a fence is split across deltas.
                    self._buffer = self._buffer[-2:]
                    return out
                self._buffer = self._buffer[fence + 3:]
                self._in_code = False
                continue
            head = self._buffer if fence == -1 else self._buffer[:fence]
            sentences, rest = self._split(head)
            out.extend(sentences)
            if fence == -1:
                self._buffer = rest
                return out
            if rest.strip():
                out.append(rest)
            self._buffer = self._buffer[fence + 3:]
            self._in_code = True
            self.code_skipped = True

    def _split(self, text: str) -> tuple[list[str], str]:
        sentences: list[str] = []
        start = 0
        for match in _BOUNDARY.finditer(text):
            candidate = text[start : match.end(1)]
            word = re.findall(r"([\w.]+)[.!?…]+[\"')\]]*$", candidate)
            if word and word[0].lower().rstrip(".") in _ABBREVIATIONS:
                continue
            # Short openers ("Certainly.") are emitted immediately too — they cut perceived latency.
            sentences.append(candidate)
            start = match.end()
        rest = text[start:]
        # Split on newlines (list items, paragraphs).
        while "\n" in rest:
            line, rest = rest.split("\n", 1)
            if line.strip():
                sentences.append(line)
        if len(rest) > _SOFT_LIMIT:
            soft = list(_SOFT_BOUNDARY.finditer(rest))
            if soft:
                cut = soft[-1].end(1)
                sentences.append(rest[:cut])
                rest = rest[soft[-1].end():]
        return [s for s in (speech_text(s) for s in sentences) if s], rest

    def flush(self) -> list[str]:
        if self._in_code:
            self._buffer = ""
            return []
        rest, self._buffer = self._buffer, ""
        cleaned = speech_text(rest)
        return [cleaned] if cleaned else []
