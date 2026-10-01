"""Renders the current snapshot as Prometheus metrics.

Units are base units (bytes, seconds, ratios). Results are gauges of the most recent successful
run and stay in place through later failures; `bandwidth_last_test_success` says whether the
latest attempt worked, as blackbox_exporter's probe_success does, so `up` keeps meaning "the
exporter is alive". Context that explains a number (server, streams, congestion control) lives
in `bandwidth_test_info`, not on the result series. No sample carries a timestamp; freshness is
in the `*_timestamp_seconds` gauges.
"""

from __future__ import annotations

import platform
from collections.abc import Callable, Iterable, Iterator, Mapping

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    InfoMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector

from . import __version__
from .model import (
    FAILURE_REASONS,
    ON_DEMAND_RESULTS,
    RESPONDER_REJECTIONS,
    SKIP_REASONS,
    DirectionResult,
    ResponderStats,
    Snapshot,
    TestState,
)

LABELS = ["test", "kind", "peer", "zone", "peer_zone"]
INFO_LABELS = [
    "backend",
    "method",
    "target",
    "server",
    "streams",
    "cca",
    "network_mode",
    "ip_family",
    "tunnel",
    "relayed",
    "tool_version",
]
CACHED = " Cached: tests run on the exporter's own schedule, not per scrape."


def _gauge(name: str, doc: str, labels: list[str] | None = None) -> GaugeMetricFamily:
    return GaugeMetricFamily(name, doc, labels=LABELS if labels is None else labels)


class BandwidthCollector(Collector):
    def __init__(
        self,
        snapshot: Callable[[], Snapshot],
        *,
        network_mode: str = "",
        revision: str = "",
        iperf3_version: str = "",
        cpu_quota_cores: float | None = None,
        only: str | None = None,
        responder: Callable[[], ResponderStats | None] | None = None,
        peer_id: str = "",
        disabled: Mapping[str, str] | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._network_mode = network_mode
        self._revision = revision
        self._iperf3_version = iperf3_version
        self._cpu_quota = cpu_quota_cores
        self._only = only
        self._responder = responder
        self._peer_id = peer_id
        self._disabled = dict(disabled or {})

    def describe(self) -> Iterable[Metric]:
        # Unchecked collector: families depend on the snapshot.
        return []

    def collect(self) -> Iterator[Metric]:
        snap = self._snapshot()
        states = [
            s for s in snap.tests if self._only is None or self._only in (s.spec.name, s.spec.key)
        ]
        yield from self._results(states)
        yield from self._bookkeeping(states)
        if self._only is None:
            yield from self._process(snap)
            yield from self._responder_metrics()
            yield from self._disabled_tests()

    def _disabled_tests(self) -> Iterator[Metric]:
        if not self._disabled:
            return
        family = _gauge(
            "bandwidth_test_disabled",
            "1 for a configured test that is not running, by reason (missing_key: its peer "
            "key is unset or too short).",
            labels=["test", "reason"],
        )
        for name, reason in sorted(self._disabled.items()):
            family.add_metric([name, reason], 1.0)
        yield family

    # --- per-test results --------------------------------------------------------------

    def _results(self, states: list[TestState]) -> Iterator[Metric]:
        directions: dict[str, Callable[[DirectionResult], float | None]] = {
            "{}_bytes_per_second": lambda r: r.bytes_per_second,
            "{}_bytes": lambda r: r.bytes,
            "{}_duration_seconds": lambda r: r.seconds,
            "{}_latency_seconds": lambda r: r.latency_seconds,
            "{}_retransmits": lambda r: r.retransmits,
        }
        docs = {
            "{}_bytes_per_second": "Throughput of the most recent successful {} test, measured "
            "at the receiver with the warm-up excluded, in bytes per second.",
            "{}_bytes": "Payload bytes counted in the measured phase of the most recent "
            "successful {} test.",
            "{}_duration_seconds": "Length of the measured phase of the most recent successful "
            "{} test.",
            "{}_latency_seconds": "Median round-trip time while the {} direction was loaded "
            "(working latency), most recent successful test.",
            "{}_retransmits": "TCP segments retransmitted during the most recent successful {} "
            "test, where the engine reports them.",
        }
        for template, value in directions.items():
            for direction in ("download", "upload"):
                name = "bandwidth_" + template.format(direction)
                family = _gauge(name, docs[template].format(direction) + CACHED)
                for state in states:
                    result: DirectionResult | None = getattr(state, direction)
                    if result is None:
                        continue
                    measured = value(result)
                    if measured is not None:
                        family.add_metric(list(state.spec.labels), float(measured))
                yield family

        scalars = {
            "bandwidth_idle_latency_seconds": (
                "Median round-trip time before the load, most recent run.",
                lambda s: s.idle_latency_seconds,
            ),
            "bandwidth_jitter_seconds": (
                "Mean absolute difference between consecutive idle round-trip times.",
                lambda s: s.jitter_seconds,
            ),
            "bandwidth_packet_loss_ratio": (
                "Packet loss during the most recent successful test, where the engine reports it.",
                lambda s: s.packet_loss_ratio,
            ),
        }
        for name, (doc, getter) in scalars.items():
            family = _gauge(name, doc + CACHED)
            for state in states:
                measured = getter(state)
                if measured is not None:
                    family.add_metric(list(state.spec.labels), float(measured))
            yield family

        plan = {
            "bandwidth_plan_download_bytes_per_second": lambda s: s.spec.plan_download,
            "bandwidth_plan_upload_bytes_per_second": lambda s: s.spec.plan_upload,
        }
        for name, getter in plan.items():
            family = _gauge(name, "Contracted rate from configuration, for alerts.")
            for state in states:
                if getter(state) is not None:
                    family.add_metric(list(state.spec.labels), float(getter(state)))
            yield family

    # --- per-test bookkeeping ----------------------------------------------------------

    def _bookkeeping(self, states: list[TestState]) -> Iterator[Metric]:
        success = _gauge(
            "bandwidth_last_test_success",
            "1 if the most recent attempt succeeded, 0 if it failed. Absent before the first "
            "attempt.",
        )
        saturated = _gauge(
            "bandwidth_last_test_cpu_saturated",
            "1 when the tester used more than 90% of its CPU budget during the most recent run, "
            "so CPU rather than the network may have limited the result.",
        )
        last_success = _gauge(
            "bandwidth_last_success_timestamp_seconds",
            "Unix time the most recent successful test finished; 0 if none yet.",
        )
        last_attempt = _gauge(
            "bandwidth_last_attempt_timestamp_seconds",
            "Unix time the most recent attempt started; 0 if none yet.",
        )
        next_run = _gauge(
            "bandwidth_next_run_timestamp_seconds",
            "Unix time the next scheduled run is due.",
        )
        ip_hash = _gauge(
            "bandwidth_public_ip_hash",
            "CRC-32 of the public egress address seen by the far end. Changes when the address "
            "does.",
            labels=["test", "kind"],
        )
        tests = CounterMetricFamily(
            "bandwidth_tests", "Test runs attempted (skipped runs are not attempts).", labels=LABELS
        )
        failures = CounterMetricFamily(
            "bandwidth_test_failures", "Failed test runs by reason.", labels=[*LABELS, "reason"]
        )
        skipped = CounterMetricFamily(
            "bandwidth_tests_skipped",
            "Runs deliberately not made, by reason (budget, rate limiting, ...).",
            labels=[*LABELS, "reason"],
        )
        sent = CounterMetricFamily(
            "bandwidth_sent_bytes",
            "Every byte tests sent, warm-up, probes and failed runs included. The input to "
            "data-budget alerts.",
            labels=LABELS,
        )
        received = CounterMetricFamily(
            "bandwidth_received_bytes",
            "Every byte tests received, warm-up, probes and failed runs included.",
            labels=LABELS,
        )
        info = InfoMetricFamily(
            "bandwidth_test",
            "Context of each test's most recent run: backend, method, server and so on.",
            labels=LABELS,
        )
        for state in states:
            labels = list(state.spec.labels)
            if state.last_test_success is not None:
                success.add_metric(labels, 1.0 if state.last_test_success else 0.0)
                saturated.add_metric(labels, 1.0 if state.cpu_saturated else 0.0)
            last_success.add_metric(labels, state.last_success_time)
            last_attempt.add_metric(labels, state.last_attempt_time)
            next_run.add_metric(labels, state.next_run_time)
            if state.public_ip_hash is not None:
                ip_hash.add_metric(labels[:2], state.public_ip_hash)
            tests.add_metric(labels, state.tests_total)
            for reason in FAILURE_REASONS:
                failures.add_metric([*labels, reason], state.failures.get(reason, 0))
            for reason in SKIP_REASONS:
                skipped.add_metric([*labels, reason], state.skipped.get(reason, 0))
            sent.add_metric(labels, state.sent_bytes_total)
            received.add_metric(labels, state.received_bytes_total)
            info.add_metric(labels, self._info(state))
        yield from (success, saturated, last_success, last_attempt, next_run, ip_hash)
        yield from (tests, failures, skipped, sent, received, info)

    def _info(self, state: TestState) -> dict[str, str]:
        values = dict.fromkeys(INFO_LABELS, "")
        values.update(
            backend=state.spec.backend,
            target=state.spec.target,
            streams=str(state.spec.streams),
            network_mode=self._network_mode,
        )
        for key, value in state.info.items():
            if key in values and value:
                values[key] = value
        return values

    # --- process-wide ------------------------------------------------------------------

    def _process(self, snap: Snapshot) -> Iterator[Metric]:
        in_progress = _gauge("bandwidth_test_in_progress", "1 while a test is running.", labels=[])
        in_progress.add_metric([], 1.0 if snap.in_progress else 0.0)
        queue = _gauge(
            "bandwidth_queue_length", "Runs waiting for the single test worker.", labels=[]
        )
        queue.add_metric([], float(snap.queue_length))
        yield in_progress
        yield queue

        if snap.budget_limit_bytes is not None:
            limit = _gauge(
                "bandwidth_data_budget_bytes",
                "Configured data budget per billing period, all tests and directions.",
                labels=[],
            )
            limit.add_metric([], float(snap.budget_limit_bytes))
            yield limit
        used = _gauge(
            "bandwidth_budget_period_transferred_bytes",
            "Bytes charged to the data budget in the current billing period (persisted across "
            "restarts; resets on the configured day).",
            labels=[],
        )
        used.add_metric([], float(snap.budget_transferred_bytes))
        yield used

        quota = _gauge(
            "bandwidth_cpu_quota_cores",
            "Detected cgroup CPU quota in cores; 0 when unlimited.",
            labels=[],
        )
        quota.add_metric([], float(self._cpu_quota or 0.0))
        yield quota

        on_demand = CounterMetricFamily(
            "bandwidth_on_demand_requests", "On-demand run requests by result.", labels=["result"]
        )
        for result in ON_DEMAND_RESULTS:
            on_demand.add_metric([result], snap.on_demand.get(result, 0))
        yield on_demand

        build = InfoMetricFamily("bandwidth_build", "Build metadata.")
        build.add_metric(
            [],
            {
                "version": __version__,
                "revision": self._revision,
                "python_version": platform.python_version(),
                "iperf3_version": self._iperf3_version,
            },
        )
        yield build

        if self._peer_id:
            identity = InfoMetricFamily(
                "bandwidth_peer",
                "This instance's peer id: the `peer` value other instances use for it.",
            )
            identity.add_metric([], {"peer_id": self._peer_id})
            yield identity

    def _responder_metrics(self) -> Iterator[Metric]:
        stats = self._responder() if self._responder is not None else None
        if stats is None:
            return
        sessions = CounterMetricFamily(
            "bandwidth_responder_sessions", "Slots granted to peers for east/west tests."
        )
        sessions.add_metric([], stats.sessions_total)
        rejected = CounterMetricFamily(
            "bandwidth_responder_rejected_sessions",
            "Peer requests refused, by reason (auth, replay, busy, business_hours, ...).",
            labels=["reason"],
        )
        for reason in RESPONDER_REJECTIONS:
            rejected.add_metric([reason], stats.rejected.get(reason, 0))
        sent = CounterMetricFamily(
            "bandwidth_responder_sent_bytes",
            "Bytes this responder sent to peers (built-in engine).",
        )
        sent.add_metric([], stats.sent_bytes_total)
        received = CounterMetricFamily(
            "bandwidth_responder_received_bytes",
            "Bytes this responder received from peers (built-in engine).",
        )
        received.add_metric([], stats.received_bytes_total)
        busy = _gauge("bandwidth_responder_busy", "1 while a peer holds a slot here.", labels=[])
        busy.add_metric([], 1.0 if stats.active_slots else 0.0)
        yield from (sessions, rejected, sent, received, busy)
