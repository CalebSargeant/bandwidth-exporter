"""A missing peer key disables that east/west test only; the responder's keys stay mandatory."""

import textwrap

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from bandwidth_exporter import cli
from bandwidth_exporter.collector import BandwidthCollector
from bandwidth_exporter.config import load_settings
from bandwidth_exporter.model import Snapshot

KEY = "k" * 32
CONFIG = """
    north_south:
      - {name: cloudflare, backend: cloudflare}
    east_west:
      - name: own-key
        peers: [{id: a, address: "10.0.0.1:10057"}]
        auth: {key_env: BWEXP_OWN_KEY}
      - name: shared-key
        peers: [{id: b, address: "10.0.0.2:10057"}]
        auth: {key_env: BWEXP_SHARED_KEY}
"""


def settings_from(tmp_path, text=CONFIG):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return load_settings(path)


def test_only_the_tests_without_a_key_are_keyless(tmp_path, monkeypatch):
    monkeypatch.setenv("BWEXP_OWN_KEY", KEY)
    monkeypatch.delenv("BWEXP_SHARED_KEY", raising=False)
    settings = settings_from(tmp_path)
    assert cli.keyless_tests(settings) == ("shared-key",)
    assert cli.missing_peer_keys(settings) == [
        "shared-key: the peer key (auth.key_env) is unset or under 16 characters"
    ]


def test_a_short_key_counts_as_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("BWEXP_OWN_KEY", KEY)
    monkeypatch.setenv("BWEXP_SHARED_KEY", "too-short")
    assert cli.keyless_tests(settings_from(tmp_path)) == ("shared-key",)


def test_the_responder_still_refuses_to_start_without_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("BWEXP_PEER_KEYS", raising=False)
    settings = settings_from(tmp_path, "responder: {enabled: true}\n")
    assert cli.serve(settings) == 2
    assert cli.missing_peer_keys(settings)[0].startswith("responder: the peer keys")


def test_disabled_tests_are_exported(tmp_path, monkeypatch):
    monkeypatch.setenv("BWEXP_OWN_KEY", KEY)
    monkeypatch.delenv("BWEXP_SHARED_KEY", raising=False)
    settings = settings_from(tmp_path)
    factory = cli.collector_factory(settings, scheduler=None, disabled=cli.keyless_tests(settings))
    registry = CollectorRegistry(auto_describe=False)
    registry.register(BandwidthCollector(lambda: Snapshot(), **factory.keywords))
    samples = [
        (sample.labels, sample.value)
        for family in text_string_to_metric_families(generate_latest(registry).decode())
        for sample in family.samples
        if sample.name == "bandwidth_test_disabled"
    ]
    assert samples == [({"test": "shared-key", "reason": "missing_key"}, 1.0)]


def test_nothing_disabled_means_no_series():
    registry = CollectorRegistry(auto_describe=False)
    registry.register(BandwidthCollector(lambda: Snapshot()))
    assert "bandwidth_test_disabled" not in generate_latest(registry).decode()
