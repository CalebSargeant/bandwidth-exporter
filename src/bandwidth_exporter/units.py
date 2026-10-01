"""Human units in configuration: durations, byte sizes and rates.

Configuration accepts `10s`, `500GB` and `1Gbit/s`; everything inside the exporter and every
metric is in base units (seconds, bytes, bytes per second).
"""

from __future__ import annotations

import re

_DURATION_UNITS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
}
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w)")

# Decimal and binary byte units. Lower-case variants of the decimal ones are accepted
# (`gb`), but `Gb` is rejected below: in rates it means gigabits, so it is ambiguous here.
_BYTE_UNITS = {
    "b": 1,
    "kb": 10**3,
    "mb": 10**6,
    "gb": 10**9,
    "tb": 10**12,
    "pb": 10**15,
    "kib": 2**10,
    "mib": 2**20,
    "gib": 2**30,
    "tib": 2**40,
    "pib": 2**50,
}
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")

_BIT_PREFIX = {"": 1, "k": 10**3, "m": 10**6, "g": 10**9, "t": 10**12}
_RATE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(\S*)\s*$")


def parse_duration(value: str | float) -> float:
    """Return seconds for `90`, `1.5s`, `15m`, `4h`, `1h30m` or `500ms`."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError(f"duration must not be negative: {value!r}")
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("empty duration")
    pos = 0
    total = 0.0
    for match in _DURATION_PART.finditer(text):
        if match.start() != pos:
            break
        total += float(match.group(1)) * _DURATION_UNITS[match.group(2)]
        pos = match.end()
    if pos != len(text):
        raise ValueError(f"not a duration: {value!r} (use e.g. 10s, 15m, 4h, 1h30m)")
    return total


def format_duration(seconds: float) -> str:
    """Shortest exact form of a duration, for logs and `check-config` output."""
    if seconds == 0:
        return "0s"
    if seconds < 1:
        return f"{round(seconds * 1000)}ms"
    remaining = round(seconds)
    if abs(seconds - remaining) > 1e-9:
        return f"{seconds:g}s"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts)


def parse_bytes(value: str | int) -> int:
    """Return bytes for `500GB`, `5GiB`, `25MB` or a plain integer."""
    if isinstance(value, bool):
        raise ValueError(f"not a size: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"size must not be negative: {value!r}")
        return value
    match = _SIZE.match(str(value))
    if not match:
        raise ValueError(f"not a size: {value!r} (use e.g. 500GB, 25MB, 5GiB)")
    number, unit = match.groups()
    if unit and unit[-1] == "b" and len(unit) > 1 and unit[-2].isupper():
        # `Gb`, `Mb`: bits, not bytes.
        raise ValueError(f"{value!r} looks like bits; sizes are in bytes (GB, MB, GiB)")
    factor = _BYTE_UNITS.get(unit.lower() if unit else "b")
    if factor is None:
        raise ValueError(f"unknown size unit in {value!r}")
    return round(float(number) * factor)


def parse_rate(value: str | float) -> float:
    """Return bytes per second for `1Gbit/s`, `500Mbps`, `100MB/s` or a plain number.

    Bit rates are the norm for line rates, so `Gbit/s`, `Gb/s` and `Gbps` mean bits;
    `GB/s` means bytes. A plain number is already bytes per second.
    """
    if isinstance(value, bool):
        raise ValueError(f"not a rate: {value!r}")
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError(f"rate must not be negative: {value!r}")
        return float(value)
    match = _RATE.match(str(value))
    if not match:
        raise ValueError(f"not a rate: {value!r} (use e.g. 1Gbit/s, 500Mbps, 100MB/s)")
    number, unit = match.groups()
    amount = float(number)
    if not unit:
        return amount
    if unit.endswith("/s") or (unit.lower().endswith("ps") and len(unit) > 2):
        base = unit[:-2]
    else:
        raise ValueError(f"rate needs a per-second unit: {value!r} (e.g. 1Gbit/s)")
    lowered = base.lower()
    if lowered.endswith("bit"):
        prefix = lowered[:-3]
        if prefix in _BIT_PREFIX:
            return amount * _BIT_PREFIX[prefix] / 8
    elif base.endswith("b"):
        prefix = lowered[:-1]
        if prefix in _BIT_PREFIX:
            return amount * _BIT_PREFIX[prefix] / 8
    elif base.endswith("B"):
        factor = _BYTE_UNITS.get(lowered)
        if factor is not None:
            return amount * factor
    raise ValueError(f"unknown rate unit in {value!r}")


def format_rate(bytes_per_second: float) -> str:
    """Bit rate for humans: `941.2 Mbit/s`."""
    bits = bytes_per_second * 8
    for unit, size in (("Gbit/s", 1e9), ("Mbit/s", 1e6), ("kbit/s", 1e3)):
        if bits >= size:
            return f"{bits / size:.1f} {unit}"
    return f"{bits:.0f} bit/s"
