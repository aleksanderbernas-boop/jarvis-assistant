"""Tool memory: learn which (safe, predefined) method works best for an operation.

Only the *ordering* of built-in alternatives is learned — never new behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jarvis.storage.db import Database


class ToolStats:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, tool: str, method: str, ok: bool) -> None:
        now = datetime.now(UTC).isoformat()
        self._db.execute(
            "INSERT INTO tool_stats(tool, method, successes, failures, last_used_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tool, method) DO UPDATE SET successes = successes + excluded.successes, "
            "failures = failures + excluded.failures, last_used_at = excluded.last_used_at",
            (tool, method, int(ok), int(not ok), now),
        )

    def rank(self, tool: str, methods: list[str]) -> list[str]:
        """Order methods by smoothed success rate; the default order breaks ties."""
        rows = {
            r["method"]: r
            for r in self._db.query("SELECT method, successes, failures FROM tool_stats WHERE tool = ?", (tool,))
        }

        def score(item: tuple[int, str]) -> tuple[float, int]:
            index, method = item
            row = rows.get(method)
            if not row:
                return (0.5, -index)
            rate = (row["successes"] + 1) / (row["successes"] + row["failures"] + 2)
            return (rate, -index)

        return [m for _, m in sorted(enumerate(methods), key=score, reverse=True)]

    def snapshot(self) -> list[dict[str, object]]:
        return self._db.query("SELECT tool, method, successes, failures, last_used_at FROM tool_stats ORDER BY tool")
