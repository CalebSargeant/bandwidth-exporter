"""Business hours: windows in which no throughput test starts.

A test saturates the link it measures, so on a production uplink it belongs outside the hours
when people depend on that link. Random schedules count only time outside business hours: the
gap to the next run is drawn as usual and then laid out over the allowed hours, so tests spread
evenly over evenings, nights and weekends instead of piling up at the end of the working day.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEK = 7 * 86400


@dataclass(frozen=True)
class Window:
    """Blocked from `start` to `end` local time on each of `days` (0 = Monday). An `end` at or
    before `start` runs into the next day; equal times block the whole day."""

    days: frozenset[int]
    start: time
    end: time

    def length(self) -> float:
        begin = self.start.hour * 3600 + self.start.minute * 60
        finish = self.end.hour * 3600 + self.end.minute * 60
        return float(finish - begin if finish > begin else 86400 - begin + finish)


def parse_clock(value: str | int) -> time:
    """`07:00` or `19:30`. YAML 1.1 reads an unquoted 19:30 as the base-60 integer 1170, so an
    integer is taken as minutes after midnight, which turns it back into the intended time."""
    if isinstance(value, bool):
        raise ValueError(f"not a time of day: {value!r}")
    if isinstance(value, int):
        minutes = value
    else:
        text = str(value).strip()
        hours, sep, mins = text.partition(":")
        if not sep or not hours.isdigit() or not mins.isdigit() or len(mins) != 2:
            raise ValueError(f"not a time of day: {value!r} (use HH:MM)")
        minutes = int(hours) * 60 + int(mins)
        if int(mins) > 59:
            raise ValueError(f"not a time of day: {value!r}")
    if not 0 <= minutes <= 24 * 60:
        raise ValueError(f"not a time of day: {value!r}")
    if minutes == 24 * 60:
        return time(0, 0)
    return time(minutes // 60, minutes % 60)


class BusinessHours:
    def __init__(self, windows: Sequence[Window] = (), timezone: str = "UTC") -> None:
        self.windows = tuple(windows)
        self.timezone = timezone
        self.zone = ZoneInfo(timezone)
        if self.windows and not self._leaves_time():
            raise ValueError("business hours cover the whole week, so no test could ever run")

    def _leaves_time(self) -> bool:
        """Whether any moment of a week is outside business hours. Checked without the
        searching methods below, which assume that it is."""
        if self.blocked_per_week() < WEEK:
            return True  # overlapping windows are the only way the sum can reach a week
        reference = datetime(2026, 1, 5, tzinfo=self.zone).timestamp()  # a Monday
        spans = self._intervals(reference, days=16)
        at = reference
        for begin, end in spans:
            if end <= at:
                continue
            if begin > at:
                return True
            at = end
            if at >= reference + WEEK:
                return False
        return True

    @property
    def enabled(self) -> bool:
        return bool(self.windows)

    def blocked_per_week(self) -> float:
        return sum(window.length() * len(window.days) for window in self.windows)

    def _intervals(self, around: float, days: int = 9) -> list[tuple[float, float]]:
        """Blocked intervals as Unix times, merged and sorted, from the local day before
        `around` for `days` days."""
        first = datetime.fromtimestamp(around, tz=self.zone).date() - timedelta(days=1)
        spans: list[tuple[float, float]] = []
        for offset in range(days + 1):
            day = first + timedelta(days=offset)
            for window in self.windows:
                if day.weekday() not in window.days:
                    continue
                begin = datetime.combine(day, window.start, tzinfo=self.zone)
                if window.end > window.start:
                    end = datetime.combine(day, window.end, tzinfo=self.zone)
                else:
                    end = datetime.combine(day + timedelta(days=1), window.end, tzinfo=self.zone)
                spans.append((begin.timestamp(), end.timestamp()))
        spans.sort()
        merged: list[tuple[float, float]] = []
        for begin, end in spans:
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((begin, end))
        return merged

    def _containing(self, at: float) -> tuple[float, float] | None:
        intervals = self._intervals(at)
        index = bisect.bisect_right([begin for begin, _ in intervals], at) - 1
        if index >= 0 and intervals[index][0] <= at < intervals[index][1]:
            return intervals[index]
        return None

    def is_blocked(self, at: float) -> bool:
        return self.enabled and self._containing(at) is not None

    def blocked_until(self, at: float) -> float:
        """The end of the business hours `at` falls in, or `at` itself outside them. Merged
        windows that touch count as one."""
        if not self.enabled:
            return at
        while (span := self._containing(at)) is not None:
            at = span[1]
        return at

    def next_block(self, at: float) -> float | None:
        """Start of the next business hours after `at` (not the current one)."""
        if not self.enabled:
            return None
        for begin, _ in self._intervals(at):
            if begin > at:
                return begin
        return None

    def advance(self, start: float, seconds: float) -> float:
        """The time that lies `seconds` of allowed (non-business) time after `start`."""
        at = self.blocked_until(start)
        remaining = max(0.0, seconds)
        while True:
            block = self.next_block(at)
            if block is None or at + remaining <= block:
                return at + remaining
            remaining -= block - at
            at = self.blocked_until(block)

    def allowed_between(self, start: float, end: float) -> float:
        """Seconds outside business hours between `start` and `end`."""
        if end <= start:
            return 0.0
        if not self.enabled:
            return end - start
        allowed = 0.0
        at = start
        while at < end:
            span = self._containing(at)
            if span is not None:
                at = min(end, self.blocked_until(at))
                continue
            block = self.next_block(at)
            stop = end if block is None else min(end, block)
            allowed += stop - at
            at = stop
        return allowed

    def describe(self) -> str:
        if not self.enabled:
            return "none"
        parts = []
        for window in self.windows:
            days = ",".join(DAYS[d] for d in sorted(window.days))
            parts.append(f"{days} {window.start:%H:%M}-{window.end:%H:%M}")
        return f"{'; '.join(parts)} ({self.timezone})"
