import pytest

from bandwidth_exporter.units import (
    format_duration,
    format_rate,
    parse_bytes,
    parse_duration,
    parse_rate,
)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("10s", 10),
        ("1.5s", 1.5),
        ("500ms", 0.5),
        ("15m", 900),
        ("4h", 14400),
        ("1h30m", 5400),
        ("1d", 86400),
        ("1w", 604800),
        (90, 90),
        (0.25, 0.25),
    ],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "10", "10x", "s10", "1h 30m", "-5s", "1.5.2s", True])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_parse_duration_rejects_negative_numbers():
    with pytest.raises(ValueError):
        parse_duration(-1)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (0.4, "400ms"), (10, "10s"), (5400, "1h30m"), (86400, "1d"), (2.5, "2.5s")],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


@pytest.mark.parametrize(
    ("text", "size"),
    [
        ("500GB", 500 * 10**9),
        ("25MB", 25 * 10**6),
        ("25mb", 25 * 10**6),
        ("5GiB", 5 * 2**30),
        ("1.5kB", 1500),
        ("1024", 1024),
        (2048, 2048),
        ("10 MB", 10 * 10**6),
    ],
)
def test_parse_bytes(text, size):
    assert parse_bytes(text) == size


@pytest.mark.parametrize("text", ["5Gb", "5Mb", "lots", "5XB", "-1", False])
def test_parse_bytes_rejects(text):
    with pytest.raises(ValueError):
        parse_bytes(text)


@pytest.mark.parametrize(
    ("text", "bytes_per_second"),
    [
        ("1Gbit/s", 125e6),
        ("1 Gbit/s", 125e6),
        ("500Mbit/s", 62.5e6),
        ("500Mbps", 62.5e6),
        ("1Gb/s", 125e6),
        ("100kbit/s", 12.5e3),
        ("8bps", 1),
        ("100MB/s", 100e6),
        ("100MBps", 100e6),
        ("1MiB/s", 2**20),
        (1000, 1000),
        ("1000", 1000),
    ],
)
def test_parse_rate(text, bytes_per_second):
    assert parse_rate(text) == pytest.approx(bytes_per_second)


@pytest.mark.parametrize("text", ["1Gbit", "1G", "fast", "1Xbit/s", "1GB"])
def test_parse_rate_rejects(text):
    with pytest.raises(ValueError):
        parse_rate(text)


def test_format_rate():
    assert format_rate(117.6e6) == "940.8 Mbit/s"
    assert format_rate(1.25e9) == "10.0 Gbit/s"
    assert format_rate(100) == "800 bit/s"
