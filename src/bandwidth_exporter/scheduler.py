"""One queue, one worker: tests never overlap, and a scrape never starts one.

Scheduled runs and on-demand requests go into a single asyncio queue consumed by exactly one
worker, so a north/south test can never overlap another test from this process. A test that is
already queued or running is not queued again. After each run the next one is drawn from the
test's schedule; a failed run is retried sooner (10 min, doubling), never later than the
schedule would have run it anyway.

At start-up the scheduler catches up the way systemd's `Persistent=` does: a test runs right
away only if its last attempt is older than one schedule interval. Otherwise the persisted plan
stands, so a rollout or a Flux reconcile does not trigger a gigabyte-scale test.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from . import state as state_file
from .budget import Budget
from .config import TestSpec
from .engines import supports_latency_only
from .model import ON_DEMAND_RESULTS, RunResult, Snapshot, TestState
from .runner import Runner
from .units import format_rate

log = logging.getLogger(__name__)

FAILURE_RETRY = 600.0

TriggerOutcome = Literal["accepted", "conflict", "rate_limited", "unknown"]


class Clock(Protocol):
    def time(self) -> float:
        """Wall time in Unix seconds."""

    async def sleep(self, seconds: float) -> None:
        """Wait `seconds` of this clock's time."""


class SystemClock:
    def time(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class Job:
    name: str
    reason: Literal["scheduled", "on_demand", "once"]


class Scheduler:
    def __init__(
        self,
        specs: list[TestSpec],
        runner: Runner,
        *,
        budget: Budget,
        state_path: Path | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        startup_delay: float = 30.0,
        trigger_min_interval: float = 900.0,
    ) -> None:
        self.runner = runner
        self.budget = budget
        self.state_path = state_path
        self.clock = clock or SystemClock()
        self.rng = rng or random.Random()  # noqa: S311 - scheduling, not cryptography
        self.startup_delay = startup_delay
        self.trigger_min_interval = trigger_min_interval
        self._states: dict[str, TestState] = {s.name: TestState(spec=s) for s in specs}
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._pending: set[str] = set()
        self._running: str | None = None
        self._last_trigger: dict[str, float] = {}
        self._on_demand = dict.fromkeys(ON_DEMAND_RESULTS, 0)
        self._wakeup = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.started = False
        self.snapshot = Snapshot()
        self._publish()

    # --- lifecycle -----------------------------------------------------------------------

    def restore(self, document: dict[str, Any]) -> None:
        """Load persisted results and decide when each test runs first."""
        self.budget.restore(document.get("budget") or {})
        now = self.clock.time()
        for name, fresh in list(self._states.items()):
            restored = state_file.restore_test(fresh.spec, document)
            self._states[name] = replace(restored, next_run_time=self._first_run(restored, now))
        self._publish()

    def _first_run(self, state: TestState, now: float) -> float:
        interval = state.spec.schedule.interval()
        if state.last_attempt_time <= 0 or now - state.last_attempt_time >= interval:
            return now + self.startup_delay
        if state.next_run_time > now:
            return state.next_run_time
        return state.spec.schedule.next_after(now, self.rng)

    async def start(self) -> None:
        if all(state.next_run_time == 0 for state in self._states.values()):
            self.restore({})
        self._tasks = [
            asyncio.create_task(self._planner(), name="planner"),
            asyncio.create_task(self._worker(), name="worker"),
        ]
        self.started = True
        for state in self._states.values():
            log.info(
                "test %s (%s, %s): first run at %s",
                state.spec.name,
                state.spec.backend,
                state.spec.target,
                time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(state.next_run_time)),
            )

    async def stop(self) -> None:
        self.started = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self._persist()

    def healthy(self) -> bool:
        return self.started and all(not task.done() for task in self._tasks)

    # --- on-demand runs ------------------------------------------------------------------

    def trigger(self, name: str) -> tuple[TriggerOutcome, float]:
        """Queue a run now. Returns the outcome and, when rate-limited, seconds to wait."""
        state = self._states.get(name)
        if state is None:
            return "unknown", 0.0
        now = self.clock.time()
        if name in self._pending:
            self.record_on_demand("conflict")
            return "conflict", 0.0
        last = self._last_trigger.get(name)
        if last is not None and now - last < self.trigger_min_interval:
            self.record_on_demand("rate_limited")
            return "rate_limited", self.trigger_min_interval - (now - last)
        if self.budget.decide(state.last_transferred_bytes or None, now) != "run":
            self.record_on_demand("rate_limited")
            return "rate_limited", 3600.0
        self._last_trigger[name] = now
        self._enqueue(Job(name, "on_demand"))
        self.record_on_demand("accepted")
        return "accepted", 0.0

    def record_on_demand(self, result: str) -> None:
        self._on_demand[result] = self._on_demand.get(result, 0) + 1
        self._publish()

    # --- one-shot mode -------------------------------------------------------------------

    async def run_once(self, names: list[str] | None = None) -> list[RunResult]:
        results = []
        for name in names or list(self._states):
            results.append(await self._execute(Job(name, "once")))
        await self._persist()
        return results

    # --- internals -----------------------------------------------------------------------

    def _enqueue(self, job: Job) -> None:
        self._pending.add(job.name)
        self._queue.put_nowait(job)
        self._publish()

    async def _planner(self) -> None:
        while True:
            self._wakeup.clear()
            now = self.clock.time()
            idle = [s for s in self._states.values() if s.spec.name not in self._pending]
            for state in sorted(idle, key=lambda s: s.next_run_time):
                if state.next_run_time <= now:
                    self._enqueue(Job(state.spec.name, "scheduled"))
            upcoming = [
                s.next_run_time for s in self._states.values() if s.spec.name not in self._pending
            ]
            timeout = max(0.0, min(upcoming) - now) if upcoming else None
            await self._wait(timeout)

    async def _wait(self, timeout: float | None) -> None:
        waiter = asyncio.create_task(self._wakeup.wait())
        sleeper = asyncio.create_task(self.clock.sleep(timeout)) if timeout is not None else None
        tasks = {waiter} | ({sleeper} if sleeper else set())
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._execute(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A bug, not a network problem. Retry later instead of spinning on it.
                log.exception("run of %s failed unexpectedly", job.name)
                state = self._states[job.name]
                self._states[job.name] = replace(
                    state, next_run_time=self.clock.time() + FAILURE_RETRY
                )
            finally:
                self._pending.discard(job.name)
                self._queue.task_done()
                self._wakeup.set()
                self._publish()

    async def _execute(self, job: Job) -> RunResult:
        state = self._states[job.name]
        spec = state.spec
        now = self.clock.time()
        decision = self.budget.decide(state.last_transferred_bytes or None, now)
        if decision == "skip" or (
            decision == "latency_only" and not supports_latency_only(spec.backend)
        ):
            result = RunResult.skipped("budget", "data budget exhausted")
            started = finished = now
        else:
            self._running = spec.name
            self._publish()
            started = self.clock.time()
            try:
                result = await self.runner.run(spec, latency_only=decision == "latency_only")
            finally:
                self._running = None
            finished = self.clock.time()

        self.budget.charge(result.transferred_bytes, finished)
        folded = state.with_result(result, started, finished)
        self._states[spec.name] = replace(
            folded, next_run_time=self._next_run(folded, result, finished)
        )
        self._log(job, result)
        self._publish()
        await self._persist()
        return result

    def _next_run(self, state: TestState, result: RunResult, finished: float) -> float:
        normal = state.spec.schedule.next_after(finished, self.rng)
        if result.status == "failure":
            backoff = FAILURE_RETRY * 2 ** max(0, state.consecutive_failures - 1)
            return min(normal, finished + backoff)
        return normal

    def _log(self, job: Job, result: RunResult) -> None:
        name = job.name
        if result.status == "success":
            parts = []
            if result.download:
                parts.append(f"download {format_rate(result.download.bytes_per_second)}")
            if result.upload:
                parts.append(f"upload {format_rate(result.upload.bytes_per_second)}")
            if result.idle_latency_seconds is not None:
                parts.append(f"idle latency {result.idle_latency_seconds * 1000:.1f} ms")
            log.info("%s (%s): %s", name, job.reason, ", ".join(parts))
        else:
            log.warning(
                "%s (%s): %s %s: %s", name, job.reason, result.status, result.reason, result.message
            )

    def _publish(self) -> None:
        now = self.clock.time()
        self.budget.roll(now)
        self.snapshot = Snapshot(
            tests=tuple(self._states.values()),
            in_progress=self._running,
            queue_length=self._queue.qsize(),
            budget_limit_bytes=self.budget.limit,
            budget_transferred_bytes=self.budget.transferred,
            on_demand=dict(self._on_demand),
        )

    async def _persist(self) -> None:
        if self.state_path is None:
            return
        tests = list(self._states.values())
        budget = self.budget.to_dict()
        try:
            await asyncio.to_thread(state_file.save, self.state_path, tests, budget)
        except OSError as exc:
            log.warning("cannot write the state file %s: %s", self.state_path, exc)
