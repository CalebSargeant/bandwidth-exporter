"""The responder: answers peers' east/west tests, one authenticated slot at a time.

A peer asks for a slot over the signed control API. The responder admits it only outside
business hours, while it is not testing itself, and while a slot and a data port are free;
otherwise it answers 503 with Retry-After and the agent backs off. An admitted slot gets a
random token and a data port served by a single-use process: the built-in data server, or
`iperf3 -s -1` with its own idle, duration and bitrate limits. Either is killed at the slot's
deadline, whatever the peer does, and nothing a peer sends ever becomes command-line text.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import secrets
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .businesshours import BusinessHours
from .config import ResponderConfig, parse_host_port
from .control import KeyStore, NonceCache, verify
from .model import RESPONDER_REJECTIONS, ResponderStats

log = logging.getLogger(__name__)

NEWLINE = bytes([10])
READY_TIMEOUT = 5.0
IPERF3_IDLE_TIMEOUT = 15
BUSY_RETRY = 30.0
MAX_STREAMS = 32


class Exclusive:
    """One throughput test per process, whether this instance tests or answers a peer."""

    def __init__(self) -> None:
        self.holder: str | None = None
        self._free = asyncio.Event()
        self._free.set()

    def try_acquire(self, who: str) -> bool:
        if self.holder is not None:
            return False
        self.holder = who
        self._free.clear()
        return True

    async def acquire(self, who: str) -> None:
        while not self.try_acquire(who):
            await self._free.wait()

    def release(self) -> None:
        self.holder = None
        self._free.set()


@dataclass
class ActiveSlot:
    id: str
    peer: str
    engine: str
    direction: str
    port: int
    token: bytes
    expires_at: float
    process: asyncio.subprocess.Process
    done: asyncio.Event = field(default_factory=asyncio.Event)
    totals: dict[str, int] = field(default_factory=dict)
    watcher: asyncio.Task[None] | None = None


class Rejected(Exception):
    def __init__(self, status: int, reason: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retry_after = retry_after


class Responder:
    def __init__(
        self,
        config: ResponderConfig,
        self_id: str,
        keys: KeyStore,
        exclusive: Exclusive,
        hours: BusinessHours | None = None,
        *,
        python: str | None = None,
        iperf3: str = "iperf3",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.self_id = self_id
        self.keys = keys
        self.exclusive = exclusive
        self.hours = hours or BusinessHours()
        self.python = python or sys.executable
        self.iperf3 = iperf3
        self.clock = clock
        self.nonces = NonceCache()
        self._iperf3_help: str | None = None
        self.slots: dict[str, ActiveSlot] = {}
        # Slots that ended on their own, so a late release still gets the byte counts.
        self.finished: dict[str, ActiveSlot] = {}
        self.bind = parse_host_port(config.listen, default_port=None)[0]
        self.sessions_total = 0
        self.rejected = dict.fromkeys(RESPONDER_REJECTIONS, 0)
        self.sent_total = 0
        self.received_total = 0

    # --- the API's verbs ------------------------------------------------------------------

    def authenticate(self, headers: dict[str, str], method: str, path: str, body: bytes) -> str:
        verdict = verify(
            headers,
            method,
            path,
            body,
            self.keys,
            self.nonces,
            self.config.allowed_peers,
            now=self.clock(),
        )
        if not verdict.ok:
            self.rejected[verdict.reason] += 1
            raise Rejected(verdict.status, verdict.reason, "unauthenticated request")
        return verdict.peer

    def info(self) -> dict[str, Any]:
        return {
            "peer_id": self.self_id,
            "version": __version__,
            "engines": list(self.config.engines),
            "max_duration": self.config.max_duration,
            "max_bytes_per_test": self.config.max_bytes_per_test,
        }

    async def open(self, peer: str, request: dict[str, Any]) -> dict[str, Any]:
        engine, direction, streams, duration, max_bytes = self._validate(request)
        now = self.clock()
        if self.config.respect_business_hours and self.hours.is_blocked(now):
            self.rejected["business_hours"] += 1
            raise Rejected(
                503,
                "business_hours",
                "inside this peer's business hours",
                self.hours.blocked_until(now) - now,
            )
        if len(self.slots) >= self.config.max_concurrent_tests:
            self.rejected["busy"] += 1
            raise Rejected(503, "busy", "all slots are taken", BUSY_RETRY)
        if not self.slots and not self.exclusive.try_acquire("responder"):
            self.rejected["busy"] += 1
            raise Rejected(503, "busy", "this peer is running its own test", BUSY_RETRY)
        try:
            port = self._free_port()
            token = secrets.token_bytes(16)
            slot_id = secrets.token_hex(8)
            process = await self._spawn(
                engine, port, token, direction, streams, duration, max_bytes
            )
        except Rejected:
            self._release_exclusive()
            raise
        slot = ActiveSlot(
            id=slot_id,
            peer=peer,
            engine=engine,
            direction=direction,
            port=port,
            token=token,
            expires_at=now + duration,
            process=process,
        )
        self.slots[slot_id] = slot
        slot.watcher = asyncio.create_task(self._watch(slot, duration), name=f"slot-{slot_id}")
        self.sessions_total += 1
        log.info(
            "slot %s for %s: %s %s on port %d for %.0fs",
            slot_id,
            peer,
            engine,
            direction,
            port,
            duration,
        )
        return {
            "slot": slot_id,
            "port": port,
            "token": token.hex(),
            "engine": engine,
            "direction": direction,
            "expires_in": duration,
        }

    async def close(self, peer: str, slot_id: str) -> dict[str, Any]:
        slot = self.slots.get(slot_id) or self.finished.get(slot_id)
        if slot is None or slot.peer != peer:
            raise Rejected(404, "invalid", "no such slot")
        if slot.engine == "iperf3":
            _terminate(slot.process)
        elif slot.process.stdin is not None and not slot.process.stdin.is_closing():
            # The built-in server stops when its stdin closes, and reports its counts.
            slot.process.stdin.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(slot.done.wait(), timeout=3.0)
        if not slot.done.is_set():
            _terminate(slot.process)
            await slot.done.wait()
        return {"slot": slot_id, **slot.totals}

    async def shutdown(self) -> None:
        for slot in list(self.slots.values()):
            _terminate(slot.process)
        for slot in list(self.slots.values()):
            if slot.watcher is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await slot.watcher

    def stats(self) -> ResponderStats:
        return ResponderStats(
            sessions_total=self.sessions_total,
            rejected=dict(self.rejected),
            sent_bytes_total=self.sent_total,
            received_bytes_total=self.received_total,
            active_slots=len(self.slots),
        )

    # --- internals ----------------------------------------------------------------------

    def _validate(self, request: dict[str, Any]) -> tuple[str, str, int, float, int]:
        try:
            engine = str(request["engine"])
            direction = str(request["direction"])
            streams = int(request["streams"])
            duration = float(request["duration"])
            max_bytes = int(request.get("max_bytes") or 0)
        except (KeyError, TypeError, ValueError):
            self.rejected["invalid"] += 1
            raise Rejected(400, "invalid", "malformed slot request") from None
        if (
            engine not in self.config.engines
            or direction not in ("download", "upload")
            or not 1 <= streams <= MAX_STREAMS
            or not math.isfinite(duration)
            or duration <= 0
            or max_bytes < 0
        ):
            self.rejected["invalid"] += 1
            raise Rejected(400, "invalid", "unsupported slot request")
        duration = min(duration, self.config.max_duration)
        cap = self.config.max_bytes_per_test
        return engine, direction, streams, duration, min(max_bytes, cap) if max_bytes else cap

    def _free_port(self) -> int:
        used = {slot.port for slot in self.slots.values()}
        ports = self.config.data_ports
        for port in range(ports.first, ports.last + 1):
            if port not in used:
                return port
        self.rejected["no_port"] += 1
        raise Rejected(503, "no_port", "no data port is free", BUSY_RETRY)

    async def _spawn(
        self,
        engine: str,
        port: int,
        token: bytes,
        direction: str,
        streams: int,
        duration: float,
        max_bytes: int,
    ) -> asyncio.subprocess.Process:
        if engine == "builtin":
            args = [self.python, "-m", "bandwidth_exporter.dataplane"]
            payload = json.dumps(
                {
                    "port": port,
                    "bind": self.bind,
                    "token": token.hex(),
                    "direction": direction,
                    "streams": streams,
                    "duration": duration,
                    "max_bytes": max_bytes,
                }
            ).encode()
        else:
            binary = shutil.which(self.iperf3)
            if binary is None:
                self.rejected["engine_error"] += 1
                raise Rejected(503, "engine_error", "iperf3 is not installed here", BUSY_RETRY)
            args = [
                binary,
                "--server",
                "--one-off",
                "--port",
                str(port),
                "--idle-timeout",
                str(IPERF3_IDLE_TIMEOUT),
                "--forceflush",
            ]
            if "--server-max-duration" in await self._iperf3_options(binary):
                args += ["--server-max-duration", str(math.floor(duration))]
            if self.bind not in ("0.0.0.0", "::"):  # noqa: S104 - comparing, not binding
                args += ["--bind", self.bind]
            payload = b""
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self.rejected["engine_error"] += 1
            raise Rejected(503, "engine_error", f"cannot start the data server: {exc}") from None
        assert process.stdin is not None and process.stdout is not None  # noqa: S101 - PIPEs
        if payload:
            # One line; stdin stays open, and closing it later stops the server.
            process.stdin.write(payload + NEWLINE)
        else:
            process.stdin.close()
        ready = await self._wait_ready(process, engine)
        if not ready:
            _terminate(process)
            await process.wait()
            self.rejected["engine_error"] += 1
            raise Rejected(503, "engine_error", "the data server did not start", BUSY_RETRY)
        return process

    async def _iperf3_options(self, binary: str) -> str:
        """iperf3's --help text, read once: older builds lack some server limits."""
        if self._iperf3_help is None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    binary,
                    "--help",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                output, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                self._iperf3_help = output.decode(errors="replace")
            except (OSError, TimeoutError):
                self._iperf3_help = ""
        return self._iperf3_help

    async def _wait_ready(self, process: asyncio.subprocess.Process, engine: str) -> bool:
        assert process.stdout is not None  # noqa: S101 - created with a PIPE
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=deadline - time.monotonic()
                )
            except TimeoutError:
                return False
            if not line:
                return False
            text = line.decode(errors="replace").strip()
            if engine == "builtin":
                try:
                    return bool(json.loads(text).get("ready"))
                except ValueError:
                    continue
            if "listening" in text.lower():
                return True
        return False

    async def _watch(self, slot: ActiveSlot, duration: float) -> None:
        """Kill the data server at the deadline, collect its totals, free the slot."""
        # Drain stdout and stderr while waiting, so a chatty data server can never block on a
        # full pipe. Not communicate(): it would close stdin, which tells the server to stop.
        process = slot.process
        out = asyncio.create_task(process.stdout.read()) if process.stdout else None
        err = asyncio.create_task(process.stderr.read()) if process.stderr else None
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=duration + 2)
            except TimeoutError:
                _terminate(process)
                await process.wait()
            stdout = await out if out else b""
            stderr = await err if err else b""
            if stderr.strip():
                log.info("slot %s data server: %s", slot.id, stderr.decode(errors="replace")[-500:])
            slot.totals = _last_totals(stdout) if slot.engine == "builtin" else {}
            self.sent_total += slot.totals.get("sent", 0)
            self.received_total += slot.totals.get("received", 0)
        finally:
            self.slots.pop(slot.id, None)
            self.finished[slot.id] = slot
            while len(self.finished) > 64:
                self.finished.pop(next(iter(self.finished)))
            slot.done.set()
            if not self.slots:
                self._release_exclusive()
            log.info("slot %s closed: %s", slot.id, slot.totals or "no totals")

    def _release_exclusive(self) -> None:
        if self.exclusive.holder == "responder":
            self.exclusive.release()


def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


def _last_totals(stdout: bytes) -> dict[str, int]:
    for line in reversed(stdout.decode(errors="replace").strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "sent" in data:
            return {k: int(v) for k, v in data.items() if isinstance(v, int)}
    return {}
