#!/bin/sh
# Hugging Face Space / Docker entrypoint — standalone Turnstile Solver
set -eu
cd /app

export PORT="${PORT:-7860}"
export HOST="${HOST:-0.0.0.0}"
export SOLVER_GATEWAY_HOST="${SOLVER_GATEWAY_HOST:-$HOST}"
export SOLVER_GATEWAY_PORT="${SOLVER_GATEWAY_PORT:-$PORT}"
export PROJECT_ROOT="${PROJECT_ROOT:-/app}"
export SOLVER_PYTHON="${SOLVER_PYTHON:-python}"
export SOLVER_WORKER_SCRIPT="${SOLVER_WORKER_SCRIPT:-/app/worker/browser_worker.py}"
export SOLVER_UTIL_BIN="${SOLVER_UTIL_BIN:-/app/util/solver-util}"
export SOLVER_WATCHDOG_BIN="${SOLVER_WATCHDOG_BIN:-/app/watchdog/solver-watchdog}"
export TURNSTILE_SOLVER_HEADLESS="${TURNSTILE_SOLVER_HEADLESS:-1}"

# Defaults: almost everything auto (CPU + free RAM). Override with numbers if needed.
export SOLVER_GATEWAY_WORKERS="${SOLVER_GATEWAY_WORKERS:-auto}"
export SOLVER_GATEWAY_WORKERS_MAX="${SOLVER_GATEWAY_WORKERS_MAX:-auto}"
export SOLVER_WORKER_CONCURRENCY="${SOLVER_WORKER_CONCURRENCY:-auto}"
export SOLVER_GATEWAY_TIMEOUT="${SOLVER_GATEWAY_TIMEOUT:-auto}"
export SOLVER_GATEWAY_QUEUE="${SOLVER_GATEWAY_QUEUE:-auto}"
export SOLVER_WORKER_MAX_SOLVES="${SOLVER_WORKER_MAX_SOLVES:-auto}"
export SOLVER_WATCHDOG_SOFT_MB="${SOLVER_WATCHDOG_SOFT_MB:-auto}"
export SOLVER_WATCHDOG_HARD_MB="${SOLVER_WATCHDOG_HARD_MB:-auto}"
export SOLVER_WATCHDOG_INTERVAL_SEC="${SOLVER_WATCHDOG_INTERVAL_SEC:-auto}"
export SOLVER_WATCHDOG_ATTACH="${SOLVER_WATCHDOG_ATTACH:-1}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD:-1}"

# Prefer Gitee/system chromium-browser installed at image build
if [ -z "${SOLVER_CHROME_PATH:-}" ] && [ -f /etc/solver-chrome-path ]; then
  export SOLVER_CHROME_PATH="$(cat /etc/solver-chrome-path)"
fi
if [ -z "${SOLVER_CHROME_PATH:-}" ]; then
  for c in /usr/bin/chromium-browser /usr/bin/chromium /usr/local/bin/chromium-browser; do
    if [ -x "$c" ]; then export SOLVER_CHROME_PATH="$c"; break; fi
  done
fi
if [ -n "${SOLVER_CHROME_PATH:-}" ]; then
  echo "[*] SOLVER_CHROME_PATH=${SOLVER_CHROME_PATH}"
  "${SOLVER_CHROME_PATH}" --version 2>/dev/null || true
else
  echo "[*] SOLVER_CHROME_PATH unset — worker will use Playwright default / cache"
fi

mkdir -p /app/logs /data/logs 2>/dev/null || mkdir -p /app/logs

if [ -n "${SPACE_ID:-}" ]; then
  _slug=$(echo "${SPACE_ID}" | tr '/' '-')
  echo "✅ HF Space Turnstile Solver: https://${_slug}.hf.space/  (bind ${HOST}:${PORT})"
else
  echo "✅ Turnstile Solver bind ${HOST}:${PORT}"
fi

if [ -n "${SOLVER_API_TOKEN:-}${TURNSTILE_SOLVER_TOKEN:-}" ]; then
  echo "🔒 SOLVER_API_TOKEN set — /turnstile and /result require Bearer / X-API-Key"
else
  echo "⚠️  No SOLVER_API_TOKEN — public solve endpoints are open (set secret on HF)"
fi

echo "  auto: workers=${SOLVER_GATEWAY_WORKERS} max=${SOLVER_GATEWAY_WORKERS_MAX} soft=${SOLVER_WATCHDOG_SOFT_MB} hard=${SOLVER_WATCHDOG_HARD_MB}"
echo "🚀 Starting solver-gateway..."
exec /app/gateway/solver-gateway \
  --host "${SOLVER_GATEWAY_HOST}" \
  --port "${SOLVER_GATEWAY_PORT}" \
  --workers "${SOLVER_GATEWAY_WORKERS}" \
  --work-dir /app/logs
