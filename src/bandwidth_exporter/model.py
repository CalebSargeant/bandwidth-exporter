"""Results and the immutable snapshot that `/metrics` renders.

A run produces a `RunResult` (it crosses the worker process boundary as JSON). The scheduler
folds results into per-test `TestState` records and publishes a new `Snapshot` by swapping one
reference, so a scrape always reads a consistent, complete view and never races a test.
"""

from __future__ import annotations

import zlib
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from .config import TestSpec

FAILURE_REASONS = ("timeout", "connect", "auth", "peer_busy", "protocol", "tool_error")
SKIP_REASONS = ("busy", "budget", "peer_unavailable", "rate_limited", "cross_traffic")
ON_DEMAND_RESULTS = ("accepted", "conflict", "rate_limited", "unauthorised")

Status = Literal["success", "failure", "skipped"]


@dataclass(frozen=True)
class DirectionResult:
    """One direction's measured phase. Throughput is what the receiver counted, warm-up
    excluded."""

    bytes: int
    seconds: float
    bytes_per_second: float
    latency_seconds: float | None = None
    retransmits: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DirectionResult | None:
        if data is None:
            return None
        return cls(
            bytes=int(data["bytes"]),
            seconds=float(data["seconds"]),
            bytes_per_second=float(data["bytes_per_second"]),
            latency_seconds=_opt_float(data.get("latency_seconds")),
            retransmits=_opt_int(data.get("retransmits")),
        )


@dataclass(frozen=True)
class RunResult:
    status: Status
    reason: str | None = None
    message: str | None = None
    download: DirectionResult | None = None
    upload: DirectionResult | None = None
    idle_latency_seconds: float | None = None
    jitter_seconds: float | None = None
    packet_loss_ratio: float | None = None
    # Every byte the run moved, warm-up, probes and failed attempts included.
    sent_bytes: int = 0
    received_bytes: int = 0
    cpu_seconds: float = 0.0
    wall_seconds: float = 0.0
    cpu_saturated: bool = False
    latency_only: bool = False
    # Kept in memory to derive a hash; never exported, persisted or logged as an address.
    public_ip: str | None = None
    info: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status == "failure" and self.reason not in FAILURE_REASONS:
            raise ValueError(f"unknown failure reason {self.reason!r}")
        if self.status == "skipped" and self.reason not in SKIP_REASONS:
            raise ValueError(f"unknown skip reason {self.reason!r}")

    @property
    def transferred_bytes(self) -> int:
        return self.sent_bytes + self.received_bytes

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunResult:
        return cls(
            status=data["status"],
            reason=data.get("reason"),
            message=data.get("message"),
            download=DirectionResult.from_dict(data.get("download")),
            upload=DirectionResult.from_dict(data.get("upload")),
            idle_latency_seconds=_opt_float(data.get("idle_latency_seconds")),
            jitter_seconds=_opt_float(data.get("jitter_seconds")),
            packet_loss_ratio=_opt_float(data.get("packet_loss_ratio")),
            sent_bytes=int(data.get("sent_bytes") or 0),
            received_bytes=int(data.get("received_bytes") or 0),
            cpu_seconds=float(data.get("cpu_seconds") or 0.0),
            wall_seconds=float(data.get("wall_seconds") or 0.0),
            cpu_saturated=bool(data.get("cpu_saturated", False)),
            latency_only=bool(data.get("latency_only", False)),
            public_ip=data.get("public_ip"),
            info={str(k): str(v) for k, v in (data.get("info") or {}).items()},
        )

    @classmethod
    def failure(cls, reason: str, message: str, **kwargs: Any) -> RunResult:
        return cls(status="failure", reason=reason, message=message, **kwargs)

    @classmethod
    def skipped(cls, reason: str, message: str, **kwargs: Any) -> RunResult:
        return cls(status="skipped", reason=reason, message=message, **kwargs)


def ip_hash(address: str) -> float:
    """CRC-32 of the egress address, as blackbox_exporter does for probe_ip_addr_hash: it shows
    that the address changed without putting an address in a label."""
    return float(zlib.crc32(address.encode()))


@dataclass(frozen=True)
class TestState:
    spec: TestSpec
    # Results of the most recent successful throughput run; kept through later failures so a
    # failed run never shows up as a false zero.
    download: DirectionResult | None = None
    upload: DirectionResult | None = None
    idle_latency_seconds: float | None = None
    jitter_seconds: float | None = None
    packet_loss_ratio: float | None = None
    last_success_time: float = 0.0
    last_attempt_time: float = 0.0
    next_run_time: float = 0.0
    last_test_success: bool | None = None
    cpu_saturated: bool = False
    consecutive_failures: int = 0
    tests_total: int = 0
    failures: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FAILURE_REASONS, 0))
    skipped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SKIP_REASONS, 0))
    sent_bytes_total: int = 0
    received_bytes_total: int = 0
    info: dict[str, str] = field(default_factory=dict)
    public_ip_hash: float | None = None
    last_transferred_bytes: int = 0
    last_message: str | None = None

    __test__ = False

    def with_result(self, result: RunResult, started: float, finished: float) -> TestState:
        """Fold one run into the state. Pure: returns a new state."""
        failures = dict(self.failures)
        skipped = dict(self.skipped)
        changes: dict[str, Any] = {
            "sent_bytes_total": self.sent_bytes_total + result.sent_bytes,
            "received_bytes_total": self.received_bytes_total + result.received_bytes,
            "last_message": result.message,
        }
        if result.info:
            changes["info"] = dict(result.info)
        if result.public_ip:
            changes["public_ip_hash"] = ip_hash(result.public_ip)
        if result.idle_latency_seconds is not None and result.status != "failure":
            changes["idle_latency_seconds"] = result.idle_latency_seconds
            changes["jitter_seconds"] = result.jitter_seconds

        if result.status == "skipped":
            skipped[result.reason or "busy"] += 1
        else:
            changes["tests_total"] = self.tests_total + 1
            changes["last_attempt_time"] = started
            changes["cpu_saturated"] = result.cpu_saturated
            changes["last_transferred_bytes"] = result.transferred_bytes
            if result.status == "success":
                changes.update(
                    download=result.download or self.download,
                    upload=result.upload or self.upload,
                    packet_loss_ratio=result.packet_loss_ratio,
                    last_success_time=finished,
                    last_test_success=True,
                    consecutive_failures=0,
                )
            else:
                failures[result.reason or "tool_error"] += 1
                changes.update(
                    last_test_success=False,
                    consecutive_failures=self.consecutive_failures + 1,
                )
        return replace(self, failures=failures, skipped=skipped, **changes)


@dataclass(frozen=True)
class Snapshot:
    tests: tuple[TestState, ...] = ()
    in_progress: str | None = None
    queue_length: int = 0
    budget_limit_bytes: int | None = None
    budget_transferred_bytes: int = 0
    on_demand: dict[str, int] = field(default_factory=lambda: dict.fromkeys(ON_DEMAND_RESULTS, 0))

    def get(self, name: str) -> TestState | None:
        for state in self.tests:
            if state.spec.name == name:
                return state
        return None


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)
