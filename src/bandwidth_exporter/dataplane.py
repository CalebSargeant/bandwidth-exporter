"""The built-in engine's wire format and the responder's data server for one slot.

Each data connection opens with a 32-byte hello: a magic, the slot's random token, the stream
index and the direction. The server accepts only connections that present the token the
authenticated control API just issued, at most `streams` of them, for at most the slot's
duration and byte budget, and then exits. Payload is one reused random buffer; reads go
straight into a preallocated buffer. Blocking sockets, a thread per stream: the copy happens in
the kernel and releases the GIL, so this keeps up at multi-gigabit rates.

The parent writes the slot as one JSON line on stdin and keeps stdin open; closing it means
"stop now", which is how a released slot ends at once.

    python -m bandwidth_exporter.dataplane < slot.json
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field

MAGIC = b"BWX1"
HELLO_SIZE = 32
DIRECTIONS = {"download": 0, "upload": 1}
BUFFER = 1 << 20
IO_TIMEOUT = 10.0
HELLO_TIMEOUT = 5.0


def hello(token: bytes, index: int, direction: str) -> bytes:
    packet = MAGIC + token + struct.pack("!HB", index, DIRECTIONS[direction])
    return packet.ljust(HELLO_SIZE, b"\0")


def parse_hello(packet: bytes) -> tuple[bytes, int, str] | None:
    if len(packet) != HELLO_SIZE or not packet.startswith(MAGIC):
        return None
    token = packet[4:20]
    index, code = struct.unpack("!HB", packet[20:23])
    direction = next((name for name, value in DIRECTIONS.items() if value == code), None)
    return (token, index, direction) if direction else None


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return bytes(data)


@dataclass
class SlotServer:
    port: int
    token: bytes
    direction: str
    streams: int
    duration: float
    max_bytes: int
    bind: str = "0.0.0.0"  # noqa: S104 - peers connect from other hosts
    sent: int = 0
    received: int = 0
    accepted: int = 0
    rejected: int = 0
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    conns: list[socket.socket] = field(default_factory=list)

    def listen(self) -> socket.socket:
        family = socket.AF_INET6 if ":" in self.bind else socket.AF_INET
        listener = socket.create_server(
            (self.bind, self.port), family=family, backlog=self.streams + 8
        )
        listener.settimeout(0.25)
        return listener

    def _budget_left(self, count: int) -> bool:
        with self.lock:
            if self.direction == "download":
                self.sent += count
                return self.sent < self.max_bytes
            self.received += count
            return self.received < self.max_bytes

    def _serve(self, conn: socket.socket) -> None:
        try:
            if self.direction == "download":
                payload = memoryview(os.urandom(BUFFER))
                while not self.stop.is_set():
                    conn.sendall(payload)
                    if not self._budget_left(len(payload)):
                        break
            else:
                buffer = memoryview(bytearray(BUFFER))
                while not self.stop.is_set():
                    count = conn.recv_into(buffer)
                    if not count or not self._budget_left(count):
                        break
        except OSError:
            # The peer hung up or the slot was stopped: either ends this stream.
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def _admit(self, conn: socket.socket) -> bool:
        conn.settimeout(HELLO_TIMEOUT)
        try:
            parsed = parse_hello(recv_exact(conn, HELLO_SIZE))
        except OSError:
            parsed = None
        if parsed is None:
            return False
        token, _index, direction = parsed
        if not hmac.compare_digest(token, self.token) or direction != self.direction:
            return False
        conn.settimeout(IO_TIMEOUT)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return True

    def run(self, listener: socket.socket) -> dict[str, int]:
        deadline = time.monotonic() + self.duration
        threads: list[threading.Thread] = []
        with listener:
            while (
                time.monotonic() < deadline
                and self.accepted < self.streams
                and not self.stop.is_set()
            ):
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    if threads and not any(t.is_alive() for t in threads):
                        break
                    continue
                if not self._admit(conn):
                    self.rejected += 1
                    with contextlib.suppress(OSError):
                        conn.close()
                    continue
                self.accepted += 1
                self.conns.append(conn)
                thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
                thread.start()
                threads.append(thread)
        while (
            any(t.is_alive() for t in threads)
            and time.monotonic() < deadline
            and not self.stop.is_set()
        ):
            time.sleep(0.1)
        self.stop.set()
        for conn in self.conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
        for thread in threads:
            thread.join(timeout=IO_TIMEOUT)
        return {
            "sent": self.sent,
            "received": self.received,
            "streams": self.accepted,
            "rejected": self.rejected,
        }


def main() -> int:
    spec = json.loads(sys.stdin.readline())
    server = SlotServer(
        port=int(spec["port"]),
        token=bytes.fromhex(spec["token"]),
        direction=spec["direction"],
        streams=int(spec["streams"]),
        duration=float(spec["duration"]),
        max_bytes=int(spec["max_bytes"]),
        bind=spec.get("bind") or "0.0.0.0",  # noqa: S104
    )
    try:
        listener = server.listen()
    except OSError as exc:
        print(json.dumps({"ready": False, "error": str(exc)}), flush=True)
        return 1
    print(json.dumps({"ready": True}), flush=True)

    def stop_on_eof() -> None:
        sys.stdin.read()
        server.stop.set()

    threading.Thread(target=stop_on_eof, daemon=True).start()
    print(json.dumps(server.run(listener)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
