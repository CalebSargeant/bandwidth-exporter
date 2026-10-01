"""Zones: where each instance runs, carried on every per-test series as `zone` and `peer_zone`."""

import textwrap

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from bandwidth_exporter import cli
from bandwidth_exporter.collector import BandwidthCollector
from bandwidth_exporter.config import (
    ConfigError,
    east_west_plans,
    enabled_tests,
    load_settings,
)
from bandwidth_exporter.model import Snapshot, TestState

CONFIG = """
    peer_id: K3S-Worker-PRD0
    zones:
      K3S-Worker-PRD0: amsterdam
      k3s-worker-prd1: rotterdam
      k3s-worker-prd2: rotterdam
    north_south:
      - {name: cloudflare, backend: cloudflare}
    east_west:
      - name: mesh
        peers:
          - {id: k3s-worker-prd1, address: "10.150.100.21:10057"}
          - {id: csam-all-git1, address: "10.150.0.100:10057"}
"""


def settings_from(tmp_path, text=CONFIG):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return load_settings(path)


def test_zones_are_keyed_by_lower_case_peer_id(tmp_path):
    settings = settings_from(tmp_path)
    assert settings.zones["k3s-worker-prd0"] == "amsterdam"
    assert settings.own_zone == "amsterdam"


def test_north_south_results_carry_the_testers_zone(tmp_path):
    (cloudflare,) = enabled_tests(settings_from(tmp_path))
    assert (cloudflare.zone, cloudflare.peer_zone) == ("amsterdam", "")
    assert cloudflare.labels == ("cloudflare", "north_south", "", "amsterdam", "")


def test_east_west_pairs_carry_both_zones(tmp_path):
    (plan,) = east_west_plans(settings_from(tmp_path))
    rotterdam = plan.pair("k3s-worker-prd1", "10.150.100.21:10057")
    assert rotterdam.labels == ("mesh", "east_west", "k3s-worker-prd1", "amsterdam", "rotterdam")
    # A peer missing from the map has no zone, and the label is empty rather than absent.
    runner = plan.pair("csam-all-git1", "10.150.0.100:10057")
    assert (runner.zone, runner.peer_zone) == ("amsterdam", "")


def test_without_zones_the_labels_are_empty(tmp_path):
    settings = settings_from(tmp_path, "north_south: [{name: cloudflare, backend: cloudflare}]\n")
    (cloudflare,) = enabled_tests(settings)
    assert settings.own_zone == ""
    assert cloudflare.labels == ("cloudflare", "north_south", "", "", "")


@pytest.mark.parametrize(
    ("zone", "message"),
    [('"Amsterdam"', "zone names"), ('"a b"', "zone names"), ('""', "zone names"), ("on", "quote")],
)
def test_zone_names_follow_the_name_rules(tmp_path, zone, message):
    path = tmp_path / "config.yaml"
    path.write_text(f"zones: {{k3s-worker-prd0: {zone}}}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_settings(path)


def test_series_are_labelled_with_the_zones(tmp_path):
    (plan,) = east_west_plans(settings_from(tmp_path))
    spec = plan.pair("k3s-worker-prd1", "10.150.100.21:10057")
    registry = CollectorRegistry(auto_describe=False)
    registry.register(BandwidthCollector(lambda: Snapshot(tests=(TestState(spec=spec),))))
    text = generate_latest(registry).decode()
    labels = [
        sample.labels
        for family in text_string_to_metric_families(text)
        for sample in family.samples
        if sample.name == "bandwidth_next_run_timestamp_seconds"
    ]
    assert labels == [
        {
            "test": "mesh",
            "kind": "east_west",
            "peer": "k3s-worker-prd1",
            "zone": "amsterdam",
            "peer_zone": "rotterdam",
        }
    ]


def test_check_config_prints_the_zones(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    assert cli.main(["--config", str(path), "check-config"]) == 0
    out = capsys.readouterr().out
    assert "zones: k3s-worker-prd0=amsterdam, k3s-worker-prd1=rotterdam" in out
    assert "this instance: amsterdam" in out
