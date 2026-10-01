"""The responder's HTTP API, on its own port: signed requests only.

    GET    /v1/info          who answers here (peer id, engines, limits)
    POST   /v1/slots         ask for a slot: {engine, direction, streams, duration, max_bytes}
    DELETE /v1/slots/{id}    release it early; returns the data server's byte counts

Refusals: 401 bad or missing signature, 403 peer not allowed, 400 bad request, 503 with
Retry-After when busy, out of ports, or inside this peer's business hours.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .responder import Rejected, Responder


def _refusal(exc: Rejected) -> JSONResponse:
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(max(1, int(exc.retry_after + 0.999)))
    return JSONResponse(
        {"error": str(exc), "reason": exc.reason}, status_code=exc.status, headers=headers
    )


def create_control_app(responder: Responder) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await responder.shutdown()

    app = FastAPI(
        title="bandwidth-exporter responder",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def signed(
        request: Request, handler: Callable[[str, bytes], Awaitable[dict[str, Any]]], status: int
    ) -> JSONResponse:
        body = await request.body()
        try:
            peer = responder.authenticate(
                dict(request.headers), request.method, request.url.path, body
            )
            return JSONResponse(await handler(peer, body), status_code=status)
        except Rejected as exc:
            return _refusal(exc)

    @app.get("/v1/info")
    async def info(request: Request) -> JSONResponse:
        async def handler(_peer: str, _body: bytes) -> dict[str, Any]:
            return responder.info()

        return await signed(request, handler, 200)

    @app.post("/v1/slots")
    async def open_slot(request: Request) -> JSONResponse:
        async def handler(peer: str, body: bytes) -> dict[str, Any]:
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                raise Rejected(400, "invalid", "the body is not JSON") from None
            if not isinstance(payload, dict):
                raise Rejected(400, "invalid", "the body is not a JSON object")
            return await responder.open(peer, payload)

        return await signed(request, handler, 201)

    @app.delete("/v1/slots/{slot_id}")
    async def close_slot(slot_id: str, request: Request) -> JSONResponse:
        async def handler(peer: str, _body: bytes) -> dict[str, Any]:
            return await responder.close(peer, slot_id)

        return await signed(request, handler, 200)

    return app
