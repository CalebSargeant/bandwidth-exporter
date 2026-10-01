"""The peer control protocol: HMAC-signed HTTP between an agent and a responder.

A responder hands out short slots for east/west tests and must never become an open bandwidth
sink, so every request carries an HMAC-SHA256 signature over the method, path, peer id, a
timestamp, a nonce and a hash of the body (which holds the requested engine, streams, duration
and bytes). The responder checks the signature, a 60 s clock window and a nonce cache, so a
captured request cannot be replayed. Nothing in the protocol is secret, so it runs over plain
HTTP on the pod network or a VPN; the signature is what authenticates.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import parse_host_port

HEADER_PEER = "X-Bwexp-Peer"
HEADER_TIME = "X-Bwexp-Timestamp"
HEADER_NONCE = "X-Bwexp-Nonce"
HEADER_SIGNATURE = "X-Bwexp-Signature"
MAX_SKEW = 60.0
MIN_KEY_LENGTH = 16
TOKEN_BYTES = 16


def canonical(method: str, path: str, peer: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), path, peer, timestamp, nonce, digest]).encode()


def sign(
    key: bytes, method: str, path: str, peer: str, timestamp: str, nonce: str, body: bytes
) -> str:
    return hmac.new(
        key, canonical(method, path, peer, timestamp, nonce, body), hashlib.sha256
    ).hexdigest()


def signed_headers(
    key: bytes, method: str, path: str, peer: str, body: bytes, now: float | None = None
) -> dict[str, str]:
    timestamp = str(int(now if now is not None else time.time()))
    nonce = secrets.token_hex(16)
    return {
        HEADER_PEER: peer,
        HEADER_TIME: timestamp,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: sign(key, method, path, peer, timestamp, nonce, body),
    }


class KeyStore:
    """The responder's keys: one key every peer uses, or one per peer id."""

    def __init__(self, shared: bytes | None = None, per_peer: Mapping[str, bytes] | None = None):
        self.shared = shared
        self.per_peer = dict(per_peer or {})

    @classmethod
    def parse(cls, value: str) -> KeyStore:
        text = value.strip()
        if not text:
            raise ValueError("no peer keys configured")
        if text.startswith("{"):
            data = json.loads(text)
            if not isinstance(data, dict) or not data:
                raise ValueError("peer keys must be a JSON object of peer id to key")
            keys = {str(k): str(v).encode() for k, v in data.items()}
            for peer, key in keys.items():
                _check_key(key, f"the key for {peer}")
            return cls(per_peer=keys)
        key = text.encode()
        _check_key(key, "the shared peer key")
        return cls(shared=key)

    def key_for(self, peer: str) -> bytes | None:
        return self.per_peer.get(peer, self.shared)


def _check_key(key: bytes, what: str) -> None:
    if len(key) < MIN_KEY_LENGTH:
        raise ValueError(f"{what} is shorter than {MIN_KEY_LENGTH} characters")


def agent_key(value: str | None, env: str) -> bytes:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"${env} is empty: east/west tests need a peer key")
    key = text.encode()
    _check_key(key, f"${env}")
    return key


class NonceCache:
    """Nonces seen inside the clock window; a repeat is a replay."""

    def __init__(self, ttl: float = 2 * MAX_SKEW, limit: int = 100_000) -> None:
        self.ttl = ttl
        self.limit = limit
        self._seen: dict[str, float] = {}

    def check_and_add(self, nonce: str, now: float) -> bool:
        """True for a fresh nonce (and remembers it), False for a replay."""
        if len(self._seen) > self.limit or len(self._seen) % 256 == 0:
            self._seen = {n: t for n, t in self._seen.items() if t > now}
        if nonce in self._seen and self._seen[nonce] > now:
            return False
        self._seen[nonce] = now + self.ttl
        return True


@dataclass(frozen=True)
class Verdict:
    ok: bool
    peer: str = ""
    reason: str = ""  # auth | forbidden | replay

    @property
    def status(self) -> int:
        return 403 if self.reason == "forbidden" else 401


def verify(
    headers: Mapping[str, str],
    method: str,
    path: str,
    body: bytes,
    keys: KeyStore,
    nonces: NonceCache,
    allowed: tuple[str, ...] = (),
    now: float | None = None,
) -> Verdict:
    lowered = {k.lower(): v for k, v in headers.items()}
    peer = lowered.get(HEADER_PEER.lower(), "")
    timestamp = lowered.get(HEADER_TIME.lower(), "")
    nonce = lowered.get(HEADER_NONCE.lower(), "")
    signature = lowered.get(HEADER_SIGNATURE.lower(), "")
    if not (peer and timestamp and nonce and signature):
        return Verdict(False, peer, "auth")
    key = keys.key_for(peer)
    if key is None:
        return Verdict(False, peer, "auth")
    expected = sign(key, method, path, peer, timestamp, nonce, body)
    if not hmac.compare_digest(expected, signature):
        return Verdict(False, peer, "auth")
    current = now if now is not None else time.time()
    try:
        skew = abs(current - float(timestamp))
    except ValueError:
        return Verdict(False, peer, "auth")
    if skew > MAX_SKEW:
        return Verdict(False, peer, "auth")
    if not nonces.check_and_add(nonce, current):
        return Verdict(False, peer, "replay")
    if allowed and peer not in allowed:
        return Verdict(False, peer, "forbidden")
    return Verdict(True, peer)


class ControlError(Exception):
    def __init__(
        self, status: int, message: str, retry_after: float | None = None, reason: str = ""
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.reason = reason


@dataclass(frozen=True)
class Slot:
    id: str
    port: int
    token: bytes
    engine: str
    direction: str
    expires_in: float


class ControlClient:
    """The agent's side, blocking (it runs in the worker process or a thread)."""

    def __init__(self, address: str, self_id: str, key: bytes, timeout: float = 5.0) -> None:
        self.host, self.port = parse_host_port(address, default_port=None)
        self.self_id = self_id
        self.key = key
        self.timeout = timeout

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = b"" if payload is None else json.dumps(payload, sort_keys=True).encode()
        headers = signed_headers(self.key, method, path, self.self_id, body)
        headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, path, body=body or None, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        finally:
            conn.close()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if response.status >= 400:
            retry = response.headers.get("Retry-After")
            raise ControlError(
                response.status,
                str(data.get("error") or f"HTTP {response.status}"),
                float(retry) if retry and retry.replace(".", "", 1).isdigit() else None,
                str(data.get("reason") or ""),
            )
        return data

    def info(self) -> dict[str, Any]:
        return self._request("GET", "/v1/info")

    def open_slot(
        self, engine: str, direction: str, streams: int, duration: float, max_bytes: int
    ) -> Slot:
        data = self._request(
            "POST",
            "/v1/slots",
            {
                "engine": engine,
                "direction": direction,
                "streams": streams,
                "duration": duration,
                "max_bytes": max_bytes,
            },
        )
        return Slot(
            id=str(data["slot"]),
            port=int(data["port"]),
            token=bytes.fromhex(str(data["token"])),
            engine=str(data["engine"]),
            direction=str(data["direction"]),
            expires_in=float(data["expires_in"]),
        )

    def close_slot(self, slot_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/slots/{slot_id}")
