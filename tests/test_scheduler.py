"""The scheduler, driven by a fake clock and a fake runner."""

import asyncio
import random

import pytest

from bandwidth_exporter import state as state_file
from bandwidth_exporter.budget import Budget
from bandwidth_exporter.model import DirectionResult, RunResult
from bandwidth_exporter.scheduler import FAILURE_RETRY, Scheduler

from .conftest import make_spec

HOUR = 3600.0
FIXED = {"schedule": {"random": {"mean": "1h", "min": "1h", "max": "1h"}}}


def ok(rate=100.0, moved=1000):
    return RunResult(
        status="success",
        download=DirectionResult(int(rate), 1.0, rate),
        upload=DirectionResult(int(rate), 1.0, rate),
        idle_latency_seconds=0.01,
        sent_bytes=moved // 2,
        received_bytes=moved // 2,
    )


class FakeRunner:
    def __init__(self, clock, results=None, takes=30.0):
        self.clock = clock
        self.results = list(results or [])
        self.takes = takes
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def run(self, spec, *, latency_only=False):
        self.calls.append((spec.name, latency_only, self.clock.time()))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await self.clock.sleep(self.takes)
        finally:
            self.active -= 1
        if self.results:
            return self.results.pop(0)
        return ok()


def scheduler_for(clock, specs, runner, **kwargs):
    kwargs.setdefault("budget", Budget(limit=None))
    kwargs.setdefault("rng", random.Random(7))
    kwargs.setdefault("startup_delay", 30.0)
    return Scheduler(specs, runner, clock=clock, **kwargs)


async def test_first_run_after_the_startup_delay_then_on_schedule(clock):
    runner = FakeRunner(clock)
    sched = scheduler_for(clock, [make_spec("a", **FIXED)], runner)
    await sched.start()
    try:
        await clock.advance(29)
        assert runner.calls == []
        await clock.advance(2)
        assert [c[0] for c in runner.calls] == ["a"]
        await clock.advance(30)  # run finishes at t0+60
        state = sched.snapshot.get("a")
        assert state.last_test_success is True
        assert state.next_run_time == pytest.approx(clock.now - 1 + HOUR, abs=2)
        await clock.advance(HOUR)
        assert len(runner.calls) == 2
    finally:
        await sched.stop()


async def test_tests_never_overlap(clock):
    runner = FakeRunner(clock, takes=120.0)
    specs = [make_spec(n, **FIXED) for n in ("a", "b", "c")]
    sched = scheduler_for(clock, specs, runner)
    await sched.start()
    try:
        await clock.advance(31)
        assert sched.snapshot.in_progress == "a"
        assert sched.snapshot.queue_length == 2
        await clock.advance(400)
        assert [c[0] for c in runner.calls] == ["a", "b", "c"]
        assert runner.max_active == 1
        assert sched.snapshot.in_progress is None
    finally:
        await sched.stop()


async def test_failure_retries_sooner_with_backoff(clock):
    failure = RunResult.failure("connect", "down")
    runner = FakeRunner(clock, results=[failure, failure, ok()], takes=0.0)
    sched = scheduler_for(clock, [make_spec("a", **FIXED)], runner)
    await sched.start()
    try:
        await clock.advance(31)
        start = clock.now
        assert sched.snapshot.get("a").next_run_time == pytest.approx(start + FAILURE_RETRY, abs=2)
        await clock.advance(FAILURE_RETRY)
        assert len(runner.calls) == 2
        # second consecutive failure: twice the wait
        assert sched.snapshot.get("a").next_run_time == pytest.approx(
            clock.now + 2 * FAILURE_RETRY, abs=2
        )
        await clock.advance(2 * FAILURE_RETRY)
        assert len(runner.calls) == 3
        state = sched.snapshot.get("a")
        assert state.last_test_success is True
        assert state.failures["connect"] == 2
        assert state.consecutive_failures == 0
    finally:
        await sched.stop()


async def test_backoff_never_delays_past_the_schedule(clock):
    failure = RunResult.failure("timeout", "slow")
    runner = FakeRunner(clock, results=[failure] * 10, takes=0.0)
    quick = {"schedule": {"random": {"mean": "15m", "min": "15m", "max": "15m"}}}
    sched = scheduler_for(clock, [make_spec("a", **quick)], runner)
    await sched.start()
    try:
        await clock.advance(31 + FAILURE_RETRY + 2 * FAILURE_RETRY)
        state = sched.snapshot.get("a")
        assert state.next_run_time - clock.now <= 15 * 60 + 1
    finally:
        await sched.stop()


async def test_trigger_outcomes(clock):
    runner = FakeRunner(clock, takes=60.0)
    sched = scheduler_for(
        clock, [make_spec("a", **FIXED), make_spec("b", **FIXED)], runner, startup_delay=HOUR
    )
    sched.trigger_min_interval = 900.0
    await sched.start()
    try:
        assert sched.trigger("nope") == ("unknown", 0.0)
        assert sched.trigger("a")[0] == "accepted"
        await clock.settle()
        assert sched.trigger("a")[0] == "conflict"
        await clock.advance(61)
        outcome, wait = sched.trigger("a")
        assert outcome == "rate_limited"
        assert wait == pytest.approx(900 - 61, abs=1)
        await clock.advance(900)
        assert sched.trigger("a")[0] == "accepted"
        await clock.advance(61)
        counts = sched.snapshot.on_demand
        assert counts == {"accepted": 2, "conflict": 1, "rate_limited": 1, "unauthorised": 0}
        assert [c[0] for c in runner.calls] == ["a", "a"]
    finally:
        await sched.stop()


async def test_budget_falls_back_to_latency_only_or_skips(clock):
    now = clock.time()
    budget = Budget(limit=1000, on_exhausted="latency_only")
    budget.charge(1000, now)
    runner = FakeRunner(clock, takes=1.0)
    specs = [make_spec("cf", **FIXED), make_spec("ip", "iperf3", **FIXED)]
    sched = scheduler_for(clock, specs, runner, budget=budget)
    await sched.start()
    try:
        await clock.advance(40)
        assert runner.calls[0][:2] == ("cf", True)
        assert len(runner.calls) == 1  # iperf3 cannot do latency-only, so it was skipped
        assert sched.snapshot.get("ip").skipped["budget"] == 1
        assert sched.snapshot.get("ip").tests_total == 0
    finally:
        await sched.stop()


async def test_budget_is_charged(clock):
    budget = Budget(limit=10**9)
    runner = FakeRunner(clock, results=[ok(moved=4000)], takes=1.0)
    sched = scheduler_for(clock, [make_spec("a", **FIXED)], runner, budget=budget)
    await sched.start()
    try:
        await clock.advance(40)
        assert sched.snapshot.budget_transferred_bytes == 4000
        assert sched.snapshot.budget_limit_bytes == 10**9
    finally:
        await sched.stop()


async def test_catch_up_and_persisted_plan(clock, tmp_path):
    path = tmp_path / "state.json"
    spec = make_spec("a", **FIXED)
    runner = FakeRunner(clock, takes=1.0)
    sched = scheduler_for(clock, [spec], runner, state_path=path)
    await sched.start()
    await clock.advance(40)
    planned = sched.snapshot.get("a").next_run_time
    await sched.stop()
    assert path.is_file()

    # Restart ten minutes later: the last attempt is recent, so the persisted plan stands.
    await clock.advance(600)
    runner2 = FakeRunner(clock, takes=1.0)
    sched2 = scheduler_for(clock, [spec], runner2, state_path=path)
    sched2.restore(state_file.load(path))
    assert sched2.snapshot.get("a").next_run_time == pytest.approx(planned)
    assert sched2.snapshot.get("a").download is not None  # results survive the restart
    await sched2.start()
    await clock.advance(60)
    assert runner2.calls == []
    await sched2.stop()

    # Restart after more than one interval: catch up right away.
    await clock.advance(2 * HOUR)
    runner3 = FakeRunner(clock, takes=1.0)
    sched3 = scheduler_for(clock, [spec], runner3, state_path=path)
    sched3.restore(state_file.load(path))
    await sched3.start()
    await clock.advance(31)
    assert len(runner3.calls) == 1
    await sched3.stop()


async def test_run_once(clock):
    runner = FakeRunner(clock, results=[ok(), RunResult.failure("connect", "x")], takes=0.0)
    sched = scheduler_for(clock, [make_spec("a"), make_spec("b")], runner)
    results = await sched.run_once()
    assert [r.status for r in results] == ["success", "failure"]
    assert sched.snapshot.get("b").failures["connect"] == 1


async def test_a_crashing_runner_does_not_stop_the_scheduler(clock):
    class Exploding(FakeRunner):
        async def run(self, spec, *, latency_only=False):
            self.calls.append((spec.name, latency_only, self.clock.time()))
            if len(self.calls) == 1:
                raise RuntimeError("boom")
            return ok()

    runner = Exploding(clock)
    sched = scheduler_for(clock, [make_spec("a", **FIXED)], runner)
    await sched.start()
    try:
        await clock.advance(31)
        assert sched.healthy()
        assert len(runner.calls) == 1
        # Retried after the failure back-off, not in a tight loop.
        await clock.advance(FAILURE_RETRY - 10)
        assert len(runner.calls) == 1
        await clock.advance(20)
        assert len(runner.calls) == 2
        assert sched.snapshot.get("a").last_test_success is True
    finally:
        await sched.stop()


async def test_stop_cancels_a_running_test(clock):
    runner = FakeRunner(clock, takes=10_000.0)
    sched = scheduler_for(clock, [make_spec("a")], runner)
    await sched.start()
    await clock.advance(31)
    assert sched.snapshot.in_progress == "a"
    await asyncio.wait_for(sched.stop(), timeout=5)
    assert not sched.healthy()
