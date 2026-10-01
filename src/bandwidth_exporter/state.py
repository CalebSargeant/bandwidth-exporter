"""The state file: last results, schedule position and budget, kept across restarts.

Without it every Flux reconcile or rollout would start a gigabyte-scale test, and the result
series would have a gap after each restart. Results are restored only while the test still
points at the same backend and target; a changed target is a different measurement.
Counters are not persisted: Prometheus handles counter resets.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import TestSpec
from .model import DirectionResult, TestState

log = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_FILE = "state.json"


def state_path(state_dir: Path) -> Path:
    return state_dir / STATE_FILE


def load(path: Path) -> dict[str, Any]:
    """Return the persisted document, or an empty one if it is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": STATE_VERSION, "tests": {}, "budget": {}}
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable state file %s: %s", path, exc)
        return {"version": STATE_VERSION, "tests": {}, "budget": {}}
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        log.warning("ignoring state file %s with unknown version", path)
        return {"version": STATE_VERSION, "tests": {}, "budget": {}}
    data.setdefault("tests", {})
    data.setdefault("budget", {})
    return data


def restore_test(spec: TestSpec, document: dict[str, Any]) -> TestState:
    """A fresh state for `spec`, with persisted results if they belong to the same target."""
    state = TestState(spec=spec)
    entry = document.get("tests", {}).get(spec.name)
    if not isinstance(entry, dict):
        return state
    if entry.get("backend") != spec.backend or entry.get("target") != spec.target:
        log.info("test %s now points elsewhere; not restoring its old results", spec.name)
        return state
    try:
        return replace(
            state,
            download=DirectionResult.from_dict(entry.get("download")),
            upload=DirectionResult.from_dict(entry.get("upload")),
            idle_latency_seconds=entry.get("idle_latency_seconds"),
            jitter_seconds=entry.get("jitter_seconds"),
            packet_loss_ratio=entry.get("packet_loss_ratio"),
            last_success_time=float(entry.get("last_success_time") or 0.0),
            last_attempt_time=float(entry.get("last_attempt_time") or 0.0),
            next_run_time=float(entry.get("next_run_time") or 0.0),
            last_test_success=entry.get("last_test_success"),
            consecutive_failures=int(entry.get("consecutive_failures") or 0),
            info={str(k): str(v) for k, v in (entry.get("info") or {}).items()},
            public_ip_hash=entry.get("public_ip_hash"),
            last_transferred_bytes=int(entry.get("last_transferred_bytes") or 0),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("ignoring malformed state for %s: %s", spec.name, exc)
        return state


def dump_test(state: TestState) -> dict[str, Any]:
    def direction(result: DirectionResult | None) -> dict[str, Any] | None:
        if result is None:
            return None
        return {
            "bytes": result.bytes,
            "seconds": result.seconds,
            "bytes_per_second": result.bytes_per_second,
            "latency_seconds": result.latency_seconds,
            "retransmits": result.retransmits,
        }

    return {
        "backend": state.spec.backend,
        "target": state.spec.target,
        "download": direction(state.download),
        "upload": direction(state.upload),
        "idle_latency_seconds": state.idle_latency_seconds,
        "jitter_seconds": state.jitter_seconds,
        "packet_loss_ratio": state.packet_loss_ratio,
        "last_success_time": state.last_success_time,
        "last_attempt_time": state.last_attempt_time,
        "next_run_time": state.next_run_time,
        "last_test_success": state.last_test_success,
        "consecutive_failures": state.consecutive_failures,
        "info": state.info,
        "public_ip_hash": state.public_ip_hash,
        "last_transferred_bytes": state.last_transferred_bytes,
    }


def save(path: Path, tests: list[TestState], budget: dict[str, Any]) -> None:
    """Write atomically: a crash mid-write leaves the previous file intact."""
    document = {
        "version": STATE_VERSION,
        "tests": {state.spec.name: dump_test(state) for state in tests},
        "budget": budget,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
