#!/usr/bin/env python3
"""Streaming HTTP/1.1 throughput responder (the shape a built-in exporter responder would have).

  GET  /download?seconds=S&chunk=N  -> chunked stream of a pre-generated random payload for S seconds
  POST /upload                       -> consumes a (chunked) request body, replies {"bytes": n}

--impl aiohttp : aiohttp.web on uvloop
--impl uvicorn : Starlette app (what FastAPI builds on) served by uvicorn, loop=uvloop, http=httptools
"""
import argparse
import asyncio
import json
import os
import ssl
import time

PAYLOAD = {}


def payload(chunk):
    if chunk not in PAYLOAD:
        PAYLOAD[chunk] = os.urandom(chunk)  # incompressible, generated once, reused for every write
    return PAYLOAD[chunk]


def params(qs):
    return float(qs.get("seconds", 10)), int(qs.get("chunk", 1 << 20))


# ---------------------------------------------------------------- aiohttp
def aiohttp_app():
    from aiohttp import web

    async def download(request):
        secs, chunk = params(request.query)
        data = payload(chunk)
        resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream", "Cache-Control": "no-store"})
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            await resp.write(data)
        await resp.write_eof()
        return resp

    async def upload(request):
        n = 0
        async for part in request.content.iter_any():
            n += len(part)
        return web.json_response({"bytes": n})

    app = web.Application(client_max_size=1 << 62)
    app.router.add_get("/download", download)
    app.router.add_post("/upload", upload)
    return app


# ---------------------------------------------------------------- Starlette / uvicorn
def starlette_app():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    async def download(request):
        secs, chunk = params(request.query_params)
        data = payload(chunk)

        async def gen():
            deadline = time.monotonic() + secs
            while time.monotonic() < deadline:
                yield data

        return StreamingResponse(gen(), media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

    async def upload(request):
        n = 0
        async for part in request.stream():
            n += len(part)
        return JSONResponse({"bytes": n})

    return Starlette(routes=[Route("/download", download), Route("/upload", upload, methods=["POST"])])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--impl", choices=["aiohttp", "uvicorn"], default="aiohttp")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--tls", action="store_true")
    p.add_argument("--cert", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", "cert.pem"))
    p.add_argument("--key", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", "key.pem"))
    a = p.parse_args()

    if a.impl == "aiohttp":
        import uvloop
        from aiohttp import web
        ctx = None
        if a.tls:
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(a.cert, a.key)
        uvloop.install()
        web.run_app(aiohttp_app(), host=a.host, port=a.port, ssl_context=ctx, access_log=None, print=None)
    else:
        import uvicorn
        kw = dict(ssl_certfile=a.cert, ssl_keyfile=a.key) if a.tls else {}
        uvicorn.run(starlette_app(), host=a.host, port=a.port, loop="uvloop", http="httptools",
                    access_log=False, log_level="warning", **kw)


if __name__ == "__main__":
    main()
