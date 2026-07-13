# syntax=docker/dockerfile:1
# Standalone Hybrid Turnstile Solver (Go + Rust + C++ + Python)
#
# HF Space 拉取方式：构建时 git clone 完整仓库（含 vendor/chromium 离线 deb）。
# Chromium：优先安装仓库内置 Gitee chromium-browser.deb，不依赖构建时访问 Gitee。
#
#   REPO_URL 默认: https://github.com/xiaocongyu66/turnstile-solver.git
#   REPO_REF 默认: main
#   CHROMIUM_TAG 默认: 22.04_amd64 | 22.04_arm64
#
# Hardware: ≥ 2 vCPU / 16 GB RAM recommended

ARG PYTHON_VERSION=3.12-bookworm
ARG REPO_URL=https://github.com/xiaocongyu66/turnstile-solver.git
ARG REPO_REF=main
ARG CHROMIUM_TAG=

# ========== Stage 0: ALWAYS clone full GitHub source (includes vendor/chromium) ==========
FROM debian:bookworm-slim AS source
ARG REPO_URL
ARG REPO_REF
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Need full history depth only for files; large vendor/chromium is in the tree
RUN set -eu; \
    echo "Cloning ${REPO_URL} @ ${REPO_REF} (full tree with vendor/chromium)"; \
    git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" /src/app \
      || git clone --depth 1 "${REPO_URL}" /src/app; \
    cd /src/app && git checkout "${REPO_REF}" 2>/dev/null || true; \
    test -f /src/app/watchdog/src/main.rs; \
    test -f /src/app/gateway/main.go; \
    test -f /src/app/worker/browser_worker.py; \
    test -f /src/app/worker/proxy_pool.py; \
    test -f /src/app/worker/proxy_relay.py; \
    test -f /src/app/worker/cf_ares_helper.py; \
    test -d /src/app/vendor/CF-Ares/cf_ares; \
    test -f /src/app/util/solver_util.cpp; \
    test -f /src/app/docker/entrypoint.sh; \
    test -d /src/app/vendor/chromium; \
    ls -la /src/app/vendor/chromium; \
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
ARG CHROMIUM_TAG
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
    tini ca-certificates curl \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxshmfence1 fonts-liberation \
    unzip \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        'playwright>=1.55' \
        'curl_cffi>=0.5.7' \
        'requests>=2.28.0' \
        'seleniumbase>=4.0.0' \
        'undetected-chromedriver>=3.5.0'

# Offline install: bundled debs from vendor/chromium (Gitee jizijhj/chromium_1)
COPY --from=source /src/app/vendor/chromium /opt/vendor/chromium
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    tag="${CHROMIUM_TAG}"; \
    if [ -z "$tag" ]; then \
      case "$arch" in \
        amd64) tag="22.04_amd64" ;; \
        arm64) tag="22.04_arm64" ;; \
        *) tag="22.04_amd64" ;; \
      esac; \
    fi; \
    dir="/opt/vendor/chromium/${tag}"; \
    echo "Installing offline chromium from ${dir}"; \
    if [ ! -d "$dir" ]; then \
      echo "WARN: missing ${dir}, trying any matching *_${arch}"; \
      dir="$(ls -d /opt/vendor/chromium/*_${arch} 2>/dev/null | head -1 || true)"; \
    fi; \
    if [ -n "$dir" ] && [ -f "${dir}/chromium-browser.deb" ]; then \
      cd "$dir"; \
      dpkg -i chromium-codecs-ffmpeg-extra.deb 2>/dev/null || true; \
      dpkg -i chromium-browser.deb || true; \
      dpkg -i chromium-browser-l10n.deb 2>/dev/null || true; \
      apt-get update && apt-get install -y -f --no-install-recommends || true; \
      rm -rf /var/lib/apt/lists/*; \
    else \
      echo "WARN: no bundled chromium deb for arch=${arch}"; \
    fi; \
    CHROME=""; \
    for c in \
      /usr/bin/chromium-browser \
      /usr/bin/chromium \
      /usr/lib/chromium-browser/chromium-browser \
      /usr/lib/chromium/chromium \
      /usr/local/bin/chromium-browser; do \
      if [ -x "$c" ]; then CHROME="$c"; break; fi; \
    done; \
    if [ -n "$CHROME" ]; then \
      echo "Installed system chromium (fallback): $CHROME"; \
      "$CHROME" --version || true; \
      ln -sf "$CHROME" /usr/local/bin/chromium-browser; \
      echo "$CHROME" > /etc/solver-chrome-path; \
    else \
      echo "WARN: system chromium missing"; \
    fi; \
    # Always install modern Playwright Chromium — Turnstile rejects ancient 108-era
    # chromium-browser debs. Worker prefers /ms-playwright over system chrome.
    echo "Installing Playwright bundled chromium → /ms-playwright"; \
    mkdir -p /ms-playwright; \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright python -m playwright install --with-deps chromium || \
      PLAYWRIGHT_BROWSERS_PATH=/ms-playwright python -m playwright install chromium; \
    find /ms-playwright -type f \( -name chrome -o -name chromium -o -name headless_shell \) 2>/dev/null | head -20

WORKDIR /app
COPY --from=source /src/app/worker /app/worker
COPY --from=source /src/app/vendor/CF-Ares /app/vendor/CF-Ares
# also ship under worker/ so helper finds it even if CF_ARES_PATH is wrong
COPY --from=source /src/app/vendor/CF-Ares /app/worker/vendor/CF-Ares
COPY --from=source /src/app/docker/entrypoint.sh /entrypoint.sh
COPY --from=gobuild /out/solver-gateway /app/gateway/solver-gateway
COPY --from=rustbuild /out/solver-watchdog /app/watchdog/solver-watchdog
COPY --from=cppbuild /out/solver-util /app/util/solver-util

ENV PYTHONPATH=/app/worker:/app/vendor/CF-Ares \
    CF_ARES_PATH=/app/vendor/CF-Ares \
    PROXY_RELAY_WORK_DIR=/tmp/solver-proxy-relay \
    PROXY_RELAY_ENABLED=1 \
    PROXY_RELAY_AUTO_INSTALL=1 \
    CF_ARES=auto

RUN chmod +x /entrypoint.sh /app/gateway/solver-gateway \
      /app/watchdog/solver-watchdog /app/util/solver-util \
      /app/worker/browser_worker.py \
    && mkdir -p /app/logs /data/logs /tmp/solver-proxy-relay \
    && test -d /app/vendor/CF-Ares/cf_ares \
    && test -d /app/worker/vendor/CF-Ares/cf_ares \
    && ls -la /app/vendor/CF-Ares /app/worker/vendor/CF-Ares \
    && python -c "import sys; sys.path[:0]=['/app/vendor/CF-Ares','/app/worker']; import cf_ares, proxy_pool, cf_ares_helper as h; print('cf-ares+proxy_pool OK', getattr(cf_ares,'__version__', '?'), 'helper', h.available(), h._vendor_path())"

EXPOSE 7860
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
