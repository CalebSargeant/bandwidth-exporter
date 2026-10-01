import pytest

from bandwidth_exporter.engines.phases import PhaseController, auto_warmup_cap


def feed(controller, rate_at, step=0.1, limit=60.0):
    """Drive the controller with a byte counter whose rate is rate_at(t)."""
    t, total = 0.0, 0.0
    controller.observe(t, 0)
    while t < limit:
        t = round(t + step, 6)
        total += rate_at(t) * step
        if controller.observe(t, int(total)):
            return t
    raise AssertionError("controller never stopped")


def slow_start(t):
    # Doubles every 0.1 s from 1 MB/s until it hits 100 MB/s, then stays there.
    return min(100e6, 1e6 * 2 ** (t / 0.1))


def test_fixed_warmup_and_duration():
    controller = PhaseController(warmup=2.0, duration=5.0, max_duration=15.0, early_stop=False)
    stopped = feed(controller, lambda t: 50e6)
    window = controller.window
    assert window.start == pytest.approx(2.0)
    assert stopped == pytest.approx(7.0)
    assert window.seconds == pytest.approx(5.0)
    assert window.rate == pytest.approx(50e6, rel=0.01)
    assert controller.stop_reason == "duration"


def test_auto_warmup_excludes_slow_start():
    controller = PhaseController(warmup=None, duration=10.0, max_duration=15.0, early_stop=False)
    feed(controller, slow_start)
    warmup_end, _ = controller.warmup_end
    assert 1.4 <= warmup_end <= 2.1
    assert controller.window.rate == pytest.approx(100e6, rel=0.01)


def test_including_slow_start_would_under_read():
    controller = PhaseController(warmup=0.0, duration=2.0, max_duration=15.0, early_stop=False)
    feed(controller, slow_start)
    assert controller.window.rate < 95e6


def test_auto_warmup_falls_back_to_the_cap_when_the_rate_never_settles():
    # A sawtooth never gives three chunks within 10%.
    controller = PhaseController(warmup=None, duration=3.0, max_duration=15.0, early_stop=False)
    feed(controller, lambda t: 50e6 if int(t * 2) % 2 else 100e6)
    assert controller.warmup_end[0] == pytest.approx(2.0, abs=0.11)


def test_auto_warmup_cap_scales_with_rtt():
    assert auto_warmup_cap(None) == 2.0
    assert auto_warmup_cap(0.010) == 2.0
    assert auto_warmup_cap(0.3) == 3.0
    assert auto_warmup_cap(1.0) == 5.0


def test_early_stop_on_a_stable_rate():
    controller = PhaseController(warmup=1.0, duration=10.0, max_duration=15.0, early_stop=True)
    stopped = feed(controller, lambda t: 80e6)
    assert controller.stop_reason == "stable"
    assert 4.0 <= stopped <= 5.6
    assert controller.window.rate == pytest.approx(80e6, rel=0.01)


def test_no_early_stop_while_the_rate_moves():
    controller = PhaseController(warmup=1.0, duration=6.0, max_duration=15.0, early_stop=True)
    feed(controller, lambda t: 40e6 + 10e6 * t)
    assert controller.stop_reason == "duration"


def test_hard_cap():
    controller = PhaseController(warmup=1.0, duration=30.0, max_duration=8.0, early_stop=False)
    stopped = feed(controller, lambda t: 10e6)
    assert stopped == pytest.approx(8.0)
    assert controller.stop_reason == "max_duration"
    assert controller.window.seconds == pytest.approx(7.0)


def test_zero_rate_never_counts_as_stable():
    controller = PhaseController(warmup=None, duration=3.0, max_duration=15.0, early_stop=True)
    feed(controller, lambda t: 0.0)
    assert controller.window.bytes == 0
    assert controller.stop_reason == "duration"
