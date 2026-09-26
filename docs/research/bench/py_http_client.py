#!/usr/bin/env python3
"""Streaming HTTP/1.1 throughput client: aiohttp or httpx, download (GET) or upload (chunked POST).

Prints one JSON line with bytes, seconds, gbps and the client's own CPU seconds.
Runs on uvloop by default (--loop asyncio to compare with the stock event loop).
"""
import argparse
import asyncio
import json
import os
import resource
import ssl
import time


def cpu_self():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


async def body_gen(secs, data):
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        yield data


async def run_aiohttp(a, base, ctx):
    import aiohttp
    conn = aiohttp.TCPConnector(ssl=ctx if ctx else False)
    async with aiohttp.ClientSession(connector=conn, auto_decompress=False,
                                     timeout=aiohttp.ClientTimeout(total=None)) as s:
        t0 = time.monotonic()
        if a.dir == "down":
            n = 0
            async with s.get(f"{base}/download?seconds={a.seconds}&chunk={a.chunk}") as r:
                r.raise_for_status()
                async for part in r.content.iter_any():
                    n += len(part)
        else:
            data = os.urandom(a.chunk)
            async with s.post(f"{base}/upload", data=body_gen(a.seconds, data),
                              headers={"Content-Type": "application/octet-stream"}) as r:
                r.raise_for_status()
                n = (await r.json())["bytes"]
        return n, time.monotonic() - t0


async def run_httpx(a, base, ctx):
    import httpx
    async with httpx.AsyncClient(verify=ctx if ctx else False, timeout=None) as c:
        t0 = time.monotonic()
        if a.dir == "down":
            n = 0
            async with c.stream("GET", f"{base}/download", params={"seconds": a.seconds, "chunk": a.chunk}) as r:
                r.raise_for_status()
                async for part in r.aiter_raw():
                    n += len(part)
        else:
            data = os.urandom(a.chunk)
            r = await c.post(f"{base}/upload", content=body_gen(a.seconds, data),
                             headers={"Content-Type": "application/octet-stream"})
            r.raise_for_status()
            n = r.json()["bytes"]
        return n, time.monotonic() - t0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--impl", choices=["aiohttp", "httpx"], default="aiohttp")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--dir", choices=["down", "up"], default="down")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--chunk", type=int, default=1 << 20)
    p.add_argument("--tls", action="store_true")
    p.add_argument("--loop", choices=["uvloop", "asyncio"], default="uvloop")
    a = p.parse_args()
    scheme = "https" if a.tls else "http"
    base = f"{scheme}://{a.host}:{a.port}"
    ctx = None
    if a.tls:
        ctx = ssl.create_default_context(cafile=os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", "cert.pem"))
    fn = run_aiohttp if a.impl == "aiohttp" else run_httpx
    c0 = cpu_self()
    if a.loop == "uvloop":
        import uvloop
        n, secs = uvloop.run(fn(a, base, ctx))
    else:
        n, secs = asyncio.run(fn(a, base, ctx))
    print(json.dumps({"tool": "py_http_client", "impl": a.impl, "dir": a.dir, "tls": a.tls, "loop": a.loop,
                      "chunk": a.chunk, "bytes": n, "seconds": secs, "gbps": n * 8 / secs / 1e9,
                      "client_cpu_self_s": cpu_self() - c0}))


if __name__ == "__main__":
    main()
