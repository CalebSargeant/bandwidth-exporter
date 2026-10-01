"""Round-trip time from TCP handshakes.

`connect()` returns when the SYN-ACK arrives, so its duration is one network round trip with
no server application time in it. Probes run on their own connections next to the load, which
makes them a measure of working latency (bufferbloat) as well as idle latency.
"""

from __future__ import annotations

import contextlib
import socket
import statistics
import threading
import time
from dataclasses import dataclass, field
from itertools import pairwise

Source = tuple[str, int] | None


def connect_rtt(
    address: tuple[str, int], family: int, source: Source = None, timeout: float = 3.0
) -> float | None:
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if source is not None:
            sock.bind(source)
        started = time.perf_counter()
        sock.connect(address)
        return time.perf_counter() - started
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def median(samples: list[float]) -> float | None:
    return statistics.median(samples) if samples else None


def jitter(samples: list[float]) -> float | None:
    """Mean absolute difference between consecutive samples (Cloudflare's definition)."""
    if len(samples) < 2:
        return None
    return sum(abs(b - a) for a, b in pairwise(samples)) / (len(samples) - 1)


def idle_probe(
    address: tuple[str, int], family: int, count: int, source: Source = None, spacing: float = 0.05
) -> tuple[list[float], int]:
    """`count` sequential probes. Returns the RTTs and the number of probes that failed."""
    samples: list[float] = []
    failed = 0
    for index in range(count):
        rtt = connect_rtt(address, family, source)
        if rtt is None:
            failed += 1
        else:
            samples.append(rtt)
        if index + 1 < count:
            time.sleep(spacing)
    return samples, failed


@dataclass
class LoadedProber:
    """Probes every `interval` in a background thread while a direction is loaded."""

    address: tuple[str, int]
    family: int
    interval: float
    source: Source = None
    samples: list[tuple[float, float]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="latency-probe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            sent = time.monotonic()
            rtt = connect_rtt(self.address, self.family, self.source)
            if rtt is not None:
                self.samples.append((sent, rtt))
            self._stop.wait(self.interval)

    def median_between(self, start: float, end: float) -> float | None:
        return median([rtt for sent, rtt in self.samples if start <= sent <= end])
