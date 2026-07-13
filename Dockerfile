# syntax=docker/dockerfile:1
# Standalone Hybrid Turnstile Solver (Go + Rust + C++ + Python)
#
# HF Space 拉取方式（推荐）：
#   Space 里只需本 Dockerfile（或连 GitHub 后自动同步）。
#   构建时 ALWAYS git clone 完整仓库，不依赖 Space 上传 gateway/watchdog/worker。
#
#   REPO_URL 默认: https://github.com/xiaocongyu66/turnstile-solver.git
#   REPO_REF 默认: main
#
# Hardware: ≥ 2 vCPU / 16 GB RAM recommended (Chromium)

ARG PYTHON_VERSION=3.11-bookworm
ARG REPO_URL=https://github.com/xiaocongyu66/turnstile-solver.git
ARG REPO_REF=main

# ========== Stage 0: ALWAYS clone full GitHub source ==========
FROM debian:bookworm-slim AS source
ARG REPO_URL
ARG REPO_REF
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN set -eu; \
    echo "Cloning ${REPO_URL} @ ${REPO_REF}"; \
    git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" /src/app \
      || git clone --depth 1 "${REPO_URL}" /src/app; \
    cd /src/app && git checkout "${REPO_REF}" 2>/dev/null || true; \
    test -f /src/app/watchdog/src/main.rs; \
    test -f /src/app/gateway/main.go; \
    test -f /src/app/worker/browser_worker.py; \
    test -f /src/app/util/solver_util.cpp; \
    test -f /src/app/docker/entrypoint.sh; \
    echo "Source OK: $(cd /src/app && git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# ---------- Go gateway ----------
FROM golang:1.22-bookworm AS gobuild
WORKDIR /src
COPY --from=source /src/app/gateway/go.mod /src/app/gateway/main.go ./
RUN mkdir -p /out && go build -trimpath -ldflags='-s -w' -o /out/solver-gateway .

# ---------- Rust watchdog ----------
FROM rust:1-bookworm AS rustbuild
WORKDIR /src
RUN mkdir -p /out
COPY --from=source /src/app/watchdog/Cargo.toml ./
COPY --from=source /src/app/watchdog/Cargo.lock* ./
COPY --from=source /src/app/watchdog/src ./src
RUN cargo build --release && cp target/release/solver-watchdog /out/solver-watchdog

# ---------- C++ util ----------
FROM debian:bookworm-slim AS cppbuild
RUN apt-get update && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/* && mkdir -p /out
COPY --from=source /src/app/util/solver_util.cpp /src/app/util/solver_util.hpp /src/
RUN g++ -O2 -std=c++17 -o /out/solver-util /src/solver_util.cpp

# ---------- Runtime ----------
FROM python:${PYTHON_VERSION}
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    HOST=0.0.0.0 \
    PROJECT_ROOT=/app \
    SOLVER_PYTHON=python \
    SOLVER_GATEWAY_WORKERS=auto \
    SOLVER_GATEWAY_WORKERS_MAX=4 \
    TURNSTILE_SOLVER_HEADLESS=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates curl \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxshmfence1 fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir 'playwright>=1.55' \
    && python -m playwright install chromium \
    && python -m playwright install-deps chromium || true

WORKDIR /app
COPY --from=source /src/app/worker /app/worker
COPY --from=source /src/app/docker/entrypoint.sh /entrypoint.sh
COPY --from=gobuild /out/solver-gateway /app/gateway/solver-gateway
COPY --from=rustbuild /out/solver-watchdog /app/watchdog/solver-watchdog
COPY --from=cppbuild /out/solver-util /app/util/solver-util

RUN chmod +x /entrypoint.sh /app/gateway/solver-gateway \
      /app/watchdog/solver-watchdog /app/util/solver-util \
    && mkdir -p /app/logs /data/logs

EXPOSE 7860
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
