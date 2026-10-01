import random
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from bandwidth_exporter.businesshours import BusinessHours, Window, parse_clock
from bandwidth_exporter.config import BusinessHoursConfig, ConfigError
from bandwidth_exporter.schedules import CronSchedule, RandomSchedule, next_run

AMS = ZoneInfo("Europe/Amsterdam")
WEEKDAYS = frozenset(range(5))


def ams(*args):
    return datetime(*args, tzinfo=AMS).timestamp()


def office():
    return BusinessHours([Window(WEEKDAYS, time(7), time(19))], "Europe/Amsterdam")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("07:00", time(7)),
        ("19:30", time(19, 30)),
        (1170, time(19, 30)),
        ("00:00", time(0)),
        ("24:00", time(0)),
        (420, time(7)),
    ],
)
def test_parse_clock(value, expected):
    assert parse_clock(value) == expected


@pytest.mark.parametrize("value", ["7", "7:5", "25:00", "12:60", "noon", True, -5])
def test_parse_clock_rejects(value):
    with pytest.raises(ValueError):
        parse_clock(value)


def test_blocked_on_weekdays_only():
    hours = office()
    assert hours.is_blocked(ams(2026, 10, 1, 12, 0))  # Thursday noon
    assert not hours.is_blocked(ams(2026, 10, 1, 19, 0))  # end is exclusive
    assert not hours.is_blocked(ams(2026, 10, 1, 6, 59))
    assert not hours.is_blocked(ams(2026, 10, 3, 12, 0))  # Saturday
    assert hours.blocked_until(ams(2026, 10, 1, 12, 0)) == ams(2026, 10, 1, 19, 0)
    assert hours.blocked_until(ams(2026, 10, 3, 12, 0)) == ams(2026, 10, 3, 12, 0)
    assert hours.next_block(ams(2026, 10, 2, 20, 0)) == ams(2026, 10, 5, 7, 0)  # Monday


def test_advance_counts_only_allowed_time():
    hours = office()
    # Thursday 18:00 + 3 h of allowed time: the hour until 19:00 is blocked, so 22:00.
    assert hours.advance(ams(2026, 10, 1, 18, 0), 3 * 3600) == ams(2026, 10, 1, 22, 0)
    # Thursday 22:00 + 10 h: 9 h to 07:00 Friday, then the rest after 19:00 Friday.
    assert hours.advance(ams(2026, 10, 1, 22, 0), 10 * 3600) == ams(2026, 10, 2, 20, 0)


def test_allowed_between():
    hours = office()
    week = 7 * 86400
    start = ams(2026, 10, 5, 0, 0)  # Monday
    assert hours.allowed_between(start, start + week) == week - 5 * 12 * 3600
    assert hours.allowed_between(ams(2026, 10, 1, 12, 0), ams(2026, 10, 1, 18, 0)) == 0


def test_dst_change_keeps_local_times():
    hours = office()
    # The last Sunday of October 2026 ends summer time; Monday's window is still 07:00 local.
    assert hours.next_block(ams(2026, 10, 25, 12, 0)) == ams(2026, 10, 26, 7, 0)
    assert hours.blocked_until(ams(2026, 10, 26, 8, 0)) == ams(2026, 10, 26, 19, 0)


def test_windows_across_midnight_and_merging():
    night = Window(frozenset({4}), time(22), time(6))  # Friday 22:00 to Saturday 06:00
    morning = Window(frozenset({5}), time(5), time(9))  # Saturday 05:00 to 09:00, overlapping
    hours = BusinessHours([night, morning], "Europe/Amsterdam")
    assert hours.is_blocked(ams(2026, 10, 3, 1, 0))
    assert hours.blocked_until(ams(2026, 10, 2, 23, 0)) == ams(2026, 10, 3, 9, 0)


def test_a_whole_week_is_refused():
    with pytest.raises(ValueError, match="whole week"):
        BusinessHours([Window(frozenset(range(7)), time(0), time(0))])
    # Overlapping windows that still leave a gap are fine.
    BusinessHours(
        [
            Window(frozenset(range(7)), time(0), time(12)),
            Window(frozenset(range(7)), time(6), time(23)),
        ]
    )


def test_no_hours_blocks_nothing():
    hours = BusinessHours()
    assert not hours.enabled
    assert hours.blocked_until(123.0) == 123.0
    assert hours.advance(100.0, 50.0) == 150.0
    assert hours.allowed_between(0, 10) == 10
    assert hours.describe() == "none"


def test_random_runs_never_land_in_business_hours():
    hours = office()
    schedule = RandomSchedule(mean=4 * 3600, minimum=3600, maximum=12 * 3600)
    rng = random.Random(5)
    at = ams(2026, 10, 1, 12, 0)
    for _ in range(500):
        at = next_run(schedule, at, rng, hours)
        assert not hours.is_blocked(at)


def test_random_runs_spread_over_allowed_hours():
    hours = office()
    schedule = RandomSchedule(mean=2 * 3600, minimum=600, maximum=6 * 3600)
    rng = random.Random(6)
    at = ams(2026, 10, 5, 0, 0)
    hits = [0] * 24
    for _ in range(4000):
        at = next_run(schedule, at, rng, hours)
        hits[datetime.fromtimestamp(at, tz=AMS).hour] += 1
    # Evening hours right after 19:00 get no more than the late-night hours: no pile-up.
    assert hits[19] < 2 * hits[2]


def test_cron_runs_inside_business_hours_are_skipped():
    hours = office()
    schedule = CronSchedule("0 */6 * * *", timezone="Europe/Amsterdam")  # 00, 06, 12, 18
    rng = random.Random(0)
    at = next_run(schedule, ams(2026, 10, 1, 7, 0), rng, hours)
    assert at == ams(2026, 10, 2, 0, 0)  # Thursday 12:00 and 18:00 are skipped


def test_config_shorthand_and_yaml_sexagesimal_times():
    config = BusinessHoursConfig(timezone="Europe/Amsterdam", start=420, end=1140)
    hours = config.build()
    assert hours.describe() == "mon,tue,wed,thu,fri 07:00-19:00 (Europe/Amsterdam)"
    extra = BusinessHoursConfig(
        start="07:00", end="19:00", windows=[{"days": ["sat"], "start": "09:00", "end": "13:00"}]
    )
    assert len(extra.build().windows) == 2


def test_config_requires_start_and_end():
    with pytest.raises(ValueError, match="start"):
        BusinessHoursConfig(start="07:00")
    with pytest.raises(ValueError, match="start"):
        BusinessHoursConfig(days=["mon"])


def test_a_cron_schedule_that_only_fires_in_business_hours_is_refused(tmp_path):
    from bandwidth_exporter.config import load_settings

    path = tmp_path / "config.yaml"
    path.write_text(
        'business_hours: {timezone: UTC, start: "06:00", end: "20:00"}\n'
        "north_south:\n"
        "  - {name: cf, backend: cloudflare, schedule: {cron: '0 12 * * 1-5'}}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="business hours"):
        load_settings(path)
