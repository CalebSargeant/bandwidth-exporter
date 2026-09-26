#!/usr/bin/env bash
# Cross-check that `docker run --cpus=X` caps throughput the same way as the raw cgroup-v1 CFS quota used
# by run_bench.py (--constraints quota-X). Server and client each run in their own container with --cpus=X,
# host networking (loopback), and a busybox image that chroots into the host filesystem so the exact same
# binaries/venv are used. Output: results/docker_check.jsonl
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IPERF3_NEW="${IPERF3_NEW:-/tmp/iperf3-build/install/bin/iperf3}"
PY="$HERE/.venv/bin/python"
OUT="$HERE/results/docker_check.jsonl"
SECS="${SECS:-5}"
IMG=busybox:1.36
PARTS="${PARTS:-iperf3 py aiohttp}"
# iperf3 creates its stream buffer as a temp file, so give it a writable TMPDIR (/dev/shm) inside the read-only chroot.
run() { docker run --rm --cpus="$1" --network host -e TMPDIR=/dev/shm -v /:/host:ro -v /dev/shm:/host/dev/shm "$IMG" chroot /host "${@:2}"; }
for CPUS in 0.5 1.0; do
  # iperf3 3.21, single stream
  if [[ " $PARTS " == *" iperf3 "* ]]; then
  docker run -d --name bw_srv --cpus="$CPUS" --network host -e TMPDIR=/dev/shm -v /:/host:ro -v /dev/shm:/host/dev/shm "$IMG" chroot /host "$IPERF3_NEW" -s -p 5211 >/dev/null
  sleep 1
  for i in 1 2 3; do
    run "$CPUS" "$IPERF3_NEW" -c 127.0.0.1 -p 5211 -t "$SECS" -J \
      | "$PY" -c "import json,sys; j=json.load(sys.stdin); print(json.dumps({'scenario':'iperf3-3.21 TCP P1','docker_cpus':$CPUS,'run':$i,'gbps':j['end']['sum_received']['bits_per_second']/1e9}))" >> "$OUT"
    sleep 1
  done
  docker rm -f bw_srv >/dev/null
  fi
  # Python blocking TCP, single stream
  if [[ " $PARTS " == *" py "* ]]; then
  docker run -d --name bw_srv --cpus="$CPUS" --network host -v /:/host:ro -v /dev/shm:/host/dev/shm "$IMG" chroot /host "$PY" "$HERE/py_tcp.py" server --impl blocking --port 5311 >/dev/null
  sleep 2
  for i in 1 2 3; do
    run "$CPUS" "$PY" "$HERE/py_tcp.py" client --impl blocking --port 5311 --seconds "$SECS" \
      | "$PY" -c "import json,sys; j=json.loads(sys.stdin.read()); print(json.dumps({'scenario':'py blocking sendall/recv_into P1','docker_cpus':$CPUS,'run':$i,'gbps':j['gbps']}))" >> "$OUT"
    sleep 1
  done
  docker rm -f bw_srv >/dev/null
  fi
  # aiohttp server + aiohttp client, HTTP download
  if [[ " $PARTS " == *" aiohttp "* ]]; then
  docker run -d --name bw_srv --cpus="$CPUS" --network host -v /:/host:ro "$IMG" chroot /host "$PY" "$HERE/py_http_server.py" --impl aiohttp --port 8091 >/dev/null
  sleep 3
  for i in 1 2 3; do
    run "$CPUS" "$PY" "$HERE/py_http_client.py" --impl aiohttp --port 8091 --dir down --seconds "$SECS" \
      | "$PY" -c "import json,sys; j=json.loads(sys.stdin.read()); print(json.dumps({'scenario':'HTTP down srv=aiohttp cli=aiohttp','docker_cpus':$CPUS,'run':$i,'gbps':j['gbps']}))" >> "$OUT"
    sleep 1
  done
  docker rm -f bw_srv >/dev/null
  fi
done
cat "$OUT"
