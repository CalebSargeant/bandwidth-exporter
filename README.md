# bandwidth-exporter

A Prometheus exporter that measures north/south bandwidth (site to Internet) on its own randomised
schedule and serves the last results from a cache. A scrape never starts a test, so an HA pair of
Prometheus servers does not double the tests and `scrape_timeout` is irrelevant.

Status: 0.1, north/south only. It implements phase 0 of the
[design](docs/research/bandwidth-exporter-design.md) plus the Cloudflare backend: one queue and one
worker, truncated-exponential schedules, a persisted state file with catch-up, a data budget, the
metric schema, the Helm chart, alert rules and a Grafana dashboard. East/west tests and the
responder role are phase 1.

## Backends

| Backend | Target | Use it for |
|---|---|---|
| `iperf3` | Your own iperf3 server (`iperf3 -s` on a cloud VM you run) | Production: no third-party terms, known capacity, 10 Gbit/s-class with parallel streams |
| `cloudflare` | `speed.cloudflare.com` `__down` / `__up` | Quick start. Cloudflare publishes no terms for programmatic use, so it is opt-in and infrequent; it under-reads above roughly 1 to 2.5 Gbit/s |

Nothing is measured until you name a test. NDT7 (M-Lab) and Ookla are not included, for the
licensing and data-publication reasons in the design.

## Quick start

Helm (the chart is published to `oci://ghcr.io/calebsargeant/charts/bandwidth-exporter`):

```yaml
# values.yaml
config:
  north_south:
    - name: cloudflare
      backend: cloudflare
      schedule: {random: {mean: 6h, min: 2h, max: 15h}}
serviceMonitor: {enabled: true}
prometheusRule: {enabled: true}
dashboard: {enabled: true}
```

```bash
helm install bandwidth oci://ghcr.io/calebsargeant/charts/bandwidth-exporter -n monitoring -f values.yaml
```

Docker, on a host without Kubernetes: see [deploy/compose/compose.yaml](deploy/compose/compose.yaml).
Hosts that cannot run a daemon can use `bandwidth-exporter run --once --textfile <path>` from a
timer and node_exporter's textfile collector.

## How a test runs

- Tests run in a worker process, never on the web event loop, one at a time.
- Each direction (download, then upload; never both at once) runs N parallel streams for a
  warm-up and a measured phase. The warm-up ends when three 0.5 s chunks agree within 10%, at the
  latest after max(2 s, 10 x RTT) capped at 5 s. The measured phase runs 10 s, stops early after
  3 s once stable, and never exceeds 15 s including the warm-up.
- Throughput is measured at the receiver: bytes read for downloads; for uploads, the bytes the
  far end's TCP acknowledged (Linux `TCP_INFO`), not the bytes pushed into a socket buffer.
- Latency is the TCP handshake time to the same address, idle before the load and every 400 ms
  during it, so the difference shows bufferbloat.
- The default schedule draws the gap to the next run from an exponential distribution (mean 4 h,
  truncated to 1 h to 12 h), because periodic sampling can lock onto periodic network behaviour.
  Cron with jitter is available. A failed run is retried after 10 min, doubling.
- After a restart a test runs at once only if its last attempt is older than one interval, so a
  rollout does not trigger a test. Results survive restarts in the state file.
- An optional data budget per billing period is charged with every byte. When the rest of the
  period cannot cover the next run, the exporter falls back to latency-only probing or skips.

Data use: one direction at 1 Gbit/s for 10 s moves 1.25 GB, so about six symmetric tests a day
on a 1 Gbit/s line is roughly 450 GB a month.

## Configuration

[config.example.yaml](config.example.yaml) shows every key. `bandwidth-exporter check-config`
validates a file and prints the plan. Secrets come only from environment variables.

## Metrics

Base units throughout. Result gauges hold the most recent successful run and survive later
failures, so a failed test never shows up as a zero; `bandwidth_last_test_success` says whether the
latest attempt worked. Labels are `test`, `kind` and `peer` (empty for north/south); context lives
in `bandwidth_test_info`.

| Metric | Type | Meaning |
|---|---|---|
| `bandwidth_download_bytes_per_second`, `bandwidth_upload_bytes_per_second` | gauge | Receiver-side throughput, warm-up excluded |
| `bandwidth_download_bytes`, `bandwidth_upload_bytes` | gauge | Bytes counted in the measured phase |
| `bandwidth_download_duration_seconds`, `bandwidth_upload_duration_seconds` | gauge | Length of the measured phase |
| `bandwidth_idle_latency_seconds`, `bandwidth_jitter_seconds` | gauge | Median idle round trip, and its jitter |
| `bandwidth_download_latency_seconds`, `bandwidth_upload_latency_seconds` | gauge | Median round trip under load |
| `bandwidth_download_retransmits`, `bandwidth_upload_retransmits` | gauge | Where the engine reports them |
| `bandwidth_plan_download_bytes_per_second`, `bandwidth_plan_upload_bytes_per_second` | gauge | The configured plan, for alerts |
| `bandwidth_last_test_success`, `bandwidth_last_test_cpu_saturated` | gauge | Latest attempt succeeded; it was CPU-bound |
| `bandwidth_last_success_timestamp_seconds`, `bandwidth_last_attempt_timestamp_seconds`, `bandwidth_next_run_timestamp_seconds` | gauge | Freshness and plan |
| `bandwidth_tests_total`, `bandwidth_test_failures_total{reason}`, `bandwidth_tests_skipped_total{reason}` | counter | Attempts, failures, deliberate skips |
| `bandwidth_sent_bytes_total`, `bandwidth_received_bytes_total` | counter | Every byte moved, warm-up and probes included |
| `bandwidth_test_info` | info | backend, method, target, server, streams, cca, network_mode, ip_family, tool_version |
| `bandwidth_public_ip_hash` | gauge | CRC-32 of the egress address; never the address itself |
| `bandwidth_test_in_progress`, `bandwidth_queue_length` | gauge | Scheduler state |
| `bandwidth_data_budget_bytes`, `bandwidth_budget_period_transferred_bytes` | gauge | Budget and use this period |
| `bandwidth_cpu_quota_cores`, `bandwidth_on_demand_requests_total{result}`, `bandwidth_build_info` | | Process |

Failure reasons: `timeout`, `connect`, `auth`, `peer_busy`, `protocol`, `tool_error`. Skip
reasons: `busy`, `budget`, `peer_unavailable`, `rate_limited`, `cross_traffic`.

## HTTP endpoints

`/metrics`, `/probe?target=<test>` (one test, never runs it), `/healthz`, `/readyz`,
`/api/v1/tests` (JSON), and `POST /api/v1/tests/{name}/run`, which is off by default and needs
a bearer token: 202 queued, 409 already queued or running, 429 rate-limited or over budget.

## Kubernetes notes

- One replica, `Recreate`: two testers on one egress split the link. One tester per WAN egress.
- A CPU request and no CPU limit: CFS throttling caps measured throughput in proportion.
- Non-root, read-only root filesystem, no capabilities, no service-account token.
- Default port 10056 is not yet claimed on the Prometheus default-port wiki.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
helm lint charts/bandwidth-exporter
```

Releases: bump the version in `pyproject.toml`, `src/bandwidth_exporter/__init__.py` and
`charts/bandwidth-exporter/Chart.yaml`, then push a `vX.Y.Z` tag. The release workflow publishes
the signed multi-arch image and the chart, and lists both digests on the GitHub release.

Licence: Apache-2.0. The image bundles iperf3 (BSD-3-Clause), built from ESnet's 3.22 release.
