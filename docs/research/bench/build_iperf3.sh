#!/usr/bin/env bash
# Build the latest ESnet iperf3 from the official source tarball (apt on Ubuntu 24.04 ships 3.16).
# Usage: ./build_iperf3.sh [version] [build_dir]
set -euo pipefail
VER="${1:-3.21}"
BUILD_DIR="${2:-/tmp/iperf3-build}"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
curl -sSfLO "https://downloads.es.net/pub/iperf/iperf-${VER}.tar.gz"
curl -sSfLO "https://downloads.es.net/pub/iperf/iperf-${VER}.tar.gz.sha256" || true
if [ -f "iperf-${VER}.tar.gz.sha256" ]; then sha256sum -c "iperf-${VER}.tar.gz.sha256"; fi
tar xzf "iperf-${VER}.tar.gz"
cd "iperf-${VER}"
./configure --prefix="$BUILD_DIR/install" --disable-shared >/dev/null
make -j"$(nproc)" >/dev/null
make install >/dev/null
"$BUILD_DIR/install/bin/iperf3" --version
