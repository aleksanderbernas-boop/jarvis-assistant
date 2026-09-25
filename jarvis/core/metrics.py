"""Latency and reliability statistics for the diagnostics dashboard."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from statistics import mean
from typing import Any


@dataclass
class _Series:
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=200))

    def add(self, value: float) -> None:
        self.samples.append(value)

    def summary(self) -> dict[str, float | int | None]:
        if not self.samples:
            return {"count": 0, "avg_ms": None, "p95_ms": None, "last_ms": None}
        ordered = sorted(self.samples)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return {
            "count": len(self.samples),
            "avg_ms": round(mean(self.samples), 1),
            "p95_ms": round(p95, 1),
            "last_ms": round(self.samples[-1], 1),
        }


class Metrics:
    def __init__(self) -> None:
        self.started_at = time.time()
        self._latency: dict[str, _Series] = defaultdict(_Series)
        self._tool_latency: dict[str, _Series] = defaultdict(_Series)
        self._counters: dict[str, int] = defaultdict(int)

    def record(self, name: str, ms: float) -> None:
        self._latency[name].add(ms)

    def record_tool(self, tool: str, ms: float, ok: bool) -> None:
        self._tool_latency[tool].add(ms)
        self.incr("tool_success" if ok else "tool_failure")

    def incr(self, name: str, by: int = 1) -> None:
        self._counters[name] += by

    def counter(self, name: str) -> int:
        return self._counters.get(name, 0)

    def snapshot(self) -> dict[str, Any]:
        done = self.counter("task_completed")
        finished = done + self.counter("task_failed") + self.counter("task_cancelled")
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "latency": {name: series.summary() for name, series in self._latency.items()},
            "tools": {name: series.summary() for name, series in self._tool_latency.items()},
            "counters": dict(self._counters),
            "task_completion_rate": round(done / finished, 3) if finished else None,
        }


class Stopwatch:
    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000
