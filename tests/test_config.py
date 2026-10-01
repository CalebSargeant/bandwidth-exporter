import textwrap

import pytest

from bandwidth_exporter.config import (
    ConfigError,
    Defaults,
    NorthSouthTest,
    enabled_tests,
    load_settings,
    parse_host_port,
    resolve_test,
)
from bandwidth_exporter.schedules import CronSchedule, RandomSchedule


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_defaults_without_a_file(monkeypatch):
    monkeypatch.delenv("BWEXP_LISTEN", raising=False)
    settings = load_settings(None)
    assert settings.listen == "0.0.0.0:10056"
    assert settings.listen_port == 10056
    assert settings.north_south == ()
    assert settings.trigger.enabled is False
    assert settings.budget.limit is None


def test_full_example(tmp_path):
    path = write(
        tmp_path,
        """
        listen: "127.0.0.1:9999"
        budget: {limit: 500GB, reset_day: 3}
        defaults:
          schedule: {random: {mean: 4h, min: 1h, max: 12h}}
          streams: 4
        north_south:
          - name: cloudflare
            backend: cloudflare
            schedule: {random: {mean: 6h, min: 2h, max: 15h}}
            plan: {download: 1Gbit/s, upload: 500Mbit/s}
          - name: own-iperf
            engine: iperf3
            target: iperf.example.net
            streams: 8
            schedule: {cron: "7 */6 * * *", jitter: 5m}
          - name: "off"
            backend: cloudflare
            enabled: false
        """,
    )
    settings = load_settings(path)
    assert settings.budget.limit == 500 * 10**9
    tests = enabled_tests(settings)
    assert [t.name for t in tests] == ["cloudflare", "own-iperf"]
    cloudflare, iperf = tests
    assert cloudflare.target == "speed.cloudflare.com"
    assert cloudflare.plan_download == pytest.approx(125e6)
    assert cloudflare.plan_upload == pytest.approx(62.5e6)
    assert isinstance(cloudflare.schedule, RandomSchedule)
    assert cloudflare.schedule.mean == 6 * 3600
    assert cloudflare.streams == 4
    assert cloudflare.warmup is None
    assert cloudflare.options["download_chunk"] == 50_000_000
    assert iperf.backend == "iperf3"
    assert iperf.target == "iperf.example.net:5201"
    assert iperf.streams == 8
    assert isinstance(iperf.schedule, CronSchedule)
    assert cloudflare.labels == ("cloudflare", "north_south", "")


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    path = write(tmp_path, 'listen: "127.0.0.1:9999"\n')
    monkeypatch.setenv("BWEXP_LISTEN", "127.0.0.1:8888")
    monkeypatch.setenv("BWEXP_TRIGGER__ENABLED", "true")
    monkeypatch.setenv("BWEXP_TRIGGER_TOKEN", "not-a-setting")
    settings = load_settings(path)
    assert settings.listen == "127.0.0.1:8888"
    assert settings.trigger.enabled is True


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("unknown_key: 1\n", "unknown_key"),
        ("north_south:\n  - {name: Bad Name, backend: cloudflare}\n", "test names"),
        ("north_south:\n  - {name: off, backend: cloudflare}\n", "quote the name"),
        ("north_south:\n  - {name: x, backend: speedtest}\n", "backend"),
        ("north_south:\n  - {name: x, backend: iperf3}\n", "target"),
        ("north_south:\n  - {name: x, backend: cloudflare, target: a:1}\n", "base_url"),
        (
            "north_south:\n  - {name: x, backend: cloudflare}\n"
            "  - {name: x, backend: cloudflare}\n",
            "duplicate",
        ),
        ("north_south:\n  - {name: x, backend: cloudflare, duration: soon}\n", "duration"),
        (
            "north_south:\n  - {name: x, backend: cloudflare, options: {download_chunk: 100MB}}\n",
            "download_chunk",
        ),
        (
            "north_south:\n  - {name: x, backend: cloudflare, "
            "options: {base_url: 'http://example.net'}}\n",
            "https",
        ),
        ("north_south:\n  - {name: x, backend: cloudflare, options: {colour: blue}}\n", "colour"),
        (
            "north_south:\n  - {name: x, backend: cloudflare, schedule: {cron: '7 * * *'}}\n",
            "five fields",
        ),
        (
            "north_south:\n  - {name: x, backend: cloudflare, "
            "schedule: {random: {mean: 1h, min: 1h, max: 2h}, jitter: 5m}}\n",
            "cron",
        ),
        (
            "north_south:\n  - {name: x, backend: cloudflare, warmup: 5s, max_duration: 6s}\n",
            "max_duration",
        ),
        (
            "north_south:\n  - {name: x, backend: iperf3, target: '-oops:5201'}\n",
            "bad host",
        ),
        ("east_west:\n  - {name: mesh}\n", "exactly one of `peers` or `discovery`"),
        ("budget: {on_exhausted: latency_only}\n", "on_exhausted"),
        ("budget: {limit: 5Gb}\n", "bits"),
        ("listen: nowhere\n", "port"),
    ],
)
def test_rejects(tmp_path, body, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(write(tmp_path, body))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.yaml")


def test_engine_and_backend_together_is_an_error():
    with pytest.raises(ValueError, match="not both"):
        NorthSouthTest.model_validate(
            {"name": "x", "backend": "iperf3", "engine": "iperf3", "target": "h:1"}
        )


def test_fixed_warmup_and_hard_timeout():
    spec = resolve_test(
        NorthSouthTest(name="x", backend="cloudflare", warmup="2s", duration="10s"), Defaults()
    )
    assert spec.warmup == 2
    assert spec.hard_timeout() == 60 + 2 * (15 + 30)
    worker = spec.worker_spec()
    assert "latency_only" not in worker
    assert worker["options"]["base_url"] == "https://speed.cloudflare.com"


@pytest.mark.parametrize(
    ("text", "default", "expected"),
    [
        ("host:80", None, ("host", 80)),
        ("host", 5201, ("host", 5201)),
        ("10.0.0.1:5201", None, ("10.0.0.1", 5201)),
        ("[2001:db8::1]:5201", None, ("2001:db8::1", 5201)),
        ("[::]:10056", None, ("::", 10056)),
        ("0.0.0.0:10056", None, ("0.0.0.0", 10056)),
    ],
)
def test_parse_host_port(text, default, expected):
    assert parse_host_port(text, default) == expected


@pytest.mark.parametrize(
    "text", ["host", "host:0", "host:70000", "2001:db8::1:80", "[::1", "a b:1"]
)
def test_parse_host_port_rejects(text):
    with pytest.raises(ValueError):
        parse_host_port(text, None)


def test_ipv6_iperf_target_is_bracketed():
    spec = resolve_test(
        NorthSouthTest(name="x", backend="iperf3", target="[2001:db8::1]:5202"), Defaults()
    )
    assert spec.target == "[2001:db8::1]:5202"


def test_the_example_config_is_valid():
    from pathlib import Path

    settings = load_settings(Path(__file__).parent.parent / "config.example.yaml")
    assert [t.name for t in enabled_tests(settings)] == ["cloudflare"]
