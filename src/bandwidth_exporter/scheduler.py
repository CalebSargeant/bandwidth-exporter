"""One queue, one worker: tests never overlap, and a scrape never starts one.

Scheduled runs and on-demand requests go into a single asyncio queue consumed by exactly one
worker, and the worker holds the process-wide lock it shares with the responder, so this
instance never tests while it answers a peer, nor runs two tests at once. A test that is already
queued or running is not queued again. After each run the next one is drawn from the test's
schedule, outside business hours; a failed run is retried sooner (10 min, doubling), never later
than the schedule would have run it anyway, and a peer that answers "busy" is asked again after
a random half minute to three minutes.

At start-up the scheduler catches up the way systemd's `Persistent=` does: a test runs right
away only if its last attempt is older than one schedule interval. Otherwise the persisted plan
stands, so a rollout or a Flux reconcile does not trigger a gigabyte-scale test.

East/west tests become one job per peer, and the set changes as peers come and go.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from . import state as state_file
from .budget import Budget
from .businesshours import BusinessHours
from .config import EastWestPlan, TestSpec
from .model import ON_DEMAND_RESULTS, RunResult, Snapshot, TestState
from .responder import Exclusive
from .runner import Runner
from .schedules import next_run
from .units import format_rate

log = logging.getLogger(__name__)

FAILURE_RETRY = 600.0
BUSY_RETRY = (30.0, 180.0)
BUSY_RETRIES = 5
# A run due inside business hours (a catch-up, or one that waited in the queue) moves to the
# end of them, plus up to this much, so instances do not all start at the same minute.
DEFER_JITTER = 1800.0
KEEP_ABSENT = 30 * 86400.0

TriggerOutcome = Literal["accepted", "conflict", "rate_limited", "business_hours", "unknown"]


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
    key: str
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
        startup_jitter: float = 0.0,
        trigger_min_interval: float = 900.0,
        hours: BusinessHours | None = None,
        exclusive: Exclusive | None = None,
    ) -> None:
        self.runner = runner
        self.budget = budget
        self.state_path = state_path
        self.clock = clock or SystemClock()
        self.rng = rng or random.Random()  # noqa: S311 - scheduling, not cryptography
        self.startup_delay = startup_delay
        self.startup_jitter = startup_jitter
        self.trigger_min_interval = trigger_min_interval
        self.hours = hours or BusinessHours()
        self.exclusive = exclusive or Exclusive()
        self._states: dict[str, TestState] = {s.key: TestState(spec=s) for s in specs}
        self._document: dict[str, Any] = {}
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
        self._document = document
        self.budget.restore(document.get("budget") or {})
        now = self.clock.time()
        for key, fresh in list(self._states.items()):
            restored = state_file.restore_test(fresh.spec, document)
            self._states[key] = replace(restored, next_run_time=self._first_run(restored, now))
        self._publish()

    def _first_run(self, state: TestState, now: float) -> float:
        interval = state.spec.schedule.interval()
        last = state.last_attempt_time
        if last <= 0 or self.hours.allowed_between(last, now) >= interval:
            due = now + self.startup_delay
            if self.startup_jitter:
                due += self.rng.uniform(0, self.startup_jitter)
            return self._outside_hours(due)
        if state.next_run_time > now and not self.hours.is_blocked(state.next_run_time):
            return state.next_run_time
        return self._next_after(state.spec, now)

    def _outside_hours(self, due: float) -> float:
        if not self.hours.is_blocked(due):
            return due
        return self.hours.blocked_until(due) + self.rng.uniform(0, DEFER_JITTER)

    def _next_after(self, spec: TestSpec, now: float) -> float:
        at = next_run(spec.schedule, now, self.rng, self.hours)
        return at if at is not None else self._outside_hours(now + spec.schedule.interval())

    async def start(self) -> None:
        if all(state.next_run_time == 0 for state in self._states.values()):
            self.restore(self._document)
        self._tasks = [
            asyncio.create_task(self._planner(), name="planner"),
            asyncio.create_task(self._worker(), name="worker"),
        ]
        self.started = True
        for state in self._states.values():
            self._log_plan(state)

    def _log_plan(self, state: TestState) -> None:
        log.info(
            "test %s (%s, %s): next run at %s",
            state.spec.key,
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

    # --- east/west peers -------------------------------------------------------------------

    def set_peers(self, plan: EastWestPlan, peers: Iterable[tuple[str, str]]) -> None:
        """Make the plan's jobs match `peers` (id, address): add new pairs, drop pairs whose
        peer is gone (unless one is queued or running), and follow a peer's new address."""
        wanted = dict(peers)
        now = self.clock.time()
        for key, state in list(self._states.items()):
            spec = state.spec
            if spec.kind != "east_west" or spec.name != plan.name:
                continue
            if spec.peer not in wanted:
                if key not in self._pending:
                    log.info("%s: peer %s is gone", plan.name, spec.peer)
                    # Remembered, so the results come back if the peer does.
                    self._document.setdefault("tests", {})[key] = state_file.dump_test(state)
                    del self._states[key]
            elif spec.target != wanted[spec.peer]:
                self._states[key] = replace(state, spec=replace(spec, target=wanted[spec.peer]))
        for peer_id, address in wanted.items():
            spec = plan.pair(peer_id, address)
            if spec.key in self._states:
                continue
            restored = state_file.restore_test(spec, self._document, match_target=False)
            fresh = replace(restored, next_run_time=self._first_run(restored, now))
            self._states[spec.key] = fresh
            log.info("%s: testing peer %s at %s", plan.name, peer_id, address)
            self._log_plan(fresh)
        self._wakeup.set()
        self._publish()

    # --- on-demand runs ------------------------------------------------------------------

    def trigger(self, key: str) -> tuple[TriggerOutcome, float]:
        """Queue a run now. Returns the outcome and, when refused for now, seconds to wait."""
        state = self._states.get(key)
        if state is None:
            return "unknown", 0.0
        now = self.clock.time()
        if key in self._pending:
            self.record_on_demand("conflict")
            return "conflict", 0.0
        if self.hours.is_blocked(now):
            self.record_on_demand("business_hours")
            return "business_hours", self.hours.blocked_until(now) - now
        last = self._last_trigger.get(key)
        if last is not None and now - last < self.trigger_min_interval:
            self.record_on_demand("rate_limited")
            return "rate_limited", self.trigger_min_interval - (now - last)
        if self.budget.decide(state.last_transferred_bytes or None, now) != "run":
            self.record_on_demand("rate_limited")
            return "rate_limited", 3600.0
        self._last_trigger[key] = now
        self._enqueue(Job(key, "on_demand"))
        self.record_on_demand("accepted")
        return "accepted", 0.0

    def record_on_demand(self, result: str) -> None:
        self._on_demand[result] = self._on_demand.get(result, 0) + 1
        self._publish()

    # --- one-shot mode -------------------------------------------------------------------

    async def run_once(self, keys: list[str] | None = None) -> list[RunResult]:
        results = []
        for key in keys or list(self._states):
            results.append(await self._execute(Job(key, "once")))
        await self._persist()
        return results

    # --- internals -----------------------------------------------------------------------

    def _enqueue(self, job: Job) -> None:
        self._pending.add(job.key)
        self._queue.put_nowait(job)
        self._publish()

    async def _planner(self) -> None:
        while True:
            self._wakeup.clear()
            now = self.clock.time()
            idle = [s for s in self._states.values() if s.spec.key not in self._pending]
            for state in sorted(idle, key=lambda s: s.next_run_time):
                if state.next_run_time <= now:
                    self._enqueue(Job(state.spec.key, "scheduled"))
            upcoming = [
                s.next_run_time for s in self._states.values() if s.spec.key not in self._pending
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
                log.exception("run of %s failed unexpectedly", job.key)
                state = self._states.get(job.key)
                if state is not None:
                    self._states[job.key] = replace(
                        state, next_run_time=self.clock.time() + FAILURE_RETRY
                    )
            finally:
                self._pending.discard(job.key)
                self._queue.task_done()
                self._wakeup.set()
                self._publish()

    async def _execute(self, job: Job) -> RunResult:
        state = self._states.get(job.key)
        if state is None:
            return RunResult.skipped("peer_unavailable", "the peer left before its run")
        spec = state.spec
        now = self.clock.time()
        if self.hours.is_blocked(now) and job.reason != "on_demand":
            # Only reachable for a run that waited in the queue into business hours, or a
            # one-shot run started inside them.
            result = RunResult.skipped("business_hours", "inside business hours")
            self._states[spec.key] = replace(
                state.with_result(result, now, now), next_run_time=self._outside_hours(now)
            )
            log.info(
                "%s: inside business hours, moved to %s",
                spec.key,
                time.strftime("%H:%M UTC", time.gmtime(self._states[spec.key].next_run_time)),
            )
            self._publish()
            return result
        if self.budget.decide(state.last_transferred_bytes or None, now) == "skip":
            result = RunResult.skipped("budget", "data budget exhausted for this period")
            started = finished = now
        else:
            await self.exclusive.acquire("agent")
            try:
                self._running = spec.key
                self._publish()
                started = self.clock.time()
                result = await self.runner.run(spec)
                finished = self.clock.time()
            finally:
                self._running = None
                self.exclusive.release()

        self.budget.charge(result.transferred_bytes, finished)
        current = self._states.get(spec.key, state)
        folded = current.with_result(result, started, finished)
        if spec.key in self._states:
            self._states[spec.key] = replace(
                folded, next_run_time=self._next_run(folded, result, finished)
            )
        self._log(job, result)
        self._publish()
        await self._persist()
        return result

    def _next_run(self, state: TestState, result: RunResult, finished: float) -> float:
        normal = self._next_after(state.spec, finished)
        if result.status == "failure":
            backoff = FAILURE_RETRY * 2 ** max(0, state.consecutive_failures - 1)
            return min(normal, self._outside_hours(finished + backoff))
        busy = result.status == "skipped" and result.reason == "busy"
        if busy and state.consecutive_busy <= BUSY_RETRIES:
            soon = finished + self.rng.uniform(*BUSY_RETRY)
            return min(normal, self._outside_hours(soon))
        return normal

    def _log(self, job: Job, result: RunResult) -> None:
        if result.status == "success":
            parts = []
            if result.download:
                parts.append(f"download {format_rate(result.download.bytes_per_second)}")
            if result.upload:
                parts.append(f"upload {format_rate(result.upload.bytes_per_second)}")
            if result.idle_latency_seconds is not None:
                parts.append(f"idle latency {result.idle_latency_seconds * 1000:.1f} ms")
            log.info("%s (%s): %s", job.key, job.reason, ", ".join(parts))
        else:
            log.warning(
                "%s (%s): %s %s: %s",
                job.key,
                job.reason,
                result.status,
                result.reason,
                result.message,
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
        # Keep what is known about pairs that are not active right now (a peer that left may
        # come back) for a month, so their results survive a short absence.
        cutoff = self.clock.time() - KEEP_ABSENT
        keep = {
            key: entry
            for key, entry in (self._document.get("tests") or {}).items()
            if key not in self._states
            and isinstance(entry, dict)
            and float(entry.get("last_attempt_time") or 0) > cutoff
        }
        budget = self.budget.to_dict()
        try:
            await asyncio.to_thread(state_file.save, self.state_path, tests, budget, keep)
        except OSError as exc:
            log.warning("cannot write the state file %s: %s", self.state_path, exc)
