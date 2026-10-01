import random
import statistics
from datetime import UTC, datetime

import pytest

from bandwidth_exporter.schedules import CronSchedule, RandomSchedule, describe


def test_random_gaps_stay_inside_the_bounds():
    schedule = RandomSchedule(mean=4 * 3600, minimum=3600, maximum=12 * 3600)
    rng = random.Random(1)
    gaps = [schedule.draw_gap(rng) for _ in range(5000)]
    assert min(gaps) >= 3600
    assert max(gaps) <= 12 * 3600


def test_random_gaps_do_not_pile_up_on_the_bounds():
    # Clamping would put ~22% of draws exactly on the lower bound; truncation puts none there.
    schedule = RandomSchedule(mean=4 * 3600, minimum=3600, maximum=12 * 3600)
    rng = random.Random(2)
    gaps = [schedule.draw_gap(rng) for _ in range(5000)]
    assert sum(1 for g in gaps if g == 3600) == 0


def test_random_gap_mean_matches_the_truncated_distribution():
    schedule = RandomSchedule(mean=4 * 3600, minimum=3600, maximum=12 * 3600)
    rng = random.Random(3)
    gaps = [schedule.draw_gap(rng) for _ in range(20000)]
    assert statistics.mean(gaps) == pytest.approx(schedule.truncated_mean(), rel=0.03)
    assert schedule.interval() == pytest.approx(4.25 * 3600, rel=0.01)


def test_fixed_gap_when_min_equals_max():
    schedule = RandomSchedule(mean=3600, minimum=600, maximum=600)
    assert schedule.next_after(1000.0, random.Random()) == 1600.0
    assert schedule.interval() == 600


def test_random_schedule_validates():
    with pytest.raises(ValueError):
        RandomSchedule(mean=3600, minimum=7200, maximum=3600)
    with pytest.raises(ValueError):
        RandomSchedule(mean=0, minimum=1, maximum=2)


def test_cron_next_fire_and_jitter():
    schedule = CronSchedule("7 */6 * * *", jitter=300)
    now = datetime(2026, 10, 1, 1, 0, tzinfo=UTC).timestamp()
    fire = datetime(2026, 10, 1, 6, 7, tzinfo=UTC).timestamp()
    rng = random.Random(4)
    for _ in range(50):
        value = schedule.next_after(now, rng)
        assert fire <= value <= fire + 300
    assert schedule.interval() == 6 * 3600


def test_cron_is_strictly_after_now():
    schedule = CronSchedule("7 * * * *")
    exactly = datetime(2026, 10, 1, 1, 7, tzinfo=UTC).timestamp()
    assert schedule.next_after(exactly, random.Random()) == exactly + 3600


def test_cron_timezone():
    schedule = CronSchedule("0 19 * * *", timezone="Europe/Amsterdam")
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC).timestamp()
    # 19:00 CEST is 17:00 UTC.
    assert (
        schedule.next_after(now, random.Random())
        == datetime(2026, 10, 1, 17, 0, tzinfo=UTC).timestamp()
    )


def test_cron_rejects_bad_expressions():
    with pytest.raises(Exception):  # noqa: B017 - cronsim raises its own error type
        CronSchedule("61 * * * *")


def test_describe():
    assert "mean 4:00:00" in describe(RandomSchedule(4 * 3600, 3600, 12 * 3600))
    assert "cron '7 * * * *' (UTC)" in describe(CronSchedule("7 * * * *"))
