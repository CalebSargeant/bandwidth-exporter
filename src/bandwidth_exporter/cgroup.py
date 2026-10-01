"""CPU budget detection, so a CPU cap does not show up as a slow network.

CFS quotas cap throughput almost exactly in proportion (the benchmark in docs/research), so the
exporter reads its quota at start-up, exposes it, and flags a run whose CPU use came close to it.
"""

from __future__ import annotations

import os
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")


def cpu_quota_cores(root: Path = CGROUP_ROOT) -> float | None:
    """The CFS quota in cores, or None when unlimited or unknown."""
    v2 = root / "cpu.max"
    try:
        quota, period = v2.read_text().split()[:2]
        if quota == "max":
            return None
        return int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:
        quota_us = int((root / "cpu" / "cpu.cfs_quota_us").read_text())
        period_us = int((root / "cpu" / "cpu.cfs_period_us").read_text())
    except (OSError, ValueError):
        return None
    if quota_us <= 0 or period_us <= 0:
        return None
    return quota_us / period_us


def available_cores() -> float:
    """Cores this process may use: the quota if there is one, else the CPUs it may run on."""
    quota = cpu_quota_cores()
    try:
        cpus = float(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        cpus = float(os.cpu_count() or 1)
    return min(quota, cpus) if quota else cpus


def saturated(
    cpu_seconds: float, wall_seconds: float, cores: float, threshold: float = 0.9
) -> bool:
    """True when the run used more than `threshold` of the CPU it was allowed."""
    if wall_seconds <= 0 or cores <= 0:
        return False
    return cpu_seconds / wall_seconds >= threshold * cores
