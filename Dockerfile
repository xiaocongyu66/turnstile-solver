# syntax=docker/dockerfile:1
# Standalone Hybrid Turnstile Solver (Go + Rust + C++ + Python)
#
# HF Space 拉取方式：构建时 git clone 完整仓库，不依赖 Space 上传源码。
# Chromium：优先从 Gitee 发行版安装 chromium-browser.deb（国内快、可复现），
#           失败再回退 Playwright 官方 chromium。
#
#   REPO_URL 默认: https://github.com/xiaocongyu66/turnstile-solver.git
#   REPO_REF 默认: main
#   CHROMIUM_GITEE_TAG 默认: 22.04_amd64 | 22.04_arm64（按构建架构）
#
# Hardware: ≥ 2 vCPU / 16 GB RAM recommended

ARG PYTHON_VERSION=3.11-bookworm
ARG REPO_URL=https://github.com/xiaocongyu66/turnstile-solver.git
ARG REPO_REF=main
# Override e.g. 20.04_amd64 / 22.04_arm64 — see https://gitee.com/jizijhj/chromium_1/releases
ARG CHROMIUM_GITEE_BASE=https://gitee.com/jizijhj/chromium_1/releases/download
ARG CHROMIUM_GITEE_TAG=

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
ARG CHROMIUM_GITEE_BASE
ARG CHROMIUM_GITEE_TAG
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    HOST=0.0.0.0 \
    PROJECT_ROOT=/app \
    SOLVER_PYTHON=python \
    SOLVER_GATEWAY_WORKERS=auto \
    SOLVER_GATEWAY_WORKERS_MAX=auto \
    TURNSTILE_SOLVER_HEADLESS=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates curl wget \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxshmfence1 fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir 'playwright>=1.55'

# Install chromium-browser from Gitee release (jizijhj/chromium_1), then optional Playwright fallback.
# Packages: chromium-browser.deb + chromium-codecs-ffmpeg-extra.deb (+ l10n optional)
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    tag="${CHROMIUM_GITEE_TAG}"; \
    if [ -z "$tag" ]; then \
      case "$arch" in \
        amd64) tag="22.04_amd64" ;; \
        arm64) tag="22.04_arm64" ;; \
        *) tag="22.04_amd64" ;; \
      esac; \
    fi; \
    base="${CHROMIUM_GITEE_BASE}/${tag}"; \
    mkdir -p /tmp/chromium-debs; \
    cd /tmp/chromium-debs; \
    ok=0; \
    for f in chromium-codecs-ffmpeg-extra.deb chromium-browser.deb; do \
      echo "Downloading ${base}/${f}"; \
      if curl -fL --retry 3 --retry-delay 2 -o "$f" "${base}/${f}"; then \
        ok=1; \
      else \
        echo "WARN: download failed ${f}"; \
      fi; \
    done; \
    # optional l10n — ignore failure
    curl -fL --retry 2 -o chromium-browser-l10n.deb "${base}/chromium-browser-l10n.deb" || true; \
    if [ "$ok" = "1" ] && [ -f chromium-browser.deb ]; then \
      dpkg -i chromium-codecs-ffmpeg-extra.deb 2>/dev/null || true; \
      dpkg -i chromium-browser.deb || apt-get update && apt-get install -y -f --no-install-recommends || true; \
      dpkg -i chromium-browser-l10n.deb 2>/dev/null || true; \
      apt-get update && apt-get install -y -f --no-install-recommends || true; \
      rm -rf /var/lib/apt/lists/*; \
    else \
      echo "WARN: Gitee chromium debs unavailable — will try Playwright"; \
    fi; \
    rm -rf /tmp/chromium-debs; \
    # Resolve binary path for Playwright
    CHROME=""; \
    for c in \
      /usr/bin/chromium-browser \
      /usr/bin/chromium \
      /usr/lib/chromium-browser/chromium-browser \
      /usr/lib/chromium/chromium \
      /snap/bin/chromium; do \
      if [ -x "$c" ]; then CHROME="$c"; break; fi; \
    done; \
    if [ -n "$CHROME" ]; then \
      echo "Installed system chromium: $CHROME"; \
      "$CHROME" --version || true; \
      ln -sf "$CHROME" /usr/local/bin/chromium-browser; \
      echo "export SOLVER_CHROME_PATH=$CHROME" > /etc/profile.d/solver-chrome.sh; \
      echo "$CHROME" > /etc/solver-chrome-path; \
    else \
      echo "No system chromium — installing Playwright chromium to /ms-playwright"; \
      mkdir -p /ms-playwright; \
      PLAYWRIGHT_BROWSERS_PATH=/ms-playwright python -m playwright install --with-deps chromium || \
        PLAYWRIGHT_BROWSERS_PATH=/ms-playwright python -m playwright install chromium; \
      find /ms-playwright -type f \( -name chrome -o -name chromium -o -name headless_shell \) 2>/dev/null | head -20; \
    fi

WORKDIR /app
COPY --from=source /src/app/worker /app/worker
COPY --from=source /src/app/docker/entrypoint.sh /entrypoint.sh
COPY --from=gobuild /out/solver-gateway /app/gateway/solver-gateway
COPY --from=rustbuild /out/solver-watchdog /app/watchdog/solver-watchdog
COPY --from=cppbuild /out/solver-util /app/util/solver-util

RUN chmod +x /entrypoint.sh /app/gateway/solver-gateway \
      /app/watchdog/solver-watchdog /app/util/solver-util \
      /app/worker/browser_worker.py \
    && mkdir -p /app/logs /data/logs

EXPOSE 7860
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
