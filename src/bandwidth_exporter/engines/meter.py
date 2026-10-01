"""Byte counting for parallel streams, and the loop that runs them through the phases.

Downloads count the bytes the application read. Uploads count the bytes the far end's TCP
acknowledged (Linux TCP_INFO), so a socket buffer full of unsent data never inflates a result;
without TCP_INFO they fall back to the bytes written.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import tcpinfo
from .phases import PhaseController

TICK = 0.1


@dataclass
class StreamMeter:
    """One stream's byte counts. Upload bytes come from TCP_INFO when the kernel provides it."""

    use_tcp_info: bool
    app_bytes: int = 0
    closed_acked: int = 0
    retransmits: int = 0
    sock: socket.socket | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def attach(self, sock: socket.socket) -> None:
        with self.lock:
            self.sock = sock

    def detach(self, sock: socket.socket | None) -> None:
        if sock is None:
            return
        with self.lock:
            info = tcpinfo.read(sock) if self.use_tcp_info else None
            if info is not None:
                self.closed_acked += info.bytes_acked or 0
                self.retransmits += info.total_retrans or 0
            if self.sock is sock:
                self.sock = None

    def acked(self) -> int:
        """Bytes the far end acknowledged so far, across this stream's connections."""
        with self.lock:
            live = 0
            if self.sock is not None:
                info = tcpinfo.read(self.sock)
                live = (info.bytes_acked or 0) if info is not None else 0
            return self.closed_acked + live

    def abort(self) -> None:
        """Unblock the stream thread. Calls the plain socket's shutdown, because
        SSLSocket.shutdown would also drop the SSL object under the thread's feet."""
        with self.lock:
            if self.sock is not None:
                with contextlib.suppress(OSError):
                    socket.socket.shutdown(self.sock, socket.SHUT_RDWR)


def meters_for(direction: str, count: int) -> list[StreamMeter]:
    use_tcp_info = direction == "upload" and tcpinfo.supported()
    return [StreamMeter(use_tcp_info=use_tcp_info) for _ in range(count)]


def counts_at_receiver(direction: str) -> bool:
    return direction == "download" or tcpinfo.supported()


def receiver_bytes(direction: str, meters: Sequence[StreamMeter]) -> int:
    if direction == "upload" and tcpinfo.supported():
        return sum(m.acked() for m in meters)
    return sum(m.app_bytes for m in meters)


def app_bytes(meters: Sequence[StreamMeter]) -> int:
    return sum(m.app_bytes for m in meters)


def retransmits(direction: str, meters: Sequence[StreamMeter]) -> int | None:
    if direction == "upload" and tcpinfo.supported():
        return sum(m.retransmits for m in meters)
    return None


def drive(
    direction: str,
    threads: Sequence[threading.Thread],
    meters: Sequence[StreamMeter],
    controller: PhaseController,
    stop: threading.Event,
    errors: list[tuple[str, str]],
    join_timeout: float = 15.0,
) -> None:
    """Start the stream threads and sample them until the phase controller is done, a stream
    reports an error, or every stream has ended. Then stop and join them."""
    for thread in threads:
        thread.start()
    try:
        while not controller.observe(time.monotonic(), receiver_bytes(direction, meters)):
            if errors:
                break
            if not any(thread.is_alive() for thread in threads):
                # The far end stopped sending or receiving: measure what there is.
                controller.force_finish(time.monotonic(), receiver_bytes(direction, meters))
                break
            time.sleep(TICK)
    finally:
        stop.set()
        for meter in meters:
            meter.abort()
        for thread in threads:
            thread.join(timeout=join_timeout)
