#!/usr/bin/env bash
# Reproduce every loopback measurement cited in ../bandwidth-exporter-design.md (about 25 minutes on a 4-vCPU VM).
# Results land in results/; summarize.py rebuilds results/summary_tables.md from the raw JSONL files.
# Prereqs: apt-get install iperf3 (3.16 on Ubuntu 24.04); ./build_iperf3.sh 3.21 <dir>; Go >= 1.22; uv; dockerd running.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
export IPERF3_NEW="${IPERF3_NEW:-/tmp/iperf3-build/install/bin/iperf3}"
[ -x .venv/bin/python ] || { uv venv --python python3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt; }
[ -x bin/gobench ] || (cd gobench && CGO_ENABLED=0 go build -o ../bin/gobench .)
[ -f certs/cert.pem ] || (mkdir -p certs && cd certs && openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout key.pem -out cert.pem -days 30 -subj "/CN=127.0.0.1" -addext "subjectAltName=IP:127.0.0.1")
.venv/bin/python run_bench.py --group tcp --runs 3 --seconds 10
.venv/bin/python run_bench.py --group http --runs 3 --seconds 5
.venv/bin/python run_bench.py --group constrained --runs 3 --seconds 5 --constraints quota-0.5,quota-1.0,pin-shared
.venv/bin/python run_bench.py --group tls --runs 3 --seconds 5
SECS=5 ./docker_check.sh
.venv/bin/python run_bench.py --group extras --runs 3 --seconds 5
.venv/bin/python summarize.py > results/summary_tables.md
