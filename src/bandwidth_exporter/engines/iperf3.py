"""iperf3 client against an iperf3 server you run (the self-hosted north/south target).

iperf3 is the high-fidelity engine: multi-threaded streams since 3.16, receiver-side sums,
retransmits and RTT in its JSON. Each direction is one `iperf3 -c` run, download with `-R`,
never `--bidir`. The warm-up is excluded with `-O`, so `end.sum_received` covers the measured
phase only; bytes moved during the warm-up are added from the omitted intervals for the budget.

The command line is an argument list built from validated configuration, never a shell string,
and nothing a server says ends up on a command line.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import time
from dataclasses import replace
from typing import Any

from .. import cgroup
from ..model import DirectionResult, RunResult
from . import tcpinfo
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

    results: dict[str, DirectionResult] = {}
    sent = received = 0
    children_before = _children_cpu()
    outcome: RunResult | None = None
    for direction in spec["directions"]:
        args = command(spec, direction, path)
        timeout = spec["duration"] + 60
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            outcome = RunResult.failure(
                "timeout", f"iperf3 {direction} did not finish in {timeout:g}s"
            )
            break
        except OSError as exc:
            outcome = RunResult.failure("tool_error", f"cannot run iperf3: {exc}")
            break
        try:
            document = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except ValueError:
            document = {}
        error = document.get("error") or (proc.stderr.strip() if proc.returncode else "")
        if error:
            outcome = RunResult.failure(classify(error), f"iperf3 {direction}: {error}"[:500])
            break
        try:
            result, moved = parse(document, direction)
        except (ValueError, KeyError, TypeError) as exc:
            outcome = RunResult.failure("protocol", f"iperf3 {direction}: {exc}")
            break
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


def _children_cpu() -> float:
    try:
        import resource
    except ImportError:  # not on Windows
        return 0.0
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime
