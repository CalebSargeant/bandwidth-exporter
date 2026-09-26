#!/usr/bin/env python3
"""Minimal raw-TCP throughput sender/receiver in Python.

Protocol (shared with gobench.go):
  client -> server: 64-byte ASCII header "<UP|DOWN> <seconds> <chunk>" padded with spaces.
  UP   : client sends for <seconds>, then shutdown(SHUT_WR); server counts bytes to EOF and
         replies with an 8-byte big-endian byte count.
  DOWN : server sends for <seconds>, then closes; client counts bytes to EOF.
The client prints one JSON line: bytes, seconds, gbps, cpu seconds (getrusage) and settings.

Implementations (--impl):
  blocking  : blocking sockets, sendall(memoryview) / recv_into(preallocated buffer); thread per conn.
  sendfile  : like blocking, but the sender uses os.sendfile() from a random file (zero-copy).
  asyncio   : non-blocking sockets driven by loop.sock_sendall / loop.sock_recv_into.
  uvloop    : same code as asyncio, run on uvloop.
  proto     : asyncio (uvloop) Transports: BufferedProtocol receive + transport.write with flow control.
"""
import argparse
import asyncio
import json
import os
import resource
import socket
import struct
import sys
import tempfile
import threading
import time

HDR = 64


def cpu_self():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def make_payload(chunk):
    return memoryview(bytearray(os.urandom(chunk)))


def tune(sock, bufsize):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if bufsize:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, bufsize)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, bufsize)


def parse_hdr(raw):
    d, secs, chunk = raw.decode().split()
    return d, float(secs), int(chunk)


_sendfile_path = None


def sendfile_source(chunk):
    """A 64 MiB random file (in page cache) used by os.sendfile()."""
    global _sendfile_path
    if _sendfile_path is None:
        fd, path = tempfile.mkstemp(prefix="bwbench_", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        size = 64 << 20
        block = os.urandom(1 << 20)
        with os.fdopen(fd, "wb") as f:
            for _ in range(size >> 20):
                f.write(block)
        _sendfile_path = path
    return _sendfile_path


# ---------------------------------------------------------------- blocking / sendfile
def b_send(sock, secs, chunk, use_sendfile):
    deadline = time.monotonic() + secs
    sent = 0
    if use_sendfile:
        path = sendfile_source(chunk)
        fsize = os.path.getsize(path)
        with open(path, "rb") as f:
            fd, out, off = f.fileno(), sock.fileno(), 0
            while time.monotonic() < deadline:
                n = os.sendfile(out, fd, off, chunk)
                sent += n
                off = (off + n) % (fsize - chunk)
    else:
        mv = make_payload(chunk)
        while time.monotonic() < deadline:
            sock.sendall(mv)
            sent += chunk
    return sent


def b_recv(sock, chunk):
    buf = bytearray(chunk)
    mv = memoryview(buf)
    total = 0
    while True:
        n = sock.recv_into(mv)
        if not n:
            return total
        total += n


def b_recv_exact(sock, n):
    data = b""
    while len(data) < n:
        part = sock.recv(n - len(data))
        if not part:
            raise ConnectionError("short read")
        data += part
    return data


def b_handle(conn, use_sendfile, bufsize):
    with conn:
        tune(conn, bufsize)
        d, secs, chunk = parse_hdr(b_recv_exact(conn, HDR))
        if d == "UP":
            n = b_recv(conn, chunk)
            conn.sendall(struct.pack("!Q", n))
        else:
            b_send(conn, secs, chunk, use_sendfile)
            conn.shutdown(socket.SHUT_WR)


def b_server(host, port, use_sendfile, bufsize):
    ls = socket.create_server((host, port), reuse_port=False, backlog=128)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    while True:
        conn, _ = ls.accept()
        threading.Thread(target=b_handle, args=(conn, use_sendfile, bufsize), daemon=True).start()


def b_client_stream(host, port, d, secs, chunk, use_sendfile, bufsize, out, idx):
    s = socket.create_connection((host, port))
    tune(s, bufsize)
    s.sendall(f"{d} {secs} {chunk}".ljust(HDR).encode())
    t0 = time.monotonic()
    if d == "UP":
        b_send(s, secs, chunk, use_sendfile)
        s.shutdown(socket.SHUT_WR)
        n = struct.unpack("!Q", b_recv_exact(s, 8))[0]
    else:
        n = b_recv(s, chunk)
    out[idx] = (n, time.monotonic() - t0)
    s.close()


def b_client(a):
    out = [None] * a.parallel
    ths = [threading.Thread(target=b_client_stream,
                            args=(a.host, a.port, a.dir, a.seconds, a.chunk, a.impl == "sendfile", a.sockbuf, out, i))
           for i in range(a.parallel)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return sum(o[0] for o in out), max(o[1] for o in out)


# ---------------------------------------------------------------- asyncio sock_* API
async def a_send(loop, sock, secs, chunk):
    mv = make_payload(chunk)
    deadline = time.monotonic() + secs
    sent = 0
    while time.monotonic() < deadline:
        await loop.sock_sendall(sock, mv)
        sent += chunk
    return sent


async def a_recv(loop, sock, chunk):
    buf = bytearray(chunk)
    mv = memoryview(buf)
    total = 0
    while True:
        n = await loop.sock_recv_into(sock, mv)
        if not n:
            return total
        total += n


async def a_recv_exact(loop, sock, n):
    data = b""
    while len(data) < n:
        part = await loop.sock_recv(sock, n - len(data))
        if not part:
            raise ConnectionError("short read")
        data += part
    return data


async def a_handle(loop, conn, bufsize):
    try:
        tune(conn, bufsize)
        d, secs, chunk = parse_hdr(await a_recv_exact(loop, conn, HDR))
        if d == "UP":
            n = await a_recv(loop, conn, chunk)
            await loop.sock_sendall(conn, struct.pack("!Q", n))
        else:
            await a_send(loop, conn, secs, chunk)
            conn.shutdown(socket.SHUT_WR)
    finally:
        conn.close()


async def a_server(host, port, bufsize):
    loop = asyncio.get_running_loop()
    ls = socket.create_server((host, port), backlog=128)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.setblocking(False)
    while True:
        conn, _ = await loop.sock_accept(ls)
        conn.setblocking(False)
        loop.create_task(a_handle(loop, conn, bufsize))


async def a_client_stream(a):
    loop = asyncio.get_running_loop()
    s = socket.create_connection((a.host, a.port))
    s.setblocking(False)
    tune(s, a.sockbuf)
    await loop.sock_sendall(s, f"{a.dir} {a.seconds} {a.chunk}".ljust(HDR).encode())
    t0 = time.monotonic()
    if a.dir == "UP":
        await a_send(loop, s, a.seconds, a.chunk)
        s.shutdown(socket.SHUT_WR)
        n = struct.unpack("!Q", await a_recv_exact(loop, s, 8))[0]
    else:
        n = await a_recv(loop, s, a.chunk)
    s.close()
    return n, time.monotonic() - t0


async def a_client(a):
    res = await asyncio.gather(*[a_client_stream(a) for _ in range(a.parallel)])
    return sum(r[0] for r in res), max(r[1] for r in res)


# ---------------------------------------------------------------- asyncio Transports / BufferedProtocol
class RecvProto(asyncio.BufferedProtocol):
    """Server side. Receives the header, then either sinks (UP) or sources (DOWN) data."""

    def __init__(self, bufsize):
        self.buf = bytearray(1 << 20)
        self.mv = memoryview(self.buf)
        self.hdr = bytearray()
        self.total = 0
        self.bufsize = bufsize
        self.can_write = asyncio.Event()
        self.can_write.set()

    def connection_made(self, transport):
        self.t = transport
        tune(transport.get_extra_info("socket"), self.bufsize)

    def get_buffer(self, sizehint):
        return self.mv

    def buffer_updated(self, nbytes):
        if len(self.hdr) < HDR:
            need = HDR - len(self.hdr)
            self.hdr += self.mv[: min(need, nbytes)]
            extra = nbytes - min(need, nbytes)
            self.total += extra
            if len(self.hdr) == HDR:
                d, secs, chunk = parse_hdr(bytes(self.hdr))
                self.dir = d
                if d == "DOWN":
                    asyncio.get_running_loop().create_task(self.source(secs, chunk))
        else:
            self.total += nbytes

    def eof_received(self):
        if getattr(self, "dir", "UP") == "UP":
            self.t.write(struct.pack("!Q", self.total))
            self.t.close()
        return True

    def pause_writing(self):
        self.can_write.clear()

    def resume_writing(self):
        self.can_write.set()

    async def source(self, secs, chunk):
        payload = bytes(make_payload(chunk))
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            await self.can_write.wait()
            self.t.write(payload)
        self.t.write_eof()


class SendProto(asyncio.BufferedProtocol):
    """Client side for the proto implementation."""

    def __init__(self, a, done):
        self.a, self.done = a, done
        self.buf = bytearray(1 << 20)
        self.mv = memoryview(self.buf)
        self.total = 0
        self.reply = bytearray()
        self.can_write = asyncio.Event()
        self.can_write.set()

    def connection_made(self, transport):
        self.t = transport
        tune(transport.get_extra_info("socket"), self.a.sockbuf)
        transport.write(f"{self.a.dir} {self.a.seconds} {self.a.chunk}".ljust(HDR).encode())
        self.t0 = time.monotonic()
        if self.a.dir == "UP":
            asyncio.get_running_loop().create_task(self.source())

    async def source(self):
        payload = bytes(make_payload(self.a.chunk))
        deadline = time.monotonic() + self.a.seconds
        while time.monotonic() < deadline:
            await self.can_write.wait()
            self.t.write(payload)
        self.t.write_eof()

    def pause_writing(self):
        self.can_write.clear()

    def resume_writing(self):
        self.can_write.set()

    def get_buffer(self, sizehint):
        return self.mv

    def buffer_updated(self, nbytes):
        if self.a.dir == "UP":
            self.reply += self.mv[:nbytes]
        else:
            self.total += nbytes

    def eof_received(self):
        if self.a.dir == "UP":
            self.total = struct.unpack("!Q", bytes(self.reply[:8]))[0]
        if not self.done.done():
            self.done.set_result((self.total, time.monotonic() - self.t0))
        return False

    def connection_lost(self, exc):
        if not self.done.done():
            self.done.set_result((self.total, time.monotonic() - self.t0))


async def p_server(host, port, bufsize):
    loop = asyncio.get_running_loop()
    srv = await loop.create_server(lambda: RecvProto(bufsize), host, port, reuse_address=True, backlog=128)
    await srv.serve_forever()


async def p_client(a):
    loop = asyncio.get_running_loop()
    futs = []
    for _ in range(a.parallel):
        done = loop.create_future()
        await loop.create_connection(lambda d=done: SendProto(a, d), a.host, a.port)
        futs.append(done)
    res = await asyncio.gather(*futs)
    return sum(r[0] for r in res), max(r[1] for r in res)


# ---------------------------------------------------------------- main
def run_async(coro_fn, impl):
    if impl in ("uvloop", "proto"):
        import uvloop
        return uvloop.run(coro_fn)
    return asyncio.run(coro_fn)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("role", choices=["server", "client"])
    p.add_argument("--impl", choices=["blocking", "sendfile", "asyncio", "uvloop", "proto"], default="blocking")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5301)
    p.add_argument("--dir", choices=["UP", "DOWN"], default="UP")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--chunk", type=int, default=1 << 20)
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--sockbuf", type=int, default=0, help="SO_SNDBUF/SO_RCVBUF; 0 = kernel autotuning")
    a = p.parse_args()

    if a.role == "server":
        import signal
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # run the finally-block cleanup on terminate()
        try:
            if a.impl in ("blocking", "sendfile"):
                b_server(a.host, a.port, a.impl == "sendfile", a.sockbuf)
            elif a.impl == "proto":
                run_async(p_server(a.host, a.port, a.sockbuf), a.impl)
            else:
                run_async(a_server(a.host, a.port, a.sockbuf), a.impl)
        except KeyboardInterrupt:
            pass
        finally:
            if _sendfile_path:
                os.unlink(_sendfile_path)
        return

    c0 = cpu_self()
    try:
        if a.impl in ("blocking", "sendfile"):
            n, secs = b_client(a)
        elif a.impl == "proto":
            n, secs = run_async(p_client(a), a.impl)
        else:
            n, secs = run_async(a_client(a), a.impl)
    finally:
        if _sendfile_path:
            os.unlink(_sendfile_path)
    print(json.dumps({"tool": "py_tcp", "impl": a.impl, "dir": a.dir, "parallel": a.parallel, "chunk": a.chunk,
                      "bytes": n, "seconds": secs, "gbps": n * 8 / secs / 1e9,
                      "client_cpu_self_s": cpu_self() - c0}))


if __name__ == "__main__":
    main()
