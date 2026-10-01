"""The built-in raw-TCP engine for east/west tests against a bandwidth-exporter responder.

Per direction: ask the peer's responder for a slot over the signed control API, open the
streams to the data port it names, move bytes until the phase controller has a stable,
warm-up-free window, then release the slot. Downloads count the bytes read here; uploads count
what the peer's TCP acknowledged, and the peer's own count comes back when the slot closes.
"""

from __future__ import annotations

import contextlib
import http.client
import logging
import os
import socket
import threading
import time
from dataclasses import replace
from typing import Any

from .. import cgroup
from ..config import parse_host_port
from ..control import ControlClient, ControlError, Slot, agent_key
from ..dataplane import BUFFER, hello
from ..model import DirectionResult, RunResult
from . import meter, tcpinfo
from .latency import LoadedProber, Source, idle_probe, jitter, median
from .phases import PhaseController

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10.0
SLOT_SLACK = 10.0


class RunAborted(Exception):
    def __init__(self, result: RunResult) -> None:
        super().__init__(result.message)
        self.result = result


def control_failure(exc: Exception, what: str) -> RunResult:
    """What a refused or failed control request means for this run."""
    if isinstance(exc, ControlError):
        if exc.status in (429, 503):
            reason = "peer_unavailable" if exc.reason == "business_hours" else "busy"
            return RunResult.skipped(reason, f"{what}: {exc}")
        if exc.status in (401, 403):
            return RunResult.failure("auth", f"{what}: the peer refused our signature ({exc})")
        return RunResult.failure("protocol", f"{what}: HTTP {exc.status} {exc}")
    if isinstance(exc, TimeoutError):
        return RunResult.failure("timeout", f"{what}: {exc}")
    if isinstance(exc, http.client.HTTPException):
        return RunResult.failure("protocol", f"{what}: {exc!r}")
    return RunResult.failure("connect", f"{what}: {exc}")


def resolve(host: str, port: int, ip_family: str) -> tuple[tuple[str, int], int]:
    family = {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}.get(ip_family, socket.AF_UNSPEC)
    infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"no addresses for {host}")
    fam, _, _, _, sockaddr = infos[0]
    return (str(sockaddr[0]), int(sockaddr[1])), fam


def open_slot(client: ControlClient, spec: dict[str, Any], direction: str) -> Slot:
    return client.open_slot(
        "builtin",
        direction,
        spec["streams"],
        spec["max_duration"] + SLOT_SLACK,
        spec["options"]["max_bytes"],
    )


def _stream(
    index: int,
    direction: str,
    address: tuple[str, int],
    source: Source,
    slot: Slot,
    stream_meter: meter.StreamMeter,
    stop: threading.Event,
    errors: list[tuple[str, str]],
    payload: memoryview,
) -> None:
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection(address, CONNECT_TIMEOUT, source)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        stream_meter.attach(sock)
        sock.sendall(hello(slot.token, index, direction))
        if direction == "download":
            buffer = memoryview(bytearray(BUFFER))
            while not stop.is_set():
                count = sock.recv_into(buffer)
                if not count:
                    return
                stream_meter.app_bytes += count
        else:
            while not stop.is_set():
                sock.sendall(payload)
                stream_meter.app_bytes += len(payload)
    except (ConnectionResetError, BrokenPipeError):
        # After data has flowed this is the peer ending the slot (its byte or time cap); the
        # loop then measures what there is. Before any data it is a refusal.
        if not stop.is_set() and not stream_meter.app_bytes:
            errors.append(("connect", f"{direction} stream {index}: the peer closed the stream"))
    except OSError as exc:
        if not stop.is_set():
            reason = "timeout" if isinstance(exc, TimeoutError) else "connect"
            errors.append((reason, f"{direction} stream {index}: {exc}"))
    finally:
        if sock is not None:
            stream_meter.detach(sock)
            with contextlib.suppress(OSError):
                sock.close()


def measure(
    direction: str,
    spec: dict[str, Any],
    data_address: tuple[str, int],
    control_address: tuple[str, int],
    family: int,
    source: Source,
    slot: Slot,
    rtt: float | None,
) -> tuple[DirectionResult, int]:
    options = spec["options"]
    meters = meter.meters_for(direction, spec["streams"])
    stop = threading.Event()
    errors: list[tuple[str, str]] = []
    payload = memoryview(os.urandom(BUFFER))
    threads = [
        threading.Thread(
            target=_stream,
            args=(i, direction, data_address, source, slot, meters[i], stop, errors, payload),
            name=f"{direction}-{i}",
            daemon=True,
        )
        for i in range(spec["streams"])
    ]
    controller = PhaseController(
        warmup=spec["warmup"],
        duration=spec["duration"],
        max_duration=spec["max_duration"],
        early_stop=spec["early_stop"],
        rtt=rtt,
    )
    prober = LoadedProber(control_address, family, options["loaded_latency_interval"], source)
    prober.start()
    try:
        meter.drive(direction, threads, meters, controller, stop, errors)
    finally:
        prober.stop()
    moved = meter.app_bytes(meters)
    if errors:
        reason, message = errors[0]
        raise RunAborted(_with_moved(RunResult.failure(reason, message), direction, moved))
    window = controller.window
    if window is None or window.bytes <= 0:
        failure = RunResult.failure("timeout", f"no {direction} data in the measured phase")
        raise RunAborted(_with_moved(failure, direction, moved))
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
        retransmits=meter.retransmits(direction, meters),
    )
    return result, moved


def _with_moved(result: RunResult, direction: str, moved: int) -> RunResult:
    if direction == "download":
        return replace(result, received_bytes=result.received_bytes + moved)
    return replace(result, sent_bytes=result.sent_bytes + moved)


def run(spec: dict[str, Any]) -> RunResult:
    started = time.monotonic()
    cpu_started = time.process_time()
    options = spec["options"]
    info = {
        "backend": "builtin",
        "method": "tcp",
        "target": spec["target"],
        "streams": str(spec["streams"]),
        "cca": spec.get("congestion_control") or tcpinfo.congestion_control(),
    }
    common: dict[str, Any] = {"info": info}
    sent = received = 0
    try:
        try:
            key = agent_key(os.environ.get(spec["key_env"]), spec["key_env"])
        except ValueError as exc:
            raise RunAborted(RunResult.failure("auth", str(exc))) from None
        host, control_port = parse_host_port(spec["target"], default_port=None)
        try:
            control_address, family = resolve(host, control_port, spec["ip_family"])
        except OSError as exc:
            raise RunAborted(
                RunResult.failure("connect", f"cannot resolve {host}: {exc}")
            ) from None
        info["ip_family"] = "ipv6" if family == socket.AF_INET6 else "ipv4"
        source: Source = (spec["bind_address"], 0) if spec.get("bind_address") else None
        client = ControlClient(spec["target"], spec["self_id"], key)

        samples, _failed = idle_probe(control_address, family, options["latency_samples"], source)
        if not samples:
            raise RunAborted(RunResult.failure("connect", f"{host} answers no TCP handshake"))
        common["idle_latency_seconds"] = median(samples)
        common["jitter_seconds"] = jitter(samples)

        results: dict[str, DirectionResult] = {}
        for direction in spec["directions"]:
            try:
                slot = open_slot(client, spec, direction)
            except (ControlError, OSError, http.client.HTTPException) as exc:
                raise RunAborted(
                    replace(control_failure(exc, "slot"), sent_bytes=sent, received_bytes=received)
                ) from None
            data_address = (control_address[0], slot.port)
            try:
                result, moved = measure(
                    direction,
                    spec,
                    data_address,
                    control_address,
                    family,
                    source,
                    slot,
                    min(samples),
                )
            except RunAborted as aborted:
                partial = aborted.result
                raise RunAborted(
                    replace(
                        partial,
                        sent_bytes=sent + partial.sent_bytes,
                        received_bytes=received + partial.received_bytes,
                    )
                ) from None
            finally:
                peer_counts = _close(client, slot)
            if direction == "upload" and peer_counts.get("received"):
                log.info("upload: the peer received %d bytes in total", peer_counts["received"])
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
    wall = time.monotonic() - started
    cpu = time.process_time() - cpu_started
    return replace(
        outcome,
        **{k: v for k, v in common.items() if v is not None},
        wall_seconds=round(wall, 3),
        cpu_seconds=round(cpu, 3),
        cpu_saturated=cgroup.saturated(cpu, wall, cgroup.available_cores()),
    )


def _close(client: ControlClient, slot: Slot) -> dict[str, Any]:
    try:
        return client.close_slot(slot.id)
    except (ControlError, OSError, http.client.HTTPException) as exc:
        log.warning("could not release slot %s: %s (it expires on its own)", slot.id, exc)
        return {}
