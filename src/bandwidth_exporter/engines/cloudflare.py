"""Cloudflare speed test backend: `speed.cloudflare.com/__down` and `/__up`.

The quick-start north/south backend. Cloudflare publishes no terms for programmatic use of these
endpoints, so tests against it are opt-in and infrequent, and this client never sends the result
upload that Cloudflare's own library performs.

How it measures, and why it differs from Cloudflare's browser library:
- Time-bounded, not size-bounded: N parallel streams (one thread and one keep-alive connection
  each) repeat fixed-size requests until the phase controller has a stable, warm-up-free window.
  A sequence of single size-capped requests under-reads fast links.
- Measured at the receiver: downloads count the bytes we read; uploads count the bytes the
  server's TCP acknowledged (Linux TCP_INFO), not the bytes we pushed into a socket buffer.
- Latency is the TCP handshake time to the same edge address, idle before the load and every
  400 ms during it, so it contains no server processing time.
- Cloudflare answers 403 to `__down` requests of 100 MB or more, and 429 with Retry-After when
  it rate-limits; requests stay below the cap and 429 is honoured.

Blocking sockets in threads, not asyncio: the ssl module releases the GIL while it encrypts and
decrypts, so streams spread over cores. This code only ever runs in the worker process.
"""

from __future__ import annotations

import contextlib
import email.utils
import http.client
import logging
import os
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from .. import __version__, cgroup
from ..model import DirectionResult, RunResult
from . import tcpinfo
from .latency import LoadedProber, Source, idle_probe, jitter, median
from .meter import StreamMeter
from .phases import PhaseController

log = logging.getLogger(__name__)

USER_AGENT = (
    f"bandwidth-exporter/{__version__} (+https://github.com/CalebSargeant/bandwidth-exporter)"
)
READ_BUFFER = 128 * 1024
WRITE_SLICE = 256 * 1024
SOCKET_TIMEOUT = 10.0
TICK = 0.1
MAX_RETRY_AFTER = 30.0
STREAM_RETRIES = 3
_STREAM_ERRORS = (OSError, ValueError, http.client.HTTPException)


class RunAborted(Exception):
    def __init__(self, result: RunResult) -> None:
        super().__init__(result.message)
        self.result = result


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str
    port: int
    prefix: str
    address: tuple[str, int]
    family: int

    @property
    def ip_family(self) -> str:
        return "ipv6" if self.family == socket.AF_INET6 else "ipv4"


def resolve(base_url: str, ip_family: str) -> Endpoint:
    """Resolve once, so every stream and probe in a run talks to the same edge address."""
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    family = {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}.get(ip_family, socket.AF_UNSPEC)
    infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM, 0, socket.AI_ADDRCONFIG)
    if not infos:
        raise OSError(f"no addresses for {host}")
    fam, _, _, _, sockaddr = infos[0]
    return Endpoint(
        scheme=parts.scheme,
        host=host,
        port=port,
        prefix=parts.path.rstrip("/"),
        address=(str(sockaddr[0]), int(sockaddr[1])),
        family=fam,
    )


def _open(endpoint: Endpoint, source: Source) -> socket.socket:
    sock = socket.create_connection(endpoint.address, SOCKET_TIMEOUT, source)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to the resolved address and keeps the host name for SNI and Host."""

    def __init__(
        self, endpoint: Endpoint, context: ssl.SSLContext, meter: StreamMeter | None, source: Source
    ) -> None:
        super().__init__(endpoint.host, endpoint.port, timeout=SOCKET_TIMEOUT, context=context)
        self.endpoint, self.meter, self.source, self.tls = endpoint, meter, source, context

    def connect(self) -> None:
        self.sock = self.tls.wrap_socket(
            _open(self.endpoint, self.source), server_hostname=self.endpoint.host
        )
        if self.meter is not None:
            self.meter.attach(self.sock)

    def close(self) -> None:
        if self.meter is not None:
            self.meter.detach(self.sock)
        super().close()


class PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP, for tests against a local fake only (options.allow_insecure_http)."""

    def __init__(self, endpoint: Endpoint, meter: StreamMeter | None, source: Source) -> None:
        super().__init__(endpoint.host, endpoint.port, timeout=SOCKET_TIMEOUT)
        self.endpoint, self.meter, self.source = endpoint, meter, source

    def connect(self) -> None:
        self.sock = _open(self.endpoint, self.source)
        if self.meter is not None:
            self.meter.attach(self.sock)

    def close(self) -> None:
        if self.meter is not None:
            self.meter.detach(self.sock)
        super().close()


class Session:
    """Connection factory plus the little HTTP state a browser would keep (cookies)."""

    def __init__(self, endpoint: Endpoint, bind_address: str | None) -> None:
        self.endpoint = endpoint
        self.source: Source = (bind_address, 0) if bind_address else None
        self.context = ssl.create_default_context()
        self._cookies: dict[str, str] = {}
        self._lock = threading.Lock()

    def connection(self, meter: StreamMeter | None = None) -> http.client.HTTPConnection:
        if self.endpoint.scheme == "https":
            return PinnedHTTPSConnection(self.endpoint, self.context, meter, self.source)
        return PinnedHTTPConnection(self.endpoint, meter, self.source)

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        with self._lock:
            if self._cookies:
                headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        if extra:
            headers.update(extra)
        return headers

    def absorb(self, response: http.client.HTTPResponse) -> None:
        """Keep cookies: speed.cloudflare.com ties requests to a session with `_cfuvid`."""
        for header in response.headers.get_all("Set-Cookie") or []:
            pair = header.split(";", 1)[0]
            if "=" in pair:
                name, value = pair.split("=", 1)
                with self._lock:
                    self._cookies[name.strip()] = value.strip()

    def path(self, name: str) -> str:
        return f"{self.endpoint.prefix}/{name}"


def retry_after(response: http.client.HTTPResponse, default: float = 5.0) -> float:
    value = response.headers.get("Retry-After")
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, when.timestamp() - time.time())


def status_reason(status: int) -> str:
    return "auth" if status in (401, 403) else "protocol"


def colo(headers: http.client.HTTPMessage) -> str:
    """The Cloudflare data centre that answered, e.g. AMS."""
    explicit = headers.get("cf-meta-colo")
    if explicit:
        return explicit.strip().upper()
    ray = headers.get("cf-ray") or ""
    return ray.rsplit("-", 1)[1].strip().upper() if "-" in ray else ""


def fetch_meta(session: Session) -> http.client.HTTPMessage:
    """One empty download: proves reachability and returns the edge's metadata headers."""
    for _attempt in range(3):
        conn = session.connection()
        try:
            conn.request("GET", f"{session.path('__down')}?bytes=0", headers=session.headers())
            response = conn.getresponse()
            response.read()
            session.absorb(response)
        except TimeoutError as exc:
            raise RunAborted(RunResult.failure("timeout", f"metadata request: {exc}")) from None
        except _STREAM_ERRORS as exc:
            raise RunAborted(
                RunResult.failure("connect", f"cannot reach the endpoint: {exc}")
            ) from None
        finally:
            conn.close()
        if response.status == 200:
            return response.headers
        if response.status == 429:
            delay = retry_after(response)
            if delay > MAX_RETRY_AFTER:
                break
            time.sleep(delay)
            continue
        raise RunAborted(
            RunResult.failure(status_reason(response.status), f"HTTP {response.status} on __down")
        )
    raise RunAborted(
        RunResult.skipped("rate_limited", "the endpoint is rate-limiting this address")
    )


class _Stopped(Exception):
    pass


class _RateLimited(Exception):
    def __init__(self, delay: float) -> None:
        super().__init__(delay)
        self.delay = delay


class _HttpStatus(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


@dataclass
class DirectionRun:
    """State shared by one direction's stream threads."""

    direction: str
    session: Session
    chunk: int
    streams: int
    stop: threading.Event = field(default_factory=threading.Event)
    errors: list[tuple[str, str]] = field(default_factory=list)
    meters: list[StreamMeter] = field(default_factory=list)

    def __post_init__(self) -> None:
        use_tcp_info = self.direction == "upload" and tcpinfo.supported()
        self.meters = [StreamMeter(use_tcp_info=use_tcp_info) for _ in range(self.streams)]

    @property
    def counts_at_receiver(self) -> bool:
        return self.direction == "download" or tcpinfo.supported()

    def receiver_bytes(self) -> int:
        if self.direction == "upload" and tcpinfo.supported():
            return sum(m.acked() for m in self.meters)
        return self.app_bytes()

    def app_bytes(self) -> int:
        return sum(m.app_bytes for m in self.meters)


def _check(run: DirectionRun, response: http.client.HTTPResponse) -> None:
    run.session.absorb(response)
    if response.status == 200:
        return
    with contextlib.suppress(*_STREAM_ERRORS):
        response.read()
    if response.status == 429:
        raise _RateLimited(retry_after(response))
    raise _HttpStatus(response.status)


def _download_once(
    run: DirectionRun, conn: http.client.HTTPConnection, meter: StreamMeter, buffer: memoryview
) -> None:
    conn.request(
        "GET", f"{run.session.path('__down')}?bytes={run.chunk}", headers=run.session.headers()
    )
    response = conn.getresponse()
    _check(run, response)
    while True:
        if run.stop.is_set():
            raise _Stopped
        count = response.readinto(buffer)
        if not count:
            return
        meter.app_bytes += count


def _upload_once(
    run: DirectionRun, conn: http.client.HTTPConnection, meter: StreamMeter, payload: memoryview
) -> None:
    size = run.chunk

    def body() -> Iterator[memoryview]:
        sent = 0
        while sent < size:
            if run.stop.is_set():
                raise _Stopped
            count = min(len(payload), size - sent)
            yield payload[:count]
            sent += count
            meter.app_bytes += count

    headers = run.session.headers(
        {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
    )
    conn.request("POST", run.session.path("__up"), body=body(), headers=headers)
    response = conn.getresponse()
    _check(run, response)
    response.read()


def _stream(run: DirectionRun, index: int, payload: memoryview) -> None:
    meter = run.meters[index]
    conn: http.client.HTTPConnection | None = None
    buffer = memoryview(bytearray(READ_BUFFER))
    failures = 0
    rate_limited = 0
    try:
        while not run.stop.is_set():
            if conn is None:
                conn = run.session.connection(meter)
            try:
                if run.direction == "download":
                    _download_once(run, conn, meter, buffer)
                else:
                    _upload_once(run, conn, meter, payload)
                failures = 0
            except _Stopped:
                return
            except _RateLimited as exc:
                conn.close()
                conn = None
                rate_limited += 1
                if rate_limited > STREAM_RETRIES or exc.delay > MAX_RETRY_AFTER:
                    run.errors.append(
                        ("rate_limited", "the endpoint is rate-limiting this address")
                    )
                    return
                run.stop.wait(exc.delay)
            except _HttpStatus as exc:
                run.errors.append(
                    (status_reason(exc.status), f"HTTP {exc.status} on {run.direction}")
                )
                return
            except _STREAM_ERRORS as exc:
                conn.close()
                conn = None
                if run.stop.is_set():
                    return
                failures += 1
                if failures > STREAM_RETRIES:
                    reason = "timeout" if isinstance(exc, TimeoutError) else "connect"
                    run.errors.append((reason, f"{run.direction} stream {index}: {exc}"))
                    return
                run.stop.wait(0.2)
    finally:
        if conn is not None:
            with contextlib.suppress(*_STREAM_ERRORS):
                conn.close()


def measure(
    direction: str,
    session: Session,
    spec: dict[str, Any],
    rtt: float | None,
    payload: memoryview,
) -> tuple[DirectionResult, int]:
    """Load the link in one direction. Returns the result and the bytes the run moved."""
    options = spec["options"]
    chunk = options["download_chunk"] if direction == "download" else options["upload_chunk"]
    run = DirectionRun(direction=direction, session=session, chunk=chunk, streams=spec["streams"])
    if not run.counts_at_receiver:
        log.warning("no TCP_INFO on this platform: upload is counted at the sender")
    controller = PhaseController(
        warmup=spec["warmup"],
        duration=spec["duration"],
        max_duration=spec["max_duration"],
        early_stop=spec["early_stop"],
        rtt=rtt,
    )
    prober = LoadedProber(
        session.endpoint.address,
        session.endpoint.family,
        options["loaded_latency_interval"],
        session.source,
    )
    threads = [
        threading.Thread(
            target=_stream, args=(run, i, payload), name=f"{direction}-{i}", daemon=True
        )
        for i in range(run.streams)
    ]
    prober.start()
    for thread in threads:
        thread.start()
    try:
        while not controller.observe(time.monotonic(), run.receiver_bytes()):
            if run.errors or not any(t.is_alive() for t in threads):
                break
            time.sleep(TICK)
    finally:
        run.stop.set()
        for meter in run.meters:
            meter.abort()
        for thread in threads:
            thread.join(timeout=SOCKET_TIMEOUT + 5)
        prober.stop()

    moved = run.app_bytes()
    if run.errors:
        reason, message = run.errors[0]
        aborted = (
            RunResult.skipped(reason, message)
            if reason == "rate_limited"
            else RunResult.failure(reason, message)
        )
        raise RunAborted(_add_moved(aborted, direction, moved))
    window = controller.window
    if window is None or window.bytes <= 0:
        failure = RunResult.failure("timeout", f"no {direction} data in the measured phase")
        raise RunAborted(_add_moved(failure, direction, moved))
    retransmits = None
    if direction == "upload" and tcpinfo.supported():
        retransmits = sum(m.retransmits for m in run.meters)
    log.info(
        "%s: %.1f Mbit/s over %.1fs (stopped on %s)",
        direction,
        window.rate * 8 / 1e6,
        window.seconds,
        controller.stop_reason,
    )
    result = DirectionResult(
        bytes=window.bytes,
        seconds=round(window.seconds, 3),
        bytes_per_second=window.rate,
        latency_seconds=prober.median_between(window.start, window.end),
        retransmits=retransmits,
    )
    return result, moved


def _add_moved(result: RunResult, direction: str, moved: int) -> RunResult:
    if direction == "download":
        return replace(result, received_bytes=result.received_bytes + moved)
    return replace(result, sent_bytes=result.sent_bytes + moved)


def run(spec: dict[str, Any]) -> RunResult:
    started = time.monotonic()
    cpu_started = time.process_time()
    options = spec["options"]
    info = {
        "backend": "cloudflare",
        "method": "https" if options["base_url"].startswith("https") else "http",
        "target": spec["target"],
        "streams": str(spec["streams"]),
        "cca": tcpinfo.congestion_control(),
    }
    common: dict[str, Any] = {"info": info}
    sent = received = 0
    try:
        try:
            endpoint = resolve(options["base_url"], spec["ip_family"])
        except OSError as exc:
            raise RunAborted(
                RunResult.failure("connect", f"cannot resolve the endpoint: {exc}")
            ) from None
        info["ip_family"] = endpoint.ip_family
        session = Session(endpoint, spec.get("bind_address"))
        headers = fetch_meta(session)
        info["server"] = colo(headers)
        common["public_ip"] = headers.get("cf-meta-ip")

        samples, failed = idle_probe(
            endpoint.address, endpoint.family, options["latency_samples"], session.source
        )
        if not samples:
            raise RunAborted(RunResult.failure("connect", "no latency probe got through"))
        if failed:
            log.info("%d of %d idle latency probes failed", failed, options["latency_samples"])
        common["idle_latency_seconds"] = median(samples)
        common["jitter_seconds"] = jitter(samples)

        payload = memoryview(os.urandom(WRITE_SLICE))
        results: dict[str, DirectionResult] = {}
        for direction in spec["directions"]:
            try:
                result, moved = measure(direction, session, spec, min(samples), payload)
            except RunAborted as aborted:
                partial = aborted.result
                raise RunAborted(
                    replace(
                        partial,
                        sent_bytes=sent + partial.sent_bytes,
                        received_bytes=received + partial.received_bytes,
                    )
                ) from None
            results[direction] = result
            if direction == "download":
                received += moved
            else:
                sent += moved
        outcome = RunResult(
            status="success",
            download=results.get("download"),
            upload=results.get("upload"),
            sent_bytes=sent,
            received_bytes=received,
        )
    except RunAborted as aborted:
        outcome = aborted.result
    return _finish(
        replace(outcome, **{k: v for k, v in common.items() if v is not None}), started, cpu_started
    )


def _finish(result: RunResult, started: float, cpu_started: float) -> RunResult:
    wall = time.monotonic() - started
    cpu = time.process_time() - cpu_started
    return replace(
        result,
        wall_seconds=round(wall, 3),
        cpu_seconds=round(cpu, 3),
        cpu_saturated=cgroup.saturated(cpu, wall, cgroup.available_cores()),
    )
