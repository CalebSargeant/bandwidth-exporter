# bandwidth-exporter

A Prometheus exporter that measures bandwidth on its own randomised schedule and serves the last
results from a cache: north/south (a site to the Internet), east/west (between instances you
run: nodes, clusters, sites) and, with zones, region to region. A scrape never starts a test, so an HA pair of Prometheus servers
does not double the tests and `scrape_timeout` is irrelevant. Business hours can be kept free of
tests, because a test saturates the link it measures.

Status: 0.3. It implements phases 0 and 1 of the [design](docs/research/bandwidth-exporter-design.md)
plus the Cloudflare backend: one queue and one worker, truncated-exponential schedules, business
hours, a persisted state file with catch-up, a data budget, the metric schema, the responder with
signed slots, east/west discovery and peer selection, zones, the Helm chart (Deployment,
StatefulSet or DaemonSet), alert rules and a Grafana dashboard.

## Backends

| Backend | Kind | Target | Use it for |
|---|---|---|---|
| `iperf3` | north/south | Your own iperf3 server (`iperf3 -s` on a cloud VM you run) | Production north/south: no third-party terms, known capacity, 10 Gbit/s-class with parallel streams |
| `cloudflare` | north/south | `speed.cloudflare.com` `__down` / `__up` | Quick start. No published terms for programmatic use, so opt-in and infrequent; under-reads above roughly 1 to 2.5 Gbit/s |
| `builtin` | east/west | A peer's responder | Raw TCP between instances, comfortable to 5 Gbit/s, viable to about 10 |
| `iperf3` | east/west | A peer's responder, which starts a single-use `iperf3 -s -1` per test | 10 Gbit/s-class and multi-stream east/west |

Nothing is measured until you name a test. NDT7 (M-Lab) and Ookla are not included, for the
licensing and data-publication reasons in the design.

## Quick start

Helm (the chart is published to `oci://ghcr.io/calebsargeant/charts/bandwidth-exporter`):

```yaml
# values.yaml: north/south to Cloudflare, outside office hours
config:
  business_hours: {timezone: Europe/Amsterdam, days: [mon, tue, wed, thu, fri], start: "07:00", end: "19:00"}
  north_south:
    - name: cloudflare
      backend: cloudflare
      schedule: {random: {mean: 6h, min: 2h, max: 15h}}
persistence: {enabled: true}
serviceMonitor: {enabled: true}
prometheusRule: {enabled: true}
dashboard: {enabled: true}
```

```bash
helm install bandwidth oci://ghcr.io/calebsargeant/charts/bandwidth-exporter -n monitoring -f values.yaml
```

East/west across a cluster: one pod per node, each testing three others and answering its peers.

```yaml
workload: {kind: DaemonSet}
responder: {enabled: true}
mesh: {enabled: true, randomPeers: 3}
peerKeys: {existingSecret: bandwidth-peers}   # key: at least 16 characters, e.g. openssl rand -hex 32
northSouthOn: worker-1                        # the one node that also runs north/south tests
```

Docker, on a host without Kubernetes: see [deploy/compose/compose.yaml](deploy/compose/compose.yaml).
Hosts that cannot run a daemon can use `bandwidth-exporter run --once --textfile <path>` from a
timer and node_exporter's textfile collector (north/south only).

## How a test runs

- Tests run in a worker process, never on the web event loop, one at a time per instance. An
  instance never tests while it answers a peer.
- Each direction (download, then upload; never both at once) runs N parallel streams for a
  warm-up and a measured phase. The warm-up ends when three 0.5 s chunks agree within 10%, at the
  latest after max(2 s, 10 x RTT) capped at 5 s. The measured phase runs 10 s, stops early after
  3 s once stable, and never exceeds 15 s including the warm-up.
- Throughput is measured at the receiver: bytes read for downloads; for uploads, the bytes the
  far end's TCP acknowledged (Linux `TCP_INFO`), not the bytes pushed into a socket buffer.
- Latency is part of a test, not a separate probe: the TCP handshake time before the load and
  every 400 ms during it, so the difference shows queueing (bufferbloat). For continuous latency
  between tests, use blackbox_exporter.
- The default schedule draws the gap to the next run from an exponential distribution (mean 4 h,
  truncated to 1 h to 12 h), because periodic sampling can lock onto periodic network behaviour.
  Cron with jitter is available. A failed run is retried after 10 min, doubling; a busy peer is
  asked again after half a minute to three minutes.
- Business hours: no test starts inside them. Random gaps count only the time outside them, so
  runs spread evenly over evenings, nights and weekends; a cron run inside them is skipped. A
  catch-up due inside them waits for their end, and the trigger answers 429 until then.
- After a restart a test runs at once only if its last attempt is older than one interval, so a
  rollout does not trigger a test. Results survive restarts in the state file.
- An optional data budget per billing period is charged with every byte. When the rest of the
  period cannot cover the next run, the run is skipped (`bandwidth_tests_skipped_total{reason="budget"}`).

Data use: one direction at 1 Gbit/s for 10 s moves 1.25 GB, so about six symmetric tests a day
on a 1 Gbit/s line is roughly 450 GB a month.

## East/west and the responder

Every instance can be a responder. A peer asks for a slot over a small HTTP control API (port
10057) with every request signed: HMAC-SHA256 over the method, path, peer id, a timestamp, a
nonce and the body, checked against a 60 s window and a nonce cache, so captured requests cannot
be replayed. The responder admits one slot at a time (configurable), not while it tests itself,
and not in its business hours; otherwise it answers 503 with Retry-After. A slot is a random
token and a data port (5201 to 5210) served by a single-use process that accepts only connections
presenting that token, for at most the slot's duration and byte budget, and is killed at the
deadline. With the iperf3 engine the responder starts `iperf3 -s -1` with its own idle and duration limits
instead. Nothing a peer sends becomes command-line text.

Peers come from configuration (`peers: [{id, address}]`) or from DNS (`discovery: {dns}`): a
headless Service returns one address per ready pod, and each address's responder says who it is,
because the `peer` label must be a stable id (the node name in a DaemonSet), never an address.
Each agent tests `random_peers` peers chosen by rendezvous hashing, which survives restarts and
keeps a mesh's pairs at N x k instead of N x (N-1).

Keys: one shared key for every peer, or a JSON object of peer id to key on the responder side.
They come from environment variables (`BWEXP_PEER_KEY`, `BWEXP_PEER_KEYS`), in Kubernetes from a
Secret. Each east/west test can sign with its own variable (`auth: {key_env: ...}`), so tests
to instances you share a key with elsewhere need not use the cluster's own key.

## Zones: region to region

`zones` maps peer ids to where they run (a site, region or datacenter). Every per-test series
then carries `zone`, the tester's zone, and `peer_zone`, the peer's, so a mesh that spans two
sites reads site to site without a second deployment, and the dashboard's Regions row groups
the pairs zone to zone. Peers missing from the map get empty labels.

```yaml
zones:
  worker-0: amsterdam
  worker-1: rotterdam
  worker-2: rotterdam
```

Together with north/south and east/west that gives three views of a network: to the Internet,
between instances, and between sites.

## Configuration

[config.example.yaml](config.example.yaml) shows every key. `bandwidth-exporter check-config`
validates a file, prints the plan and lists missing secrets.

## Metrics

Base units throughout. Result gauges hold the most recent successful run and survive later
failures, so a failed test never shows up as a zero; `bandwidth_last_test_success` says whether the
latest attempt worked. Labels are `test`, `kind`, `peer` (empty for north/south), `zone` and
`peer_zone` (empty without `zones`); context lives in `bandwidth_test_info`. The chart's ServiceMonitor adds `node`, the tester's node, so an
east/west result reads node to peer.

| Metric | Type | Meaning |
|---|---|---|
| `bandwidth_download_bytes_per_second`, `bandwidth_upload_bytes_per_second` | gauge | Receiver-side throughput, warm-up excluded |
| `bandwidth_download_bytes`, `bandwidth_upload_bytes` | gauge | Bytes counted in the measured phase |
| `bandwidth_download_duration_seconds`, `bandwidth_upload_duration_seconds` | gauge | Length of the measured phase |
| `bandwidth_idle_latency_seconds`, `bandwidth_jitter_seconds` | gauge | Median round trip before the load, and its jitter |
| `bandwidth_download_latency_seconds`, `bandwidth_upload_latency_seconds` | gauge | Median round trip under load |
| `bandwidth_download_retransmits`, `bandwidth_upload_retransmits` | gauge | Where the engine reports them |
| `bandwidth_plan_download_bytes_per_second`, `bandwidth_plan_upload_bytes_per_second` | gauge | The configured plan, for alerts |
| `bandwidth_last_test_success`, `bandwidth_last_test_cpu_saturated` | gauge | Latest attempt succeeded; it was CPU-bound |
| `bandwidth_last_success_timestamp_seconds`, `bandwidth_last_attempt_timestamp_seconds`, `bandwidth_next_run_timestamp_seconds` | gauge | Freshness and plan |
| `bandwidth_tests_total`, `bandwidth_test_failures_total{reason}`, `bandwidth_tests_skipped_total{reason}` | counter | Attempts, failures, deliberate skips |
| `bandwidth_sent_bytes_total`, `bandwidth_received_bytes_total` | counter | Every byte moved, warm-up and probes included |
| `bandwidth_test_info` | info | backend, method, target, server, streams, cca, network_mode, ip_family, tool_version |
| `bandwidth_public_ip_hash` | gauge | North/south: CRC-32 of the egress address; never the address itself |
| `bandwidth_test_in_progress`, `bandwidth_queue_length` | gauge | Scheduler state |
| `bandwidth_data_budget_bytes`, `bandwidth_budget_period_transferred_bytes` | gauge | Budget and use this period |
| `bandwidth_responder_sessions_total`, `bandwidth_responder_rejected_sessions_total{reason}` | counter | Slots granted; refusals (auth, replay, busy, business_hours, ...) |
| `bandwidth_responder_sent_bytes_total`, `bandwidth_responder_received_bytes_total`, `bandwidth_responder_busy` | | Responder load (built-in engine) |
| `bandwidth_peer_info{peer_id}`, `bandwidth_cpu_quota_cores`, `bandwidth_on_demand_requests_total{result}`, `bandwidth_build_info` | | Process |

Failure reasons: `timeout`, `connect`, `auth`, `peer_busy`, `protocol`, `tool_error`. Skip
reasons: `busy`, `budget`, `business_hours`, `peer_unavailable`, `rate_limited`, `cross_traffic`.

## HTTP endpoints

Metrics port (10056): `/metrics`, `/probe?target=<test or test@peer>` (never runs a test),
`/healthz`, `/readyz`, `/api/v1/tests` (JSON, with the business-hours state), and
`POST /api/v1/tests/{name}/run`, which is off by default and needs a bearer token: 202 queued,
409 already queued or running, 429 rate-limited, over budget or inside business hours.

Control port (10057, responder only, signed requests): `GET /v1/info`, `POST /v1/slots`,
`DELETE /v1/slots/{id}`.

## Kubernetes notes

- North/south: one replica, `Recreate`: two testers on one egress split the link. One tester
  per WAN egress; in a DaemonSet, `northSouthOn` names the node that runs them.
- A CPU request and no CPU limit: CFS throttling caps measured throughput in proportion.
- Non-root, read-only root filesystem, no capabilities, no service-account token, restricted Pod
  Security Standard. The NetworkPolicy admits Prometheus to the metrics port and the release's
  own pods (plus `responder.allowFrom`) to the control and data ports.
- Default ports 10056 and 10057 are not yet claimed on the Prometheus default-port wiki.

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
