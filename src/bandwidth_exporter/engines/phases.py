"""Warm-up detection and the measured phase, shared by the engines that count bytes themselves.

Slow start makes the first seconds of a transfer read low, so they are excluded. The warm-up
ends when three consecutive 0.5 s chunks agree within 10% (the FCC's stability rule), and at
the latest after max(2 s, 10 x RTT) capped at 5 s. The measured phase then runs for `duration`,
stops early after at least 3 s once the last four 2 s moving averages agree within 5%
(draft-ietf-ippm-responsiveness), and never runs past `max_duration` from the start.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

CHUNK_SECONDS = 0.5
STABLE_CHUNKS = 3
STABILITY = 0.10
MIN_MEASURED = 3.0
MOVING_AVERAGE_CHUNKS = 4
EARLY_STOP_POINTS = 4
EARLY_STOP_TOLERANCE = 0.05


@dataclass(frozen=True)
class Window:
    start: float
    start_bytes: int
    end: float
    end_bytes: int

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def bytes(self) -> int:
        return max(0, self.end_bytes - self.start_bytes)

    @property
    def rate(self) -> float:
        return self.bytes / self.seconds if self.seconds > 0 else 0.0


def auto_warmup_cap(rtt: float | None) -> float:
    return min(5.0, max(2.0, 10.0 * (rtt or 0.0)))


@dataclass
class PhaseController:
    """Feed it (time, cumulative bytes) samples; it says when to stop and what to report."""

    warmup: float | None
    duration: float
    max_duration: float
    early_stop: bool = True
    rtt: float | None = None

    started: float | None = None
    warmup_end: tuple[float, int] | None = None
    window: Window | None = None
    stop_reason: str | None = None
    _chunk_samples: list[tuple[float, int]] = field(default_factory=list)
    _chunk_rates: list[float] = field(default_factory=list)
    _measured_chunks: list[tuple[float, int]] = field(default_factory=list)
    _moving_averages: list[float] = field(default_factory=list)
    _next_chunk: float = 0.0

    @property
    def done(self) -> bool:
        return self.window is not None

    def observe(self, now: float, total: int) -> bool:
        """Record a sample. Returns True once the measurement should stop."""
        if self.window is not None:
            return True
        if self.started is None:
            self.started = now
            self._chunk_samples.append((now, total))
            self._next_chunk = now + CHUNK_SECONDS
            return False

        chunk_closed = False
        if now >= self._next_chunk:
            last_t, last_b = self._chunk_samples[-1]
            if now > last_t:
                self._chunk_rates.append((total - last_b) / (now - last_t))
            self._chunk_samples.append((now, total))
            self._next_chunk = now + CHUNK_SECONDS
            chunk_closed = True

        if self.warmup_end is None:
            self._check_warmup(now, total, chunk_closed)
            if self.warmup_end is not None:
                self._measured_chunks.append(self.warmup_end)
        elif chunk_closed:
            self._measured_chunks.append((now, total))
            self._update_moving_average()

        if self.warmup_end is not None:
            measured = now - self.warmup_end[0]
            if measured >= self.duration:
                return self._finish(now, total, "duration")
            if self.early_stop and measured >= MIN_MEASURED and self._stable():
                return self._finish(now, total, "stable")
        if now - self.started >= self.max_duration:
            if self.warmup_end is None:
                # Never stabilised and never hit the cap: measure what there is.
                self.warmup_end = self._chunk_samples[0]
            return self._finish(now, total, "max_duration")
        return False

    def _check_warmup(self, now: float, total: int, chunk_closed: bool) -> None:
        elapsed = now - (self.started if self.started is not None else now)
        if self.warmup is not None:
            if elapsed >= self.warmup:
                self.warmup_end = (now, total)
            return
        if chunk_closed and len(self._chunk_rates) >= STABLE_CHUNKS:
            recent = self._chunk_rates[-STABLE_CHUNKS:]
            top = max(recent)
            if top > 0 and (top - min(recent)) <= STABILITY * top:
                self.warmup_end = (now, total)
                return
        if elapsed >= auto_warmup_cap(self.rtt):
            self.warmup_end = (now, total)

    def _update_moving_average(self) -> None:
        points = self._measured_chunks[-(MOVING_AVERAGE_CHUNKS + 1) :]
        if len(points) < 2:
            return
        (t0, b0), (t1, b1) = points[0], points[-1]
        if t1 > t0:
            self._moving_averages.append((b1 - b0) / (t1 - t0))

    def _stable(self) -> bool:
        if len(self._moving_averages) < EARLY_STOP_POINTS:
            return False
        recent = self._moving_averages[-EARLY_STOP_POINTS:]
        current = recent[-1]
        if current <= 0:
            return False
        return statistics.pstdev(recent) <= EARLY_STOP_TOLERANCE * current

    def _finish(self, now: float, total: int, reason: str) -> bool:
        start, start_bytes = self.warmup_end or (now, total)
        self.window = Window(start, start_bytes, now, total)
        self.stop_reason = reason
        return True
