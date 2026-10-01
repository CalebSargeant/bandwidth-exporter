"""Data-plane engines. They run in the worker process, never on the web event loop."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..model import RunResult


def get_engine(backend: str) -> Callable[[dict[str, Any]], RunResult]:
    if backend == "cloudflare":
        from .cloudflare import run

        return run
    if backend == "iperf3":
        from .iperf3 import run

        return run
    raise ValueError(f"unknown backend {backend!r}")


def supports_latency_only(backend: str) -> bool:
    return backend == "cloudflare"
