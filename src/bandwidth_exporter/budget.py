"""The data budget: bytes per billing period, charged with every byte a run moves.

Before each run the scheduler estimates its cost from the last run of the same test and skips
the run when the rest of the period cannot cover it. The period resets at 00:00 UTC on the
configured day of the month. There is no latency-only fallback: continuous latency is a job for
blackbox_exporter, and a test's own latency samples only mean something next to its load.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

Decision = Literal["run", "skip"]

# A run can move more than the last one did (a faster link, a longer warm-up).
ESTIMATE_MARGIN = 1.2


@dataclass
class Budget:
    limit: int | None
    reset_day: int = 1
    period_start: date | None = None
    transferred: int = 0

    def current_period(self, now: float) -> date:
        today = datetime.fromtimestamp(now, tz=UTC).date()
        if today.day >= self.reset_day:
            return today.replace(day=self.reset_day)
        year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        return date(year, month, self.reset_day)

    def roll(self, now: float) -> None:
        period = self.current_period(now)
        if self.period_start != period:
            self.period_start = period
            self.transferred = 0

    def charge(self, nbytes: int, now: float) -> None:
        self.roll(now)
        self.transferred += max(0, nbytes)

    def remaining(self, now: float) -> int | None:
        if self.limit is None:
            return None
        self.roll(now)
        return max(0, self.limit - self.transferred)

    def decide(self, estimate: int | None, now: float) -> Decision:
        remaining = self.remaining(now)
        if remaining is None:
            return "run"
        needed = 0 if estimate is None else int(estimate * ESTIMATE_MARGIN)
        if remaining <= 0 or needed > remaining:
            return "skip"
        return "run"

    def to_dict(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "transferred": self.transferred,
        }

    def restore(self, data: dict[str, Any]) -> None:
        if not data:
            return
        try:
            start = data.get("period_start")
            self.period_start = date.fromisoformat(start) if start else None
            self.transferred = int(data.get("transferred") or 0)
        except (TypeError, ValueError):
            self.period_start, self.transferred = None, 0
