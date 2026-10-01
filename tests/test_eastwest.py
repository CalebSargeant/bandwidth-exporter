"""East/west end to end: a real responder (control API and data servers) on loopback."""

from __future__ import annotations

import asyncio
import shutil
import socket
import threading
import time
from datetime import UTC, datetime
from datetime import time as clock_time

import pytest
import uvicorn

from bandwidth_exporter.businesshours import BusinessHours, Window
from bandwidth_exporter.cli import _quiet_server
from bandwidth_exporter.config import (
    Defaults,
    EastWestTest,
    ResponderConfig,
    resolve_east_west,
)
from bandwidth_exporter.control import ControlClient, ControlError, KeyStore
from bandwidth_exporter.control_api import create_control_app
from bandwidth_exporter.dataplane import HELLO_SIZE
from bandwidth_exporter.engines import builtin, iperf3
from bandwidth_exporter.peers import Directory, Peer, rendezvous
from bandwidth_exporter.responder import Exclusive, Responder

KEY = "0123456789abcdef0123456789abcdef"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def free_range(size: int = 4) -> tuple[int, int]:
    for _ in range(200):
        first = free_port()
        if first + size > 65535:
            continue
        socks = []
        try:
            for port in range(first, first + size):
                sock = socket.socket()
                sock.bind(("127.0.0.1", port))
                socks.append(sock)
            return first, first + size - 1
        except OSError:
            continue
        finally:
            for sock in socks:
                sock.close()
    raise RuntimeError("no free port range")


class ResponderThread:
    """The control API and its data servers, in their own thread and event loop."""

    def __init__(self, hours: BusinessHours | None = None, **config: object) -> None:
        self.port = free_port()
        first, last = free_range()
        settings = {
            "enabled": True,
            "listen": f"127.0.0.1:{self.port}",
            "data_ports": {"first": first, "last": last},
            "max_duration": "20s",
            # Loopback moves several GB a second: lift the cap unless a test sets one.
            "max_bytes_per_test": "1000GB",
        }
        settings.update(config)
        self.config = ResponderConfig.model_validate(settings)
        self.hours = hours
        self.started = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.responder: Responder | None = None
        self.server = None
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.exclusive = Exclusive()
        self.responder = Responder(
            self.config, "peer-b", KeyStore(shared=KEY.encode()), self.exclusive, self.hours
        )
        server_class = _quiet_server(uvicorn)
        self.server = server_class(
            uvicorn.Config(
                create_control_app(self.responder),
                host="127.0.0.1",
                port=self.port,
                log_config=None,
                access_log=False,
                lifespan="on",
            )
        )
        serving = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        self.started.set()
        await asyncio.gather(serving)

    def __enter__(self) -> ResponderThread:
        self.thread.start()
        assert self.started.wait(15), "the responder did not start"
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=20)

    def call(self, function, *args):
        """Run a plain function on the responder's loop and wait for it."""
        done = threading.Event()
        box = {}

        def runner():
            box["value"] = function(*args)
            done.set()

        self.loop.call_soon_threadsafe(runner)
        done.wait(5)
        return box.get("value")

    def wait_idle(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while self.responder.slots and time.monotonic() < deadline:
            time.sleep(0.05)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"


def agent_spec(address: str, backend: str = "builtin", **fields: object) -> dict:
    test = EastWestTest.model_validate(
        {
            "name": "mesh",
            "backend": backend,
            "peers": [{"id": "peer-b", "address": address}],
            "warmup": fields.pop("warmup", "0.3s"),
            "duration": fields.pop("duration", "1s"),
            "max_duration": fields.pop("max_duration", "4s"),
            "streams": fields.pop("streams", 2),
            "early_stop": False,
            "options": fields.pop("options", {"latency_samples": 3, "max_bytes": "1000GB"}),
            **fields,
        }
    )
    plan = resolve_east_west(test, Defaults(), "peer-a")
    return plan.pair("peer-b", address).worker_spec()


@pytest.fixture
def peer_key(monkeypatch):
    monkeypatch.setenv("BWEXP_PEER_KEY", KEY)


def test_builtin_download_and_upload(peer_key):
    with ResponderThread() as responder:
        result = builtin.run(agent_spec(responder.address))
        assert result.status == "success", result.message
        assert result.download.bytes_per_second > 0
        assert result.upload.bytes_per_second > 0
        assert result.download.seconds == pytest.approx(1.0, abs=0.3)
        assert result.idle_latency_seconds is not None
        assert result.info["backend"] == "builtin"
        assert result.received_bytes >= result.download.bytes
        assert result.sent_bytes >= result.upload.bytes
        responder.wait_idle()
        stats = responder.responder.stats()
        assert stats.sessions_total == 2
        assert stats.sent_bytes_total >= result.download.bytes
        assert stats.received_bytes_total > 0
        assert stats.active_slots == 0
        assert responder.exclusive.holder is None


def test_a_wrong_key_is_an_auth_failure(monkeypatch):
    monkeypatch.setenv("BWEXP_PEER_KEY", "not-the-right-key-at-all")
    with ResponderThread() as responder:
        result = builtin.run(agent_spec(responder.address))
        assert (result.status, result.reason) == ("failure", "auth")
        assert responder.responder.stats().rejected["auth"] == 1
        assert responder.responder.stats().sessions_total == 0


def test_a_missing_key_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("BWEXP_PEER_KEY", raising=False)
    result = builtin.run(agent_spec("127.0.0.1:9"))
    assert (result.status, result.reason) == ("failure", "auth")


def test_a_busy_peer_is_a_skip(peer_key):
    with ResponderThread() as responder:
        assert responder.call(responder.exclusive.try_acquire, "agent")
        result = builtin.run(agent_spec(responder.address))
        assert (result.status, result.reason) == ("skipped", "busy")
        assert responder.responder.stats().rejected["busy"] == 1
        responder.call(responder.exclusive.release)


def test_business_hours_at_the_peer(peer_key):
    today = datetime.now(UTC).weekday()
    hours = BusinessHours([Window(frozenset({today}), clock_time(0), clock_time(0))], "UTC")
    with ResponderThread(hours=hours) as responder:
        result = builtin.run(agent_spec(responder.address))
        assert (result.status, result.reason) == ("skipped", "peer_unavailable")
        assert responder.responder.stats().rejected["business_hours"] == 1


def test_data_ports_ignore_strangers(peer_key):
    with ResponderThread() as responder:
        client = ControlClient(responder.address, "peer-a", KEY.encode())
        slot = client.open_slot("builtin", "download", 1, 5, 0)
        with socket.create_connection(("127.0.0.1", slot.port), timeout=5) as stranger:
            stranger.sendall(b"BWX1" + bytes(HELLO_SIZE - 4))
            stranger.settimeout(5)
            assert stranger.recv(1) == b""  # closed without a byte of payload
        totals = client.close_slot(slot.id)
        assert totals["rejected"] == 1
        assert totals["sent"] == 0


def test_slot_requests_are_validated(peer_key):
    with ResponderThread(engines=["builtin"]) as responder:
        client = ControlClient(responder.address, "peer-a", KEY.encode())
        with pytest.raises(ControlError) as refused:
            client.open_slot("iperf3", "download", 1, 5, 0)
        assert refused.value.status == 400
        with pytest.raises(ControlError) as refused:
            client.open_slot("builtin", "sideways", 1, 5, 0)
        assert refused.value.status == 400
        with pytest.raises(ControlError) as refused:
            client.open_slot("builtin", "download", 99, 5, 0)
        assert refused.value.status == 400
        assert client.info()["peer_id"] == "peer-b"


def test_slots_are_capped_and_expire(peer_key):
    with ResponderThread(max_duration="8s") as responder:
        client = ControlClient(responder.address, "peer-a", KEY.encode())
        slot = client.open_slot("builtin", "download", 1, 60, 0)
        assert slot.expires_in == 8  # clamped to the responder's limit
        # Nobody connects: the data server gives up at the deadline and the slot is freed.
        deadline = time.monotonic() + 15
        while responder.responder.slots and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not responder.responder.slots
        assert responder.exclusive.holder is None


def test_the_byte_cap_ends_a_test_without_hanging(peer_key):
    with ResponderThread(max_bytes_per_test="2MB") as responder:
        started = time.monotonic()
        result = builtin.run(agent_spec(responder.address, directions=["download"]))
        assert time.monotonic() - started < 20
        # 2 MB is gone long before the warm-up ends on loopback: nothing to report.
        assert result.status == "failure"
        assert result.reason == "timeout"


@pytest.mark.skipif(shutil.which("iperf3") is None, reason="iperf3 is not installed")
def test_iperf3_through_the_responder(peer_key):
    with ResponderThread() as responder:
        spec = agent_spec(
            responder.address,
            backend="iperf3",
            warmup="1s",
            duration="1s",
            max_duration="5s",
            options={},
        )
        result = iperf3.run(spec)
        assert result.status == "success", result.message
        assert result.download.bytes_per_second > 0
        assert result.upload.bytes_per_second > 0
        assert result.idle_latency_seconds is not None
        responder.wait_idle()
        assert responder.responder.stats().sessions_total == 2


# --- peer selection --------------------------------------------------------------------


def test_rendezvous_is_stable_and_spreads():
    peers = [Peer(f"node-{i}", f"10.0.0.{i}:10057") for i in range(10)]
    first = rendezvous("node-0", peers, 3)
    assert first == rendezvous("node-0", list(reversed(peers)), 3)
    assert len(first) == 3
    # Removing a peer that was not chosen changes nothing.
    unchosen = next(p for p in peers if p not in first)
    assert rendezvous("node-0", [p for p in peers if p != unchosen], 3) == first
    # Different agents pick different sets.
    picks = {tuple(p.id for p in rendezvous(f"node-{i}", peers, 3)) for i in range(10)}
    assert len(picks) > 1
    assert len(rendezvous("x", peers, None)) == 10  # unset k: every peer


def test_directory_with_dns_discovery():
    test = EastWestTest.model_validate(
        {
            "name": "mesh",
            "discovery": {"dns": "peers.example", "port": 10057},
            "topology": {"random_peers": 2},
        }
    )
    plan = resolve_east_west(test, Defaults(), "node-a")
    ids = {
        "10.0.0.1:10057": "node-a",
        "10.0.0.2:10057": "node-b",
        "10.0.0.3:10057": "node-c",
        "10.0.0.4:10057": None,
    }
    directory = Directory(
        [plan],
        "node-a",
        key=lambda env: None,
        resolver=lambda name: ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"],
        fetch_id=lambda address: ids[address],
    )
    selected = asyncio.run(directory.refresh())["mesh"]
    assert {peer.id for peer in selected} == {"node-b", "node-c"}  # never itself
    assert {peer.address for peer in selected} == {"10.0.0.2:10057", "10.0.0.3:10057"}
    assert directory.interval() == 300


def test_directory_with_static_peers_skips_itself():
    test = EastWestTest.model_validate(
        {
            "name": "sites",
            "peers": [
                {"id": "node-a", "address": "10.0.0.1"},
                {"id": "node-b", "address": "10.0.0.2:9000"},
            ],
        }
    )
    plan = resolve_east_west(test, Defaults(), "node-a")
    directory = Directory([plan], "node-a", key=lambda env: None)
    selected = asyncio.run(directory.refresh())["sites"]
    assert selected == [Peer("node-b", "10.0.0.2:9000")]


def test_directory_identifies_peers_over_the_signed_api(peer_key):
    with ResponderThread() as responder:
        test = EastWestTest.model_validate(
            {"name": "mesh", "discovery": {"dns": "localhost", "port": responder.port}}
        )
        plan = resolve_east_west(test, Defaults(), "peer-a")
        directory = Directory(
            [plan], "peer-a", key=lambda env: KEY.encode(), resolver=lambda name: ["127.0.0.1"]
        )
        selected = asyncio.run(directory.refresh())["mesh"]
        assert selected == [Peer("peer-b", f"127.0.0.1:{responder.port}")]


def test_exclusive_is_shared():
    async def scenario():
        exclusive = Exclusive()
        assert exclusive.try_acquire("responder")
        assert not exclusive.try_acquire("agent")
        waiter = asyncio.create_task(exclusive.acquire("agent"))
        await asyncio.sleep(0)
        assert not waiter.done()
        exclusive.release()
        await asyncio.wait_for(waiter, 1)
        assert exclusive.holder == "agent"

    asyncio.run(scenario())
