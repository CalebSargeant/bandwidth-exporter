"""Linux TCP_INFO: what the kernel knows about a connection.

`bytes_acked` is the byte count the receiver's TCP has acknowledged, so an upload measured with
it is measured at the receiver even though we are the sender, which is the rule that keeps
upload numbers honest (M-Lab once reported 134% of link capacity from bytes written into a
socket). Offsets follow include/uapi/linux/tcp.h; fields a kernel does not return read as None.
"""

from __future__ import annotations

import socket
import struct
import sys
from dataclasses import dataclass

TCP_INFO = getattr(socket, "TCP_INFO", 11)
_REQUEST = 232

_FIELDS = {
    "rtt_us": (68, "=I"),
    "total_retrans": (100, "=I"),
    "bytes_acked": (120, "=Q"),
    "bytes_received": (128, "=Q"),
    "min_rtt_us": (148, "=I"),
}


@dataclass(frozen=True)
class TcpInfo:
    rtt_us: int | None
    total_retrans: int | None
    bytes_acked: int | None
    bytes_received: int | None
    min_rtt_us: int | None


def parse(raw: bytes) -> TcpInfo:
    values: dict[str, int | None] = {}
    for name, (offset, fmt) in _FIELDS.items():
        size = struct.calcsize(fmt)
        values[name] = (
            struct.unpack_from(fmt, raw, offset)[0] if len(raw) >= offset + size else None
        )
    return TcpInfo(**values)


def supported() -> bool:
    return sys.platform.startswith("linux")


def read(sock: socket.socket) -> TcpInfo | None:
    if not supported():
        return None
    try:
        raw = sock.getsockopt(socket.IPPROTO_TCP, TCP_INFO, _REQUEST)
    except (OSError, ValueError):
        return None
    return parse(raw)


def congestion_control() -> str:
    """The kernel's default congestion control, recorded with every result."""
    try:
        with open("/proc/sys/net/ipv4/tcp_congestion_control", encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return ""
