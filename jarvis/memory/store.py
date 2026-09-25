"""Persistent memory.

Kinds:
    preference — durable user preferences ("prefers VS Code")
    fact       — durable facts about the user, their projects, locations
    episodic   — notable past interactions (completed research, finished tasks)

Short-term memory is the session history and working memory is the task
manager; both live elsewhere. Retrieval is BM25 relevance blended with
confidence and recency, so only the few memories that matter reach a prompt.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from jarvis.storage.db import Database

MemoryKind = Literal["preference", "fact", "episodic"]

_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has", "have", "i", "i'm", "in", "is", "it", "its", "me", "my", "of", "on", "or", "our", "so", "that", "the", "their", "them", "then", "there", "these", "they", "this", "to", "too", "was", "we", "were", "what", "when", "where", "which", "who", "will", "with", "you", "your", "do", "does", "did", "not", "can", "could", "should", "would", "about", "into", "just", "also", "than", "very", "really", "please", "jarvis", "remember", "that"]
)
_TOKEN = re.compile(r"[a-z0-9ąćęłńóśźż]+")


def _stem(word: str) -> str:
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


@dataclass
class Memory:
    id: str
    kind: str
    content: str
    source: str
    confidence: float
    created_at: str
    updated_at: str
    key: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    access_count: int = 0
    last_accessed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "content": self.content,
            "source": self.source,
            "confidence": self.confidence,
            "key": self.key,
            "tags": self.tags,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "access_count": self.access_count,
        }


@dataclass
class ScoredMemory:
    memory: Memory
    score: float


def _row_to_memory(row: dict[str, Any]) -> Memory:
    return Memory(
        id=row["id"],
        kind=row["kind"],
        content=row["content"],
        source=row["source"],
        confidence=row["confidence"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        key=row["key"],
        tags=json.loads(row["tags"] or "[]"),
        metadata=json.loads(row["metadata"] or "{}"),
        access_count=row["access_count"],
        last_accessed_at=row["last_accessed_at"],
    )


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class MemoryStore:
    DUPLICATE_THRESHOLD = 0.75

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── Writes ──────────────────────────────────────────────────────────────
    def add(
        self,
        content: str,
        kind: MemoryKind = "fact",
        *,
        source: str = "user_explicit",
        confidence: float = 1.0,
        key: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Memory, bool]:
        """Store a memory, merging with an existing near-duplicate. Returns (memory, created)."""
        content = content.strip()
        now = datetime.now(UTC).isoformat()
        existing = self._find_duplicate(content, kind, key)
        if existing:
            self._db.execute(
                "UPDATE memories SET content = ?, confidence = MAX(confidence, ?), updated_at = ?, "
                "metadata = ?, source = ? WHERE id = ?",
                (content, confidence, now, json.dumps({**existing.metadata, **(metadata or {})}), source, existing.id),
            )
            updated = self.get(existing.id)
            assert updated is not None
            return updated, False
        memory_id = uuid.uuid4().hex[:12]
        self._db.execute(
            "INSERT INTO memories(id, kind, content, key, source, confidence, tags, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, kind, content, key, source, confidence, json.dumps(tags or []),
             json.dumps(metadata or {}), now, now),
        )
        memory = self.get(memory_id)
        assert memory is not None
        return memory, True

    def _find_duplicate(self, content: str, kind: str, key: str | None) -> Memory | None:
        if key:
            row = self._db.query_one("SELECT * FROM memories WHERE key = ? AND kind = ?", (key, kind))
            if row:
                return _row_to_memory(row)
        tokens = set(tokenize(content))
        best: tuple[float, Memory | None] = (0.0, None)
        for row in self._db.query("SELECT * FROM memories WHERE kind = ?", (kind,)):
            similarity = _jaccard(tokens, set(tokenize(row["content"])))
            if similarity > best[0]:
                best = (similarity, _row_to_memory(row))
        return best[1] if best[0] >= self.DUPLICATE_THRESHOLD else None

    def delete(self, memory_id: str) -> bool:
        return self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,)) > 0

    def clear(self, kind: str | None = None) -> int:
        if kind:
            return self._db.execute("DELETE FROM memories WHERE kind = ?", (kind,))
        return self._db.execute("DELETE FROM memories")

    # ── Reads ───────────────────────────────────────────────────────────────
    def get(self, memory_id: str) -> Memory | None:
        row = self._db.query_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return _row_to_memory(row) if row else None

    def entries(self, kind: str | None = None, limit: int = 200) -> list[Memory]:
        if kind:
            rows = self._db.query("SELECT * FROM memories WHERE kind = ? ORDER BY updated_at DESC LIMIT ?",
                                  (kind, limit))
        else:
            rows = self._db.query("SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [_row_to_memory(r) for r in rows]

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM memories")
        return int(row["n"]) if row else 0

    def search(self, query: str, *, limit: int = 5, kinds: list[str] | None = None,
               min_score: float = 0.15, touch: bool = True) -> list[ScoredMemory]:
        """BM25 relevance × confidence × gentle recency decay."""
        query_terms = tokenize(query)
        if not query_terms:
            return []
        memories = [m for m in self.entries(limit=2000) if not kinds or m.kind in kinds]
        if not memories:
            return []
        docs = [tokenize(m.content + " " + " ".join(m.tags)) for m in memories]
        avg_len = sum(len(d) for d in docs) / len(docs) or 1.0
        doc_freq: Counter[str] = Counter()
        for doc in docs:
            doc_freq.update(set(doc))
        k1, b, n = 1.4, 0.75, len(docs)
        now = datetime.now(UTC)
        scored: list[ScoredMemory] = []
        for memory, doc in zip(memories, docs, strict=True):
            counts = Counter(doc)
            bm25 = 0.0
            for term in set(query_terms):
                if term not in counts:
                    continue
                idf = math.log(1 + (n - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
                tf = counts[term]
                bm25 += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(doc) / avg_len))
            if bm25 <= 0:
                continue
            age_days = (now - datetime.fromisoformat(memory.updated_at)).total_seconds() / 86400
            recency = 0.7 + 0.3 * math.exp(-age_days / 60)
            scored.append(ScoredMemory(memory, bm25 * memory.confidence * recency))
        scored.sort(key=lambda s: s.score, reverse=True)
        top = [s for s in scored if s.score >= min_score][:limit]
        if touch and top:
            stamp = now.isoformat()
            self._db.executemany(
                "UPDATE memories SET access_count = access_count + 1, last_accessed_at = ? WHERE id = ?",
                [(stamp, s.memory.id) for s in top],
            )
        return top

    def core_preferences(self, limit: int = 8) -> list[Memory]:
        """High-value preferences always worth having in context."""
        rows = self._db.query(
            "SELECT * FROM memories WHERE kind = 'preference' AND confidence >= 0.6 "
            "ORDER BY access_count DESC, updated_at DESC LIMIT ?",
            (limit,),
        )
        return [_row_to_memory(r) for r in rows]

    def context_for(self, query: str, limit: int = 6) -> list[Memory]:
        """Memories to inject for a request: core preferences + relevant others, deduplicated."""
        chosen: dict[str, Memory] = {m.id: m for m in self.core_preferences()}
        for scored in self.search(query, limit=limit):
            chosen.setdefault(scored.memory.id, scored.memory)
        return list(chosen.values())[: limit + 4]
