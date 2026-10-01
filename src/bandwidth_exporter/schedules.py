"""When tests run: truncated-exponential random gaps by default, cron with jitter on request.

Random start times are a measurement requirement, not a nicety: periodic sampling can lock onto
periodic network behaviour (RFC 2330), so the default gap between runs is drawn from an
exponential distribution truncated to [min, max]. Truncation is by rejection, so the gap keeps
its memoryless shape inside the bounds instead of piling up on the bounds the way clamping does.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from cronsim import CronSim

if TYPE_CHECKING:
    from .businesshours import BusinessHours


class Schedule(Protocol):
    def next_after(self, now: float, rng: random.Random) -> float:
        """Unix time of the next run after `now`."""

    def interval(self) -> float:
        """Typical gap in seconds, used for catch-up decisions at start-up."""


@dataclass(frozen=True)
class RandomSchedule:
    mean: float
    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        if not 0 < self.minimum <= self.maximum:
            raise ValueError("random schedule needs 0 < min <= max")
        if self.mean <= 0:
            raise ValueError("random schedule needs mean > 0")

    def draw_gap(self, rng: random.Random) -> float:
        if self.minimum == self.maximum:
            return self.minimum
        for _ in range(1000):
            gap = rng.expovariate(1.0 / self.mean)
            if self.minimum <= gap <= self.maximum:
                return gap
        # Only reachable with bounds far out in the tail; fall back to a uniform draw.
        return rng.uniform(self.minimum, self.maximum)

    def next_after(self, now: float, rng: random.Random) -> float:
        return now + self.draw_gap(rng)

    def interval(self) -> float:
        return self.truncated_mean()

    def truncated_mean(self) -> float:
        """Mean of the truncated distribution, which differs from the `mean` parameter."""
        rate = 1.0 / self.mean
        a, b = self.minimum, self.maximum
        if a == b:
            return a
        ea, eb = math.exp(-rate * a), math.exp(-rate * b)
        if ea - eb <= 0:
            return (a + b) / 2
        return (a * ea - b * eb) / (ea - eb) + self.mean


@dataclass(frozen=True)
class CronSchedule:
    expression: str
    jitter: float = 0.0
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        # Validate eagerly so a bad expression fails at config load, not at the first run.
        CronSim(self.expression, datetime.now(tz=self._zone()))
        if self.jitter < 0:
            raise ValueError("cron jitter must not be negative")

    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def _next_fire(self, now: float) -> float:
        start = datetime.fromtimestamp(now, tz=UTC).astimezone(self._zone())
        return next(iter(CronSim(self.expression, start))).timestamp()

    def next_after(self, now: float, rng: random.Random) -> float:
        fire = self._next_fire(now)
        return fire + (rng.uniform(0, self.jitter) if self.jitter else 0.0)

    def interval(self) -> float:
        now = datetime.now(tz=UTC).timestamp()
        first = self._next_fire(now)
        second = self._next_fire(first)
        return max(second - first, 1.0)


def next_run(
    schedule: Schedule,
    now: float,
    rng: random.Random,
    hours: BusinessHours | None = None,
    limit: int = 10_000,
) -> float | None:
    """The next run after `now`, never inside business hours.

    A random schedule's gap counts only time outside business hours, so runs spread evenly over
    the allowed hours. A cron run that falls in business hours is skipped for the next one.
    Returns None only when `limit` cron runs in a row all fall in business hours.
    """
    if hours is None or not hours.enabled:
        return schedule.next_after(now, rng)
    if isinstance(schedule, RandomSchedule):
        return hours.advance(now, schedule.draw_gap(rng))
    at = now
    for _ in range(limit):
        at = schedule.next_after(at, rng)
        if not hours.is_blocked(at):
            return at
    return None


def describe(schedule: Schedule) -> str:
    if isinstance(schedule, RandomSchedule):
        return (
            f"random gap, mean {timedelta(seconds=round(schedule.mean))}, "
            f"between {timedelta(seconds=round(schedule.minimum))} and "
            f"{timedelta(seconds=round(schedule.maximum))}"
        )
    if isinstance(schedule, CronSchedule):
        jitter = f" + up to {timedelta(seconds=round(schedule.jitter))}" if schedule.jitter else ""
        return f"cron {schedule.expression!r} ({schedule.timezone}){jitter}"
    return repr(schedule)
