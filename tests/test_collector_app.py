import random

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from bandwidth_exporter.app import create_app
from bandwidth_exporter.budget import Budget
from bandwidth_exporter.collector import BandwidthCollector
from bandwidth_exporter.config import Settings
from bandwidth_exporter.model import DirectionResult, RunResult, Snapshot, TestState
from bandwidth_exporter.scheduler import Scheduler

from .conftest import make_spec


def render(snapshot, **kwargs):
    registry = CollectorRegistry(auto_describe=False)
    registry.register(BandwidthCollector(lambda: snapshot, **kwargs))
    return generate_latest(registry).decode()


def samples(text):
    found = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            found[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return found


def lbl(**kwargs):
    base = {"test": "cf", "kind": "north_south", "peer": "", "zone": "", "peer_zone": ""}
    base.update(kwargs)
    kept = {"peer", "zone", "peer_zone"}
    return tuple(sorted((k, v) for k, v in base.items() if v != "" or k in kept))


def succeeded_state():
    spec = make_spec("cf", plan={"download": "1Gbit/s"})
    result = RunResult(
        status="success",
        download=DirectionResult(1_000_000, 2.0, 500_000.0, 0.025, None),
        upload=DirectionResult(400_000, 2.0, 200_000.0, 0.040, 4),
        idle_latency_seconds=0.012,
        jitter_seconds=0.002,
        sent_bytes=500_000,
        received_bytes=1_200_000,
        public_ip="192.0.2.10",
        info={"server": "AMS", "cca": "cubic", "ip_family": "ipv4", "method": "https"},
    )
    return TestState(spec=spec).with_result(result, 1000.0, 1010.0)


def test_no_result_series_before_the_first_success():
    text = render(Snapshot(tests=(TestState(spec=make_spec("cf")),)))
    found = samples(text)
    names = {name for name, _ in found}
    assert "bandwidth_download_bytes_per_second" not in names
    assert "bandwidth_last_test_success" not in names
    # Timestamps and counters exist (initialised) for every configured test.
    assert found[("bandwidth_last_success_timestamp_seconds", lbl())] == 0
    assert found[("bandwidth_test_failures_total", lbl(reason="timeout"))] == 0
    assert found[("bandwidth_tests_skipped_total", lbl(reason="budget"))] == 0
    assert found[("bandwidth_tests_total", lbl())] == 0


def test_results_in_base_units():
    found = samples(render(Snapshot(tests=(succeeded_state(),)), network_mode="pod"))
    assert found[("bandwidth_download_bytes_per_second", lbl())] == 500_000.0
    assert found[("bandwidth_upload_bytes_per_second", lbl())] == 200_000.0
    assert found[("bandwidth_download_bytes", lbl())] == 1_000_000
    assert found[("bandwidth_download_duration_seconds", lbl())] == 2.0
    assert found[("bandwidth_download_latency_seconds", lbl())] == 0.025
    assert found[("bandwidth_upload_retransmits", lbl())] == 4
    assert ("bandwidth_download_retransmits", lbl()) not in found
    assert found[("bandwidth_idle_latency_seconds", lbl())] == 0.012
    assert found[("bandwidth_jitter_seconds", lbl())] == 0.002
    assert found[("bandwidth_plan_download_bytes_per_second", lbl())] == 125e6
    assert ("bandwidth_plan_upload_bytes_per_second", lbl()) not in found
    assert found[("bandwidth_last_test_success", lbl())] == 1
    assert found[("bandwidth_last_success_timestamp_seconds", lbl())] == 1010.0
    assert found[("bandwidth_last_attempt_timestamp_seconds", lbl())] == 1000.0
    assert found[("bandwidth_sent_bytes_total", lbl())] == 500_000
    assert found[("bandwidth_received_bytes_total", lbl())] == 1_200_000
    assert found[("bandwidth_tests_total", lbl())] == 1


def test_public_ip_is_hashed_never_exported():
    text = render(Snapshot(tests=(succeeded_state(),)))
    assert "192.0.2.10" not in text
    found = samples(text)
    keys = [k for k in found if k[0] == "bandwidth_public_ip_hash"]
    assert keys == [("bandwidth_public_ip_hash", (("kind", "north_south"), ("test", "cf")))]


def test_info_metric():
    found = samples(render(Snapshot(tests=(succeeded_state(),)), network_mode="pod"))
    info = [labels for name, labels in found if name == "bandwidth_test_info"]
    assert len(info) == 1
    labels = dict(info[0])
    assert labels["server"] == "AMS"
    assert labels["backend"] == "cloudflare"
    assert labels["target"] == "speed.cloudflare.com"
    assert labels["streams"] == "4"
    assert labels["network_mode"] == "pod"
    assert labels["cca"] == "cubic"


def test_failure_keeps_results_and_flags_the_attempt():
    state = succeeded_state().with_result(RunResult.failure("connect", "down"), 2000.0, 2001.0)
    found = samples(render(Snapshot(tests=(state,))))
    assert found[("bandwidth_last_test_success", lbl())] == 0
    assert found[("bandwidth_download_bytes_per_second", lbl())] == 500_000.0
    assert found[("bandwidth_test_failures_total", lbl(reason="connect"))] == 1
    assert found[("bandwidth_last_success_timestamp_seconds", lbl())] == 1010.0


def test_process_metrics():
    snapshot = Snapshot(
        tests=(succeeded_state(),),
        in_progress="cf",
        queue_length=2,
        budget_limit_bytes=500,
        budget_transferred_bytes=100,
        on_demand={"accepted": 1, "conflict": 0, "rate_limited": 3, "unauthorised": 2},
    )
    found = samples(render(snapshot, cpu_quota_cores=1.5, iperf3_version="3.22", revision="abc"))
    assert found[("bandwidth_test_in_progress", ())] == 1
    assert found[("bandwidth_queue_length", ())] == 2
    assert found[("bandwidth_data_budget_bytes", ())] == 500
    assert found[("bandwidth_budget_period_transferred_bytes", ())] == 100
    assert found[("bandwidth_cpu_quota_cores", ())] == 1.5
    assert found[("bandwidth_on_demand_requests_total", (("result", "rate_limited"),))] == 3
    build = [labels for name, labels in found if name == "bandwidth_build_info"]
    assert dict(build[0])["iperf3_version"] == "3.22"


def test_only_one_test():
    other = TestState(spec=make_spec("other"))
    text = render(Snapshot(tests=(succeeded_state(), other)), only="cf")
    assert 'test="other"' not in text
    assert "bandwidth_queue_length" not in text


# --- the HTTP app ----------------------------------------------------------------------


class NeverRuns:
    async def run(self, spec):  # pragma: no cover - must not be called
        raise AssertionError("a request started a test")


def client_for(settings, token=None, specs=None):
    scheduler = Scheduler(
        specs if specs is not None else [make_spec("cf")],
        NeverRuns(),
        budget=Budget(limit=None),
        rng=random.Random(1),
        startup_delay=3600,
    )

    def factory(**kwargs):
        return BandwidthCollector(lambda: scheduler.snapshot, **kwargs)

    app = create_app(settings, scheduler, collector_factory=factory, token=token)
    return TestClient(app), scheduler


def test_endpoints_never_start_a_test():
    client, _ = client_for(Settings())
    with client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").status_code == 200
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "bandwidth_next_run_timestamp_seconds" in response.text
        assert "process_" in response.text or "python_info" in response.text
        probe = client.get("/probe", params={"target": "cf"})
        assert probe.status_code == 200
        assert "bandwidth_tests_total" in probe.text
        assert "bandwidth_queue_length" not in probe.text
        assert client.get("/probe", params={"target": "nope"}).status_code == 404
        tests = client.get("/api/v1/tests").json()
        assert tests["tests"][0]["name"] == "cf"
        assert client.get("/").status_code == 200


def test_openmetrics_negotiation():
    client, _ = client_for(Settings())
    with client:
        response = client.get(
            "/metrics", headers={"Accept": "application/openmetrics-text; version=1.0.0"}
        )
        assert response.headers["content-type"].startswith("application/openmetrics-text")
        assert response.text.rstrip().endswith("# EOF")


def test_readyz_fails_without_a_running_scheduler():
    client, scheduler = client_for(Settings())
    assert scheduler.healthy() is False
    assert client.get("/readyz").status_code == 503


def test_trigger_is_off_by_default():
    client, _ = client_for(Settings())
    with client:
        assert client.post("/api/v1/tests/cf/run").status_code == 404


@pytest.fixture
def trigger_client():
    settings = Settings(trigger={"enabled": True, "min_interval": "15m"})
    client, scheduler = client_for(settings, token="s3cret")
    with client:
        yield client, scheduler


def test_trigger_requires_the_token(trigger_client):
    client, scheduler = trigger_client
    assert client.post("/api/v1/tests/cf/run").status_code == 401
    bad = client.post("/api/v1/tests/cf/run", headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401
    basic = client.post("/api/v1/tests/cf/run", headers={"Authorization": "Basic s3cret"})
    assert basic.status_code == 401
    assert scheduler.snapshot.on_demand["unauthorised"] == 3


def test_trigger_outcomes(trigger_client, monkeypatch):
    client, scheduler = trigger_client
    auth = {"Authorization": "Bearer s3cret"}
    outcomes = iter(
        [("accepted", 0.0), ("conflict", 0.0), ("rate_limited", 12.2), ("unknown", 0.0)]
    )
    monkeypatch.setattr(scheduler, "trigger", lambda name: next(outcomes))
    assert client.post("/api/v1/tests/cf/run", headers=auth).status_code == 202
    assert client.post("/api/v1/tests/cf/run", headers=auth).status_code == 409
    limited = client.post("/api/v1/tests/cf/run", headers=auth)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "13"
    assert client.post("/api/v1/tests/zz/run", headers=auth).status_code == 404
