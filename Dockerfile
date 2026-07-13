# syntax=docker/dockerfile:1
# Standalone Hybrid Turnstile Solver (Go gateway + Rust watchdog + C++ util + Python browser)
# Hugging Face Space: SDK=Docker, hardware ≥ 2 vCPU / 16 GB recommended

ARG PYTHON_VERSION=3.11-bookworm

# ---------- Go gateway ----------
FROM golang:1.22-bookworm AS gobuild
WORKDIR /src
COPY gateway/go.mod gateway/main.go ./
RUN go build -trimpath -ldflags='-s -w' -o /out/solver-gateway .

# ---------- Rust watchdog ----------
FROM rust:1-bookworm AS rustbuild
WORKDIR /src
RUN mkdir -p /out
COPY watchdog/Cargo.toml ./
COPY watchdog/src ./src
# Generate lock on build if absent
RUN cargo build --release && cp target/release/solver-watchdog /out/solver-watchdog

# ---------- C++ util ----------
FROM debian:bookworm-slim AS cppbuild
RUN apt-get update && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/* && mkdir -p /out
COPY util/solver_util.cpp util/solver_util.hpp /src/
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
COPY worker /app/worker
COPY docker/entrypoint.sh /entrypoint.sh
COPY --from=gobuild /out/solver-gateway /app/gateway/solver-gateway
COPY --from=rustbuild /out/solver-watchdog /app/watchdog/solver-watchdog
COPY --from=cppbuild /out/solver-util /app/util/solver-util

RUN chmod +x /entrypoint.sh /app/gateway/solver-gateway \
      /app/watchdog/solver-watchdog /app/util/solver-util \
    && mkdir -p /app/logs /data/logs

EXPOSE 7860
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
