import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from bandwidth_exporter.engines import iperf3

from .conftest import make_spec

FIXTURES = Path(__file__).parent / "fixtures"


def spec(**fields):
    fields.setdefault("target", "iperf.example.net:5202")
    return make_spec("own", "iperf3", **fields).worker_spec()


def test_command_is_an_argument_list():
    args = iperf3.command(
        spec(streams=8, ip_family="ipv4", bind_address="10.0.0.2", congestion_control="bbr"),
        "download",
        "/usr/local/bin/iperf3",
    )
    assert args[:4] == ["/usr/local/bin/iperf3", "--client", "iperf.example.net", "--port"]
    assert args[4] == "5202"
    assert "--reverse" in args
    assert args[args.index("--parallel") + 1] == "8"
    assert args[args.index("--omit") + 1] == "2"  # auto warm-up is 2 s for iperf3
    assert args[args.index("--time") + 1] == "10"
    assert args[args.index("--bind") + 1] == "10.0.0.2"
    assert args[args.index("--congestion") + 1] == "bbr"
    assert "-4" in args
    assert "--bidir" not in args
    assert "--reverse" not in iperf3.command(spec(), "upload", "iperf3")


def test_ipv6_target():
    args = iperf3.command(spec(target="[2001:db8::5]:5201"), "upload", "iperf3")
    assert args[2] == "2001:db8::5"


def test_parse_upload_uses_the_receiver_sum():
    document = json.loads((FIXTURES / "iperf3_upload.json").read_text())
    result, moved = iperf3.parse(document, "upload")
    assert result.bytes == 1_172_000_000
    assert result.seconds == pytest.approx(10.02)
    assert result.bytes_per_second == pytest.approx(935728542.9 / 8)
    assert result.retransmits == 3
    assert result.latency_seconds == pytest.approx(0.012)
    # Omitted warm-up intervals still moved data.
    assert moved == 60_000_000 + 115_000_000 + 2 * 587_500_000


def test_parse_download_has_no_local_rtt():
    document = json.loads((FIXTURES / "iperf3_upload.json").read_text())
    result, _ = iperf3.parse(document, "download")
    assert result.latency_seconds is None


def test_parse_rejects_documents_without_results():
    with pytest.raises(ValueError):
        iperf3.parse({"end": {}}, "upload")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("error - the server is busy running a test. try again later", "peer_busy"),
        ("error - unable to connect to server: Connection refused", "connect"),
        ("error - control socket has closed unexpectedly", "tool_error"),
        ("error - unable to receive control message: Connection timed out", "timeout"),
    ],
)
def test_classify(error, reason):
    assert iperf3.classify(error) == reason


def test_busy_server_fails_the_run(monkeypatch):
    busy = (FIXTURES / "iperf3_busy.json").read_text()
    monkeypatch.setattr(iperf3.shutil, "which", lambda name: "/usr/bin/iperf3")
    monkeypatch.setattr(iperf3, "version", lambda path: "3.22")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout=busy, stderr="")

    monkeypatch.setattr(iperf3.subprocess, "run", fake_run)
    result = iperf3.run(spec())
    assert result.status == "failure"
    assert result.reason == "peer_busy"
    assert result.info["tool_version"] == "3.22"


def test_both_directions(monkeypatch):
    document = (FIXTURES / "iperf3_upload.json").read_text()
    calls = []
    monkeypatch.setattr(iperf3.shutil, "which", lambda name: "/usr/bin/iperf3")
    monkeypatch.setattr(iperf3, "version", lambda path: "3.22")

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=document, stderr="")

    monkeypatch.setattr(iperf3.subprocess, "run", fake_run)
    result = iperf3.run(spec())
    assert result.status == "success"
    assert "--reverse" in calls[0]
    assert "--reverse" not in calls[1]
    assert result.download.bytes == result.upload.bytes == 1_172_000_000
    assert result.received_bytes == result.sent_bytes > 1_172_000_000


def test_missing_binary(monkeypatch):
    monkeypatch.setattr(iperf3.shutil, "which", lambda name: None)
    result = iperf3.run(spec())
    assert result.status == "failure"
    assert result.reason == "tool_error"


def test_timeout(monkeypatch):
    monkeypatch.setattr(iperf3.shutil, "which", lambda name: "/usr/bin/iperf3")
    monkeypatch.setattr(iperf3, "version", lambda path: "")

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(iperf3.subprocess, "run", fake_run)
    assert iperf3.run(spec()).reason == "timeout"


@pytest.mark.skipif(shutil.which("iperf3") is None, reason="iperf3 is not installed")
def test_against_a_real_iperf3_server():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    binary = shutil.which("iperf3") or "iperf3"
    server = subprocess.Popen(
        [binary, "--server", "--bind", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.5)
        result = iperf3.run(
            spec(
                target=f"127.0.0.1:{port}", warmup="1s", duration="1s", max_duration="5s", streams=2
            )
        )
        assert result.status == "success", result.message
        assert result.download.bytes_per_second > 0
        assert result.upload.bytes_per_second > 0
        assert result.upload.retransmits is not None
        assert result.info["tool_version"]
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_a_refused_connection_is_retried_once(monkeypatch):
    document = (FIXTURES / "iperf3_upload.json").read_text()
    refused = json.dumps({"error": "error - unable to connect to server: Connection refused"})
    answers = [refused, document, refused, refused]
    calls = []
    monkeypatch.setattr(iperf3.shutil, "which", lambda name: "/usr/bin/iperf3")
    monkeypatch.setattr(iperf3, "version", lambda path: "3.22")
    monkeypatch.setattr(iperf3, "CONNECT_RETRY_DELAY", 0.0)

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=answers.pop(0), stderr="")

    monkeypatch.setattr(iperf3.subprocess, "run", fake_run)
    result = iperf3.run(spec(directions=("download", "upload")))
    # download: refused, then fine; upload: refused twice, so the run fails on connect.
    assert len(calls) == 4
    assert result.status == "failure"
    assert result.reason == "connect"
    assert result.received_bytes > 0
