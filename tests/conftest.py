"""Shared fixtures: a fake Cloudflare speed endpoint and a fake clock."""

from __future__ import annotations

import asyncio
import heapq
import itertools
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from bandwidth_exporter.config import Defaults, NorthSouthTest, TestSpec, resolve_test

CHUNK = 64 * 1024
ZEROS = bytes(CHUNK)


@dataclass
class FakeEdge:
    """Behaviour of the fake speed endpoint, adjustable per test."""

    rate_per_connection: float | None = 20e6  # bytes/s each way; None = unthrottled
    down_status: int = 200
    up_status: int = 200
    meta_status: int = 200
    retry_after: str | None = None
    requests: list[tuple[str, str]] = field(default_factory=list)
    cookies_seen: list[str] = field(default_factory=list)
    uploaded: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Throttle:
    def __init__(self, rate: float | None) -> None:
        self.rate = rate
        self.started = time.monotonic()
        self.moved = 0

    def account(self, count: int) -> None:
        self.moved += count
        if self.rate:
            due = self.started + self.moved / self.rate
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)


def _handler(edge: FakeEdge) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def _common_headers(self) -> None:
            self.send_header("cf-meta-ip", "192.0.2.10")
            self.send_header("cf-ray", "8f00aa11bb22cc33-AMS")
            self.send_header("Set-Cookie", "_cfuvid=fake-session; Path=/; HttpOnly")
            self.send_header("Server-Timing", "cfSpeedEdge;dur=1, cfSpeedWorker;dur=2")

        def _reject(self, status: int) -> None:
            self.send_response(status)
            if status == 429 and edge.retry_after is not None:
                self.send_header("Retry-After", edge.retry_after)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            with edge.lock:
                edge.requests.append(("GET", self.path))
                edge.cookies_seen.append(self.headers.get("Cookie", ""))
            if url.path != "/__down":
                self._reject(404)
                return
            size = int(parse_qs(url.query).get("bytes", ["0"])[0])
            status = edge.meta_status if size == 0 else edge.down_status
            if size >= 100_000_000:
                status = 403
            if status != 200:
                self._reject(status)
                return
            self.send_response(200)
            self._common_headers()
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            throttle = _Throttle(edge.rate_per_connection)
            sent = 0
            try:
                while sent < size:
                    count = min(CHUNK, size - sent)
                    self.wfile.write(ZEROS[:count])
                    sent += count
                    throttle.account(count)
            except OSError:
                return

        def do_POST(self) -> None:
            with edge.lock:
                edge.requests.append(("POST", self.path))
            length = int(self.headers.get("Content-Length", "0"))
            if edge.up_status != 200:
                # Drain the body first, as a well-behaved server does, then refuse.
                remaining = length
                while remaining:
                    data = self.rfile.read(min(CHUNK, remaining))
                    if not data:
                        return
                    remaining -= len(data)
                self._reject(edge.up_status)
                return
            throttle = _Throttle(edge.rate_per_connection)
            remaining = length
            try:
                while remaining:
                    data = self.rfile.read(min(CHUNK, remaining))
                    if not data:
                        return
                    remaining -= len(data)
                    with edge.lock:
                        edge.uploaded += len(data)
                    throttle.account(len(data))
            except OSError:
                return
            self.send_response(200)
            self._common_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


@pytest.fixture
def edge() -> Iterator[tuple[FakeEdge, str]]:
    state = FakeEdge()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    server.daemon_threads = True
    # Latency probes connect and hang up without a request; that is not an error here.
    server.handle_error = lambda request, client_address: None  # type: ignore[method-assign]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def cloudflare_spec(base_url: str, **overrides: Any) -> dict[str, Any]:
    """A worker spec for fast tests against the fake edge."""
    test = NorthSouthTest(
        name="cf",
        backend="cloudflare",
        warmup=overrides.pop("warmup", "0.3s"),
        duration=overrides.pop("duration", "1s"),
        max_duration=overrides.pop("max_duration", "4s"),
        streams=overrides.pop("streams", 2),
        early_stop=overrides.pop("early_stop", False),
        directions=overrides.pop("directions", ("download", "upload")),
        options={
            "base_url": base_url,
            "allow_insecure_http": True,
            "download_chunk": overrides.pop("download_chunk", "2MB"),
            "upload_chunk": overrides.pop("upload_chunk", "2MB"),
            "latency_samples": 5,
            "loaded_latency_interval": "100ms",
        },
    )
    spec = resolve_test(test, Defaults()).worker_spec(
        latency_only=overrides.pop("latency_only", False)
    )
    spec.update(overrides)
    return spec


def make_spec(name: str = "t1", backend: str = "cloudflare", **fields: Any) -> TestSpec:
    data: dict[str, Any] = {"name": name, "backend": backend}
    if backend == "iperf3":
        data["target"] = fields.pop("target", "iperf.example.net:5201")
    data.update(fields)
    return resolve_test(NorthSouthTest.model_validate(data), Defaults())


class FakeClock:
    """Wall time that only moves when a test advances it."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.now + max(0.0, seconds), next(self._seq), future))
        return await future

    async def settle(self) -> None:
        for _ in range(50):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        target = self.now + seconds
        while True:
            await self.settle()
            while self._sleepers and self._sleepers[0][2].done():
                heapq.heappop(self._sleepers)
            if self._sleepers and self._sleepers[0][0] <= target:
                deadline, _, future = heapq.heappop(self._sleepers)
                self.now = max(self.now, deadline)
                future.set_result(None)
                continue
            break
        self.now = target
        await self.settle()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
