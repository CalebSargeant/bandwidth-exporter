"""Runs one test in a worker process and turns whatever happens into a RunResult."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from typing import Protocol

from .config import TestSpec
from .model import RunResult

log = logging.getLogger(__name__)


class Runner(Protocol):
    async def run(self, spec: TestSpec, *, latency_only: bool = False) -> RunResult:
        """Run one test and report what happened; never raises for a failed test."""


class SubprocessRunner:
    def __init__(self, command: list[str] | None = None, timeout: float | None = None) -> None:
        self.command = command or [sys.executable, "-m", "bandwidth_exporter.worker"]
        # Overrides the per-test deadline; tests use it, production does not.
        self.timeout = timeout

    async def run(self, spec: TestSpec, *, latency_only: bool = False) -> RunResult:
        payload = json.dumps(spec.worker_spec(latency_only=latency_only)).encode()
        timeout = self.timeout or spec.hard_timeout()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return RunResult.failure("tool_error", f"cannot start the worker: {exc}")
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=timeout)
        except TimeoutError:
            await _kill(proc)
            return RunResult.failure("timeout", f"the run exceeded {timeout:g}s and was stopped")
        except asyncio.CancelledError:
            await _kill(proc)
            raise
        for line in stderr.decode(errors="replace").splitlines():
            log.info("[%s] %s", spec.name, line)
        lines = stdout.decode(errors="replace").strip().splitlines()
        if not lines:
            return RunResult.failure(
                "tool_error", f"the worker exited with code {proc.returncode} and no result"
            )
        try:
            return RunResult.from_dict(json.loads(lines[-1]))
        except (ValueError, KeyError, TypeError) as exc:
            return RunResult.failure("tool_error", f"unreadable worker result: {exc}")


async def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=10)
