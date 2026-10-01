"""The Cloudflare backend against a local fake of speed.cloudflare.com."""

import http.client
import socket
import struct

import pytest

from bandwidth_exporter.engines import cloudflare, tcpinfo
from bandwidth_exporter.model import ip_hash

from .conftest import cloudflare_spec


def test_download_and_upload_are_measured(edge):
    state, url = edge
    state.rate_per_connection = 10e6
    result = cloudflare.run(cloudflare_spec(url, streams=2))
    assert result.status == "success", result.message
    # Two throttled streams of 10 MB/s each.
    assert result.download.bytes_per_second == pytest.approx(20e6, rel=0.25)
    assert result.upload.bytes_per_second == pytest.approx(20e6, rel=0.3)
    assert result.download.seconds == pytest.approx(1.0, abs=0.25)
    assert result.download.latency_seconds is not None
    assert result.idle_latency_seconds is not None
    assert result.jitter_seconds is not None
    assert result.received_bytes >= result.download.bytes
    assert result.sent_bytes >= result.upload.bytes
    assert result.info["server"] == "AMS"
    assert result.info["backend"] == "cloudflare"
    assert result.public_ip == "192.0.2.10"
    assert ip_hash(result.public_ip) > 0
    assert result.wall_seconds > 0
    # Requests stay below Cloudflare's 100 MB cap, and the session cookie is sent back.
    assert all(
        "bytes=" not in path or int(path.split("bytes=")[1]) < 100_000_000
        for _, path in state.requests
    )
    assert any("_cfuvid=fake-session" in cookie for cookie in state.cookies_seen)


def test_upload_is_counted_at_the_receiver(edge):
    state, url = edge
    state.rate_per_connection = 5e6
    result = cloudflare.run(cloudflare_spec(url, directions=("upload",), streams=1))
    assert result.status == "success", result.message
    if tcpinfo.supported():
        assert result.upload.retransmits is not None
    # The receiver cannot have received more than the fake edge read.
    assert result.upload.bytes <= state.uploaded + 64 * 1024 * 4


def test_rate_limited_is_a_skip(edge):
    state, url = edge
    state.meta_status = 429
    state.retry_after = "3600"
    result = cloudflare.run(cloudflare_spec(url))
    assert result.status == "skipped"
    assert result.reason == "rate_limited"


def test_rate_limit_during_the_load(edge):
    state, url = edge
    state.down_status = 429
    state.retry_after = "120"
    result = cloudflare.run(cloudflare_spec(url, directions=("download",)))
    assert result.status == "skipped"
    assert result.reason == "rate_limited"
    assert result.idle_latency_seconds is not None


def test_forbidden_is_an_auth_failure(edge):
    state, url = edge
    state.meta_status = 403
    result = cloudflare.run(cloudflare_spec(url))
    assert result.status == "failure"
    assert result.reason == "auth"


def test_server_error_on_upload(edge):
    state, url = edge
    state.up_status = 503
    result = cloudflare.run(cloudflare_spec(url))
    assert result.status == "failure"
    assert result.reason == "protocol"
    assert "upload" in result.message
    # The download that already ran is counted for the budget.
    assert result.received_bytes > 0


def test_unreachable_endpoint():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = cloudflare.run(cloudflare_spec(f"http://127.0.0.1:{port}"))
    assert result.status == "failure"
    assert result.reason == "connect"


def test_unresolvable_endpoint():
    result = cloudflare.run(cloudflare_spec("http://no-such-host.invalid"))
    assert result.status == "failure"
    assert result.reason == "connect"


def test_colo_from_headers():
    message = http.client.HTTPMessage()
    message["cf-ray"] = "8f00aa11bb22cc33-FRA"
    assert cloudflare.colo(message) == "FRA"
    message["cf-meta-colo"] = "ams"
    assert cloudflare.colo(message) == "AMS"
    assert cloudflare.colo(http.client.HTTPMessage()) == ""


def test_retry_after_parsing():
    class Response:
        def __init__(self, value):
            self.headers = http.client.HTTPMessage()
            if value is not None:
                self.headers["Retry-After"] = value

    assert cloudflare.retry_after(Response("7")) == 7
    assert cloudflare.retry_after(Response(None)) == 5
    assert cloudflare.retry_after(Response("Wed, 21 Oct 2015 07:28:00 GMT")) == 0
    assert cloudflare.retry_after(Response("soon")) == 5


def test_tcpinfo_parse():
    raw = bytearray(232)
    struct.pack_into("=I", raw, 68, 1500)
    struct.pack_into("=I", raw, 100, 7)
    struct.pack_into("=Q", raw, 120, 123456789)
    struct.pack_into("=Q", raw, 128, 42)
    struct.pack_into("=I", raw, 148, 900)
    info = tcpinfo.parse(bytes(raw))
    assert (info.rtt_us, info.total_retrans, info.bytes_acked) == (1500, 7, 123456789)
    assert (info.bytes_received, info.min_rtt_us) == (42, 900)
    short = tcpinfo.parse(bytes(raw[:104]))
    assert short.total_retrans == 7
    assert short.bytes_acked is None
