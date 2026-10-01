"""HTTP surface: /metrics, /probe, health, a JSON status view and the optional trigger.

Every endpoint reads the cached snapshot; none of them starts a test, except the on-demand
trigger, which is off by default, authenticated, and queues the run rather than running it.
"""

from __future__ import annotations

import hmac
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from html import escape
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import CollectorRegistry, GCCollector, PlatformCollector, ProcessCollector
from prometheus_client.exposition import choose_encoder

from . import __version__
from .businesshours import BusinessHours
from .collector import BandwidthCollector
from .config import Settings
from .scheduler import Scheduler
from .units import format_rate


def build_registry(collector: BandwidthCollector) -> CollectorRegistry:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(collector)
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    GCCollector(registry=registry)
    return registry


def create_app(
    settings: Settings,
    scheduler: Scheduler,
    *,
    collector_factory: Callable[..., BandwidthCollector],
    token: str | None = None,
    manage_scheduler: bool = True,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if manage_scheduler:
            await scheduler.start()
        try:
            yield
        finally:
            if manage_scheduler:
                await scheduler.stop()

    app = FastAPI(
        title="bandwidth-exporter",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    registry = build_registry(collector_factory())

    def exposition(request: Request, reg: CollectorRegistry) -> Response:
        encoder, content_type = choose_encoder(request.headers.get("accept", ""))
        return Response(content=encoder(reg), media_type=content_type)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        rows = "".join(
            f"<li><a href='/probe?target={escape(s.spec.key)}'>{escape(s.spec.key)}</a> "
            f"({escape(s.spec.backend)}, {escape(s.spec.target)})</li>"
            for s in scheduler.snapshot.tests
        )
        return (
            "<html><head><title>bandwidth-exporter</title></head><body>"
            f"<h1>bandwidth-exporter {__version__}</h1>"
            "<p><a href='/metrics'>Metrics</a> · <a href='/api/v1/tests'>Tests (JSON)</a></p>"
            f"<ul>{rows}</ul></body></html>"
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        if not scheduler.healthy():
            return JSONResponse({"status": "scheduler not running"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        return exposition(request, registry)

    @app.get("/probe")
    async def probe(request: Request, target: str) -> Response:
        """One test's cached results, for Prometheus Operator `Probe` resources. Never runs a
        test."""
        snap = scheduler.snapshot
        if snap.get(target) is None and not snap.named(target):
            return JSONResponse({"error": f"unknown test {target!r}"}, status_code=404)
        single = CollectorRegistry(auto_describe=False)
        single.register(collector_factory(only=target))
        return exposition(request, single)

    @app.get("/api/v1/tests")
    async def tests() -> dict[str, Any]:
        snap = scheduler.snapshot
        return {
            "in_progress": snap.in_progress,
            "queue_length": snap.queue_length,
            "business_hours": _hours_view(scheduler.hours),
            "budget": {
                "limit_bytes": snap.budget_limit_bytes,
                "transferred_bytes": snap.budget_transferred_bytes,
            },
            "tests": [_test_view(state) for state in snap.tests],
        }

    @app.post("/api/v1/tests/{name}/run", status_code=202)
    async def run(name: str, request: Request) -> Response:
        if not settings.trigger.enabled:
            return JSONResponse({"error": "the trigger is disabled"}, status_code=404)
        if not token or not _authorised(request.headers.get("authorization", ""), token):
            scheduler.record_on_demand("unauthorised")
            return JSONResponse(
                {"error": "unauthorised"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        outcome, wait = scheduler.trigger(name)
        if outcome == "unknown":
            hint = " (east/west tests: name@peer)" if scheduler.snapshot.named(name) else ""
            return JSONResponse({"error": f"unknown test {name!r}{hint}"}, status_code=404)
        if outcome == "conflict":
            return JSONResponse({"error": "already queued or running"}, status_code=409)
        if outcome in ("rate_limited", "business_hours"):
            message = (
                "inside business hours"
                if outcome == "business_hours"
                else "rate limited or over the data budget"
            )
            return JSONResponse(
                {"error": message},
                status_code=429,
                headers={"Retry-After": str(max(1, int(wait + 0.999)))},
            )
        return JSONResponse({"status": "queued", "test": name}, status_code=202)

    return app


def _hours_view(hours: BusinessHours) -> dict[str, Any]:
    now = time.time()
    active = hours.is_blocked(now)
    return {
        "configured": hours.describe(),
        "active": active,
        "ends": hours.blocked_until(now) if active else None,
    }


def _authorised(header: str, token: str) -> bool:
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented.strip().encode(), token.encode())


def _test_view(state: Any) -> dict[str, Any]:
    def direction(result: Any) -> dict[str, Any] | None:
        if result is None:
            return None
        return {
            "bytes_per_second": result.bytes_per_second,
            "rate": format_rate(result.bytes_per_second),
            "bytes": result.bytes,
            "seconds": result.seconds,
            "latency_seconds": result.latency_seconds,
            "retransmits": result.retransmits,
        }

    return {
        "name": state.spec.name,
        "key": state.spec.key,
        "peer": state.spec.peer or None,
        "kind": state.spec.kind,
        "backend": state.spec.backend,
        "target": state.spec.target,
        "download": direction(state.download),
        "upload": direction(state.upload),
        "idle_latency_seconds": state.idle_latency_seconds,
        "jitter_seconds": state.jitter_seconds,
        "last_success": state.last_success_time or None,
        "last_attempt": state.last_attempt_time or None,
        "next_run": state.next_run_time or None,
        "last_test_success": state.last_test_success,
        "last_message": state.last_message,
        "info": state.info,
    }


def trigger_token(settings: Settings) -> str | None:
    if not settings.trigger.enabled:
        return None
    value = os.environ.get(settings.trigger.token_env, "").strip()
    return value or None
