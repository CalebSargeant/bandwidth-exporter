import json
from datetime import UTC, date, datetime

import pytest

from bandwidth_exporter import state as state_file
from bandwidth_exporter.budget import Budget
from bandwidth_exporter.model import DirectionResult, RunResult, TestState, ip_hash

from .conftest import make_spec


def ts(*args):
    return datetime(*args, tzinfo=UTC).timestamp()


# --- budget ---------------------------------------------------------------------------


def test_budget_period_rolls_on_the_reset_day():
    budget = Budget(limit=100, reset_day=5)
    budget.charge(60, ts(2026, 10, 4))
    assert budget.period_start == date(2026, 9, 5)
    assert budget.remaining(ts(2026, 10, 4, 23)) == 40
    assert budget.remaining(ts(2026, 10, 5)) == 100
    assert budget.period_start == date(2026, 10, 5)


def test_budget_period_across_new_year():
    budget = Budget(limit=100, reset_day=10)
    assert budget.current_period(ts(2026, 1, 3)) == date(2025, 12, 10)


def test_budget_decisions():
    now = ts(2026, 10, 10)
    unlimited = Budget(limit=None)
    assert unlimited.decide(10**15, now) == "run"
    budget = Budget(limit=1000)
    assert budget.decide(None, now) == "run"
    assert budget.decide(800, now) == "run"
    assert budget.decide(900, now) == "skip"  # 900 x 1.2 margin > 1000
    budget.charge(1000, now)
    assert budget.decide(None, now) == "skip"


def test_budget_round_trip():
    budget = Budget(limit=10)
    budget.charge(7, ts(2026, 10, 2))
    restored = Budget(limit=10)
    restored.restore(json.loads(json.dumps(budget.to_dict())))
    assert restored.transferred == 7
    assert restored.period_start == date(2026, 10, 1)
    restored.restore({})
    assert restored.transferred == 7
    restored.restore({"period_start": "garbage"})
    assert restored.transferred == 0


# --- model ----------------------------------------------------------------------------


def test_result_round_trip():
    result = RunResult(
        status="success",
        download=DirectionResult(1000, 1.0, 1000.0, 0.02, None),
        upload=DirectionResult(500, 1.0, 500.0, None, 3),
        idle_latency_seconds=0.01,
        sent_bytes=600,
        received_bytes=1200,
        info={"server": "AMS"},
    )
    assert RunResult.from_dict(json.loads(json.dumps(result.to_dict()))) == result
    assert result.transferred_bytes == 1800


def test_result_rejects_unknown_reasons():
    with pytest.raises(ValueError):
        RunResult.failure("gremlins", "x")
    with pytest.raises(ValueError):
        RunResult.skipped("bored", "x")


def test_state_keeps_the_last_success_through_a_failure():
    spec = make_spec()
    good = RunResult(
        status="success",
        download=DirectionResult(10, 1.0, 10.0),
        idle_latency_seconds=0.01,
        jitter_seconds=0.001,
        sent_bytes=5,
        received_bytes=10,
        public_ip="192.0.2.1",
    )
    state = TestState(spec=spec).with_result(good, 100.0, 110.0)
    assert state.last_test_success is True
    assert state.last_success_time == 110.0
    assert state.last_attempt_time == 100.0
    assert state.public_ip_hash == ip_hash("192.0.2.1")

    bad = RunResult.failure("connect", "down", sent_bytes=1)
    state = state.with_result(bad, 200.0, 201.0)
    assert state.last_test_success is False
    assert state.download.bytes_per_second == 10.0
    assert state.idle_latency_seconds == 0.01
    assert state.failures["connect"] == 1
    assert state.consecutive_failures == 1
    assert state.tests_total == 2
    assert state.sent_bytes_total == 6
    assert state.last_success_time == 110.0


def test_skips_are_not_attempts():
    spec = make_spec()
    skipped = RunResult.skipped("budget", "over budget", received_bytes=3)
    state = TestState(spec=spec).with_result(skipped, 10.0, 11.0)
    assert state.tests_total == 0
    assert state.skipped["budget"] == 1
    assert state.last_attempt_time == 0.0
    assert state.received_bytes_total == 3
    assert state.last_test_success is None


def test_latency_only_counts_from_a_successful_run():
    spec = make_spec()
    busy = RunResult.skipped("busy", "peer busy", idle_latency_seconds=0.02)
    state = TestState(spec=spec).with_result(busy, 10.0, 11.0)
    assert state.idle_latency_seconds is None
    assert state.consecutive_busy == 1
    state = state.with_result(busy, 12.0, 13.0)
    assert state.consecutive_busy == 2
    ok = RunResult(status="success", idle_latency_seconds=0.01, jitter_seconds=0.001)
    state = state.with_result(ok, 14.0, 15.0)
    assert state.idle_latency_seconds == 0.01
    assert state.consecutive_busy == 0


# --- state file -----------------------------------------------------------------------


def test_state_file_round_trip(tmp_path):
    spec = make_spec()
    state = TestState(spec=spec).with_result(
        RunResult(status="success", upload=DirectionResult(9, 1.0, 9.0, 0.03, 2)), 5.0, 6.0
    )
    path = tmp_path / "state" / "state.json"
    state_file.save(path, [state], {"period_start": "2026-10-01", "transferred": 9})
    document = state_file.load(path)
    restored = state_file.restore_test(spec, document)
    assert restored.upload == state.upload
    assert restored.last_success_time == 6.0
    assert document["budget"]["transferred"] == 9
    assert not list(path.parent.glob(".state-*"))


def test_state_is_not_restored_for_a_new_target(tmp_path):
    old = make_spec("t1", "iperf3", target="old.example.net:5201")
    new = make_spec("t1", "iperf3", target="new.example.net:5201")
    state = TestState(spec=old).with_result(
        RunResult(status="success", download=DirectionResult(9, 1.0, 9.0)), 5.0, 6.0
    )
    path = tmp_path / "state.json"
    state_file.save(path, [state], {})
    restored = state_file.restore_test(new, state_file.load(path))
    assert restored.download is None
    assert restored.last_success_time == 0.0


@pytest.mark.parametrize("content", ["{not json", '{"version": 99}', "[]"])
def test_unreadable_state_starts_fresh(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    assert state_file.load(path)["tests"] == {}


def test_missing_state_starts_fresh(tmp_path):
    assert state_file.load(tmp_path / "absent.json") == {"version": 1, "tests": {}, "budget": {}}
