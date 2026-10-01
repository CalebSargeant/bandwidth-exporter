"""iperf3 client against an iperf3 server you run (the self-hosted north/south target).

iperf3 is the high-fidelity engine: multi-threaded streams since 3.16, receiver-side sums,
retransmits and RTT in its JSON. Each direction is one `iperf3 -c` run, download with `-R`,
never `--bidir`. The warm-up is excluded with `-O`, so `end.sum_received` covers the measured
phase only; bytes moved during the warm-up are added from the omitted intervals for the budget.

The command line is an argument list built from validated configuration, never a shell string,
and nothing a server says ends up on a command line.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import math
import os
import shutil
import socket
import subprocess
import time
from dataclasses import replace
from typing import Any

from .. import cgroup
from ..config import format_host_port, parse_host_port
from ..control import ControlClient, ControlError, agent_key
from ..model import DirectionResult, RunResult
from . import tcpinfo
from .builtin import control_failure
from .latency import idle_probe, jitter, median
from .phases import auto_warmup_cap

BUSY_MARKERS = ("server is busy", "busy running a test")
CONNECT_MARKERS = (
    "unable to connect",
    "connection refused",
    "no route to host",
    "name or service not known",
    "unable to resolve",
    "network is unreachable",
)
TIMEOUT_MARKERS = ("timed out", "timeout")


def version(binary: str = "iperf3") -> str:
    """`3.22` for `iperf 3.22 (cJSON 1.7.15)`, or "" when iperf3 is missing."""
    path = shutil.which(binary)
    if path is None:
        return ""
    try:
        output = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    first = output.splitlines()[0] if output else ""
    parts = first.split()
    return parts[1] if len(parts) >= 2 and parts[0] == "iperf" else ""


def command(spec: dict[str, Any], direction: str, binary_path: str) -> list[str]:
    host, _, port = spec["target"].rpartition(":")
    host = host.strip("[]")
    omit = math.ceil(spec["warmup"] if spec["warmup"] is not None else auto_warmup_cap(None))
    args = [
        binary_path,
        "--client",
        host,
        "--port",
        port,
        "--json",
        "--time",
        str(max(1, math.ceil(spec["duration"]))),
        "--omit",
        str(omit),
        "--parallel",
        str(spec["streams"]),
        "--connect-timeout",
        str(int(spec["options"]["connect_timeout"] * 1000)),
    ]
    if direction == "download":
        args.append("--reverse")
    if spec["ip_family"] == "ipv4":
        args.append("-4")
    elif spec["ip_family"] == "ipv6":
        args.append("-6")
    if spec.get("bind_address"):
        args += ["--bind", spec["bind_address"]]
    if spec.get("congestion_control"):
        args += ["--congestion", spec["congestion_control"]]
    return args


def classify(error: str) -> str:
    text = error.lower()
    if any(marker in text for marker in BUSY_MARKERS):
        return "peer_busy"
    if any(marker in text for marker in CONNECT_MARKERS):
        return "connect"
    if any(marker in text for marker in TIMEOUT_MARKERS):
        return "timeout"
    return "tool_error"


def parse(document: dict[str, Any], direction: str) -> tuple[DirectionResult, int]:
    """The measured result and the bytes the whole run moved (warm-up included)."""
    end = document.get("end") or {}
    received = end.get("sum_received") or {}
    sent = end.get("sum_sent") or {}
    if not received or not received.get("seconds"):
        raise ValueError("iperf3 JSON has no end.sum_received")
    measured_bytes = int(received["bytes"])
    seconds = float(received["seconds"])
    rate = float(received.get("bits_per_second", 0.0)) / 8 or measured_bytes / seconds

    moved = 0
    for interval in document.get("intervals") or []:
        moved += int((interval.get("sum") or {}).get("bytes") or 0)
    moved = max(moved, measured_bytes)

    retransmits = sent.get("retransmits")
    rtts = [
        (stream.get("sender") or {}).get("mean_rtt")
        for stream in end.get("streams") or []
        if (stream.get("sender") or {}).get("mean_rtt")
    ]
    # mean_rtt is the local sender's smoothed RTT under load, so only uploads report it.
    latency = (sum(rtts) / len(rtts)) / 1e6 if rtts and direction == "upload" else None
    return (
        DirectionResult(
            bytes=measured_bytes,
            seconds=round(seconds, 3),
            bytes_per_second=rate,
            latency_seconds=latency,
            retransmits=int(retransmits) if retransmits is not None else None,
        ),
        moved,
    )


def run(spec: dict[str, Any]) -> RunResult:
    """North/south: run against `target`, your own iperf3 server. East/west: `target` is a
    peer's responder, which starts a single-use iperf3 server for each direction."""
    started = time.monotonic()
    binary = spec["options"]["binary"]
    path = shutil.which(binary)
    info = {
        "backend": "iperf3",
        "method": "tcp",
        "target": spec["target"],
        "streams": str(spec["streams"]),
        "cca": spec.get("congestion_control") or tcpinfo.congestion_control(),
        "ip_family": spec["ip_family"] if spec["ip_family"] != "auto" else "",
    }
    if path is None:
        return RunResult.failure("tool_error", f"{binary} not found", info=info)
    info["tool_version"] = version(path)

    east_west = spec.get("kind") == "east_west"
    client: ControlClient | None = None
    host = ""
    latency: dict[str, Any] = {}
    if east_west:
        try:
            key = agent_key(os.environ.get(spec["key_env"]), spec["key_env"])
        except ValueError as exc:
            return RunResult.failure("auth", str(exc), info=info)
        host, control_port = parse_host_port(spec["target"], default_port=None)
        client = ControlClient(spec["target"], spec["self_id"], key)
        samples = _idle_latency(host, control_port, spec)
        if samples:
            latency = {"idle_latency_seconds": median(samples), "jitter_seconds": jitter(samples)}

    results: dict[str, DirectionResult] = {}
    sent = received = 0
    children_before = _children_cpu()
    outcome: RunResult | None = None
    for direction in spec["directions"]:
        run_spec = spec
        slot = None
        if client is not None:
            omit = math.ceil(
                spec["warmup"] if spec["warmup"] is not None else auto_warmup_cap(None)
            )
            try:
                slot = client.open_slot(
                    "iperf3", direction, spec["streams"], spec["duration"] + omit + 10, 0
                )
            except (ControlError, OSError, http.client.HTTPException) as exc:
                outcome = control_failure(exc, "slot")
                break
            run_spec = {**spec, "target": format_host_port(host, slot.port)}
        try:
            outcome_or_result = _run_direction(run_spec, direction, path)
        finally:
            if client is not None and slot is not None:
                with contextlib.suppress(ControlError, OSError, http.client.HTTPException):
                    client.close_slot(slot.id)
        if isinstance(outcome_or_result, RunResult):
            outcome = outcome_or_result
            break
        result, moved = outcome_or_result
        results[direction] = result
        if direction == "download":
            received += moved
        else:
            sent += moved

    if outcome is None:
        outcome = RunResult(
            status="success",
            download=results.get("download"),
            upload=results.get("upload"),
            **latency,
        )
    wall = time.monotonic() - started
    cpu = _children_cpu() - children_before
    return replace(
        outcome,
        sent_bytes=sent,
        received_bytes=received,
        info=info,
        wall_seconds=round(wall, 3),
        cpu_seconds=round(cpu, 3),
        cpu_saturated=cgroup.saturated(cpu, wall, cgroup.available_cores()),
    )


def _run_direction(
    spec: dict[str, Any], direction: str, path: str
) -> RunResult | tuple[DirectionResult, int]:
    """One iperf3 client run: the result and the bytes moved, or a failed RunResult."""
    args = command(spec, direction, path)
    timeout = spec["duration"] + 60
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return RunResult.failure("timeout", f"iperf3 {direction} did not finish in {timeout:g}s")
    except OSError as exc:
        return RunResult.failure("tool_error", f"cannot run iperf3: {exc}")
    try:
        document = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except ValueError:
        document = {}
    error = document.get("error") or (proc.stderr.strip() if proc.returncode else "")
    if error:
        return RunResult.failure(classify(error), f"iperf3 {direction}: {error}"[:500])
    try:
        return parse(document, direction)
    except (ValueError, KeyError, TypeError) as exc:
        return RunResult.failure("protocol", f"iperf3 {direction}: {exc}")


def _idle_latency(host: str, port: int, spec: dict[str, Any]) -> list[float]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    family, _, _, _, sockaddr = infos[0]
    source = (spec["bind_address"], 0) if spec.get("bind_address") else None
    samples, _ = idle_probe((str(sockaddr[0]), int(sockaddr[1])), family, 10, source)
    return samples


def _children_cpu() -> float:
    try:
        import resource
    except ImportError:  # not on Windows
        return 0.0
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime
