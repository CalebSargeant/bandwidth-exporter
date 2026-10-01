# syntax=docker/dockerfile:1.7
#
# bandwidth-exporter: the Python exporter plus an iperf3 built from ESnet's release tarball.
# Debian's iperf3 lags (trixie ships 3.18, before the 3.19.1 and 3.22 security fixes), so it is
# compiled here, statically linked against libiperf and without OpenSSL: iperf3's own RSA
# authentication stays off, as the design asks.
#
#   docker buildx bake app-local
#   docker run --rm -p 10056:10056 -v "$PWD/config.yaml:/etc/bandwidth-exporter/config.yaml:ro" \
#     bandwidth-exporter:dev

ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.21@sha256:ff07b86af50d4d9391d9daf4ff89ce427bc544f9aae87057e69a1cc0aa369946

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS iperf3
ARG IPERF3_VERSION=3.22
ARG IPERF3_SHA256=1c0d0fb02c52626111d6e132db80edfbf27bbaff8bd9245df2a371dcb0b35a92
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSLo iperf.tar.gz \
      "https://github.com/esnet/iperf/releases/download/${IPERF3_VERSION}/iperf-${IPERF3_VERSION}.tar.gz" \
 && echo "${IPERF3_SHA256}  iperf.tar.gz" | sha256sum -c - \
 && tar xzf iperf.tar.gz --strip-components=1 \
 && ./configure --disable-shared --without-openssl --prefix=/opt/iperf3 \
 && make -j"$(nproc)" \
 && make install \
 && /opt/iperf3/bin/iperf3 --version

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Dependencies first, from the lock file, so source changes do not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
ARG REVISION=""
LABEL org.opencontainers.image.title="bandwidth-exporter" \
      org.opencontainers.image.description="Scheduled bandwidth tests with cached results for Prometheus" \
      org.opencontainers.image.source="https://github.com/CalebSargeant/bandwidth-exporter" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp \
    BWEXP_REVISION=${REVISION}
COPY --from=iperf3 /opt/iperf3/bin/iperf3 /usr/local/bin/iperf3
COPY --from=build /opt/venv /opt/venv
RUN useradd --system --uid 10001 --gid 0 --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin bwexp \
 && mkdir -p /var/lib/bandwidth-exporter /etc/bandwidth-exporter \
 && chown 10001:0 /var/lib/bandwidth-exporter \
 && iperf3 --version \
 && bandwidth-exporter version
USER 10001:0
EXPOSE 10056
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:10056/healthz', timeout=4)"]
ENTRYPOINT ["bandwidth-exporter"]
CMD ["serve"]
