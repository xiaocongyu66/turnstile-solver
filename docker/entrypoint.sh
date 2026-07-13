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
# HF Space often has a cgroup limit (~16G) while MemTotal shows the host (128G+).
# Optional override if cgroup is hidden: SOLVER_MEMORY_LIMIT_MB=16384
# export SOLVER_MEMORY_LIMIT_MB="${SOLVER_MEMORY_LIMIT_MB:-}"
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

# Built-in proxy pool + CF-Ares (env-driven)
export PYTHONPATH="${PYTHONPATH:-/app/worker:/app/vendor/CF-Ares}"
export CF_ARES_PATH="${CF_ARES_PATH:-/app/vendor/CF-Ares}"
# Align with grok_register: engine=auto, timeout=30, chrome only if CF_ARES_CHROME_PATH set
export CF_ARES="${CF_ARES:-1}"
export CF_ARES_BROWSER_ENGINE="${CF_ARES_BROWSER_ENGINE:-auto}"
export CF_ARES_HEADLESS="${CF_ARES_HEADLESS:-1}"
export CF_ARES_TIMEOUT="${CF_ARES_TIMEOUT:-30}"
export CF_ARES_IMPERSONATE="${CF_ARES_IMPERSONATE:-chrome120}"
export CF_ARES_SESSION_DIR="${CF_ARES_SESSION_DIR:-/tmp/solver-cf-ares-sessions}"
# Do NOT set CF_ARES_CHROME_PATH to Playwright chromium (ChromeDriver mismatch).
export CF_ARES_CHROME_PATH="${CF_ARES_CHROME_PATH:-}"

# ── Standalone sing-box proxy service (global HTTP/SOCKS) ──────────
# https://github.com/SagerNet/sing-box
export PROXY_SERVICE_ENABLED="${PROXY_SERVICE_ENABLED:-auto}"
export PROXY_SERVICE_HOST="${PROXY_SERVICE_HOST:-127.0.0.1}"
export PROXY_SERVICE_PORT="${PROXY_SERVICE_PORT:-7890}"
export PROXY_SERVICE_DNS="${PROXY_SERVICE_DNS:-1.1.1.1,8.8.8.8,8.8.4.4}"
export PROXY_SERVICE_DNS_STRATEGY="${PROXY_SERVICE_DNS_STRATEGY:-prefer_ipv4}"
export PROXY_SERVICE_MODE="${PROXY_SERVICE_MODE:-urltest}"
export PROXY_SERVICE_URLTEST_URL="${PROXY_SERVICE_URLTEST_URL:-https://www.gstatic.com/generate_204}"
export PROXY_SERVICE_URLTEST_INTERVAL="${PROXY_SERVICE_URLTEST_INTERVAL:-3m}"
export PROXY_SERVICE_WORK_DIR="${PROXY_SERVICE_WORK_DIR:-/tmp/solver-proxy-service}"
export PROXY_SERVICE_LOG_LEVEL="${PROXY_SERVICE_LOG_LEVEL:-warn}"
export PROXY_SERVICE_APPLY_GLOBAL="${PROXY_SERVICE_APPLY_GLOBAL:-1}"
export PROXY_RELAY_ENABLED="${PROXY_RELAY_ENABLED:-1}"
export PROXY_RELAY_AUTO_INSTALL="${PROXY_RELAY_AUTO_INSTALL:-1}"
export PROXY_RELAY_WORK_DIR="${PROXY_RELAY_WORK_DIR:-/tmp/solver-proxy-relay}"
export PROXY_POOL_STRATEGY="${PROXY_POOL_STRATEGY:-round_robin}"
export PROXY_TEST_ENABLED="${PROXY_TEST_ENABLED:-1}"
export PROXY_TEST_URLS="${PROXY_TEST_URLS:-https://challenges.cloudflare.com/turnstile/v0/api.js,https://accounts.x.ai/sign-up?redirect=grok-com}"
export PROXY_TEST_TIMEOUT="${PROXY_TEST_TIMEOUT:-12}"
export PROXY_TEST_WORKERS="${PROXY_TEST_WORKERS:-8}"
export PROXY_TEST_ACCEPT_STATUS="${PROXY_TEST_ACCEPT_STATUS:-200-399}"
export PROXY_TEST_STATE_FILE="${PROXY_TEST_STATE_FILE:-/tmp/solver-proxy-test.json}"
export PROXY_TEST_CACHE_SEC="${PROXY_TEST_CACHE_SEC:-300}"
mkdir -p "${PROXY_RELAY_WORK_DIR}" "${CF_ARES_SESSION_DIR}" "${PROXY_SERVICE_WORK_DIR}" 2>/dev/null || true

_proxy_hint="(empty — set PROXY_POOL for residential/ISP)"
if [ -n "${PROXY_POOL:-}${PROXY_POOL_LIST:-}${PROXIES:-}${PROXY_LIST:-}${SOLVER_PROXY:-}${CF_ARES_PROXY:-}" ]; then
  _proxy_hint="configured (PROXY_POOL / SOLVER_PROXY)"
fi
echo "🌐 proxy: ${_proxy_hint}  strategy=${PROXY_POOL_STRATEGY}  relay=${PROXY_RELAY_ENABLED}  test=${PROXY_TEST_ENABLED}"
echo "🛡️  CF_ARES=${CF_ARES} engine=${CF_ARES_BROWSER_ENGINE} path=${CF_ARES_PATH}"

# Start global sing-box proxy service when PROXY_POOL has share-links / any nodes
_start_proxy_svc=0
case "${PROXY_SERVICE_ENABLED}" in
  1|true|yes|on) _start_proxy_svc=1 ;;
  0|false|no|off) _start_proxy_svc=0 ;;
  *)
    # auto: start if PROXY_POOL set
    if [ -n "${PROXY_POOL:-}${PROXY_POOL_LIST:-}${PROXIES:-}${PROXY_LIST:-}${PROXY_POOL_FILE:-}" ]; then
      _start_proxy_svc=1
    fi
    ;;
esac

if [ "${_start_proxy_svc}" = "1" ]; then
  echo "🛰️  Starting sing-box proxy service (DNS=${PROXY_SERVICE_DNS} mode=${PROXY_SERVICE_MODE} port=${PROXY_SERVICE_PORT})..."
  # run in background; logs to work dir
  (
    cd /app
    PYTHONPATH=/app/worker:/app/vendor/CF-Ares \
      python /app/worker/proxy_service.py run \
      >"${PROXY_SERVICE_WORK_DIR}/service.stdout.log" 2>&1
  ) &
  echo $! >"${PROXY_SERVICE_WORK_DIR}/service.pid"
  # wait for ready (proxy_service prints PROXY_SERVICE_URL=)
  _wait=0
  while [ "${_wait}" -lt 45 ]; do
    if [ -f "${PROXY_SERVICE_WORK_DIR}/proxy.env" ]; then
      # shellcheck disable=SC1090
      . "${PROXY_SERVICE_WORK_DIR}/proxy.env"
      echo "   proxy-service ready HTTP_PROXY=${HTTP_PROXY:-?} pid=$(cat "${PROXY_SERVICE_WORK_DIR}/service.pid" 2>/dev/null || echo '?')"
      break
    fi
    # died?
    if [ -f "${PROXY_SERVICE_WORK_DIR}/service.pid" ]; then
      _spid=$(cat "${PROXY_SERVICE_WORK_DIR}/service.pid")
      if ! kill -0 "${_spid}" 2>/dev/null; then
        echo "   ⚠️  proxy-service exited early; tail:"
        tail -30 "${PROXY_SERVICE_WORK_DIR}/service.stdout.log" 2>/dev/null || true
        tail -30 "${PROXY_SERVICE_WORK_DIR}/sing-box.log" 2>/dev/null || true
        break
      fi
    fi
    _wait=$((_wait + 1))
    sleep 1
  done
  if [ -n "${HTTP_PROXY:-}" ] && [ "${PROXY_SERVICE_APPLY_GLOBAL}" != "0" ]; then
    export HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NO_PROXY no_proxy
    export SOLVER_USE_GLOBAL_PROXY=1
    export PROXY_SERVICE_APPLY_GLOBAL=1
    # Prefer global mixed port for per-request solvers too
    export SOLVER_PROXY="${SOLVER_PROXY:-$HTTP_PROXY}"
    echo "   🌍 global proxy applied → ${HTTP_PROXY}"
  else
    echo "   ⚠️  proxy-service not ready — workers will use per-node relay fallback"
  fi
else
  echo "🛰️  proxy-service skipped (PROXY_SERVICE_ENABLED=${PROXY_SERVICE_ENABLED})"
fi

# Thin adapter diagnose — uses vendor/CF-Ares as library (not rewritten)
python - <<'PY' 2>/dev/null || echo "   ⚠️  CF-Ares diagnose failed"
import sys
sys.path[:0] = ["/app/worker", "/app/vendor/CF-Ares"]
try:
    import cf_ares_helper as h
    d = h.diagnose()
    print(
        f"   CF-Ares available={d.get('available')} ver={d.get('version')} "
        f"vendor={d.get('vendor_path')} err={d.get('error') or 'none'}"
    )
except Exception as e:
    print(f"   ⚠️  CF-Ares import error: {e}")
PY

# When global proxy is up, skip per-node boot test (service already urltests)
if [ -n "${HTTP_PROXY:-}" ] && [ "${PROXY_SERVICE_APPLY_GLOBAL:-0}" = "1" ]; then
  echo "🔎 proxy test: using global sing-box service (${HTTP_PROXY}); skip per-node boot test"
  # optional quick curl through global proxy
  if command -v curl >/dev/null 2>&1; then
    if curl -sS -m 15 -o /dev/null -w "   global_proxy_probe=%{http_code} time=%{time_total}\n" \
      -x "${HTTP_PROXY}" "https://accounts.x.ai/sign-up?redirect=grok-com" 2>/dev/null; then
      :
    else
      echo "   ⚠️  global proxy probe failed (service may still work for browser)"
    fi
  fi
elif [ "${PROXY_TEST_ENABLED}" != "0" ] && [ -n "${PROXY_POOL:-}${PROXY_POOL_LIST:-}${PROXIES:-}${PROXY_LIST:-}${SOLVER_PROXY:-}${CF_ARES_PROXY:-}${PROXY_POOL_FILE:-}" ]; then
  echo "🔎 Testing proxies → xAI (timeout=${PROXY_TEST_TIMEOUT}s workers=${PROXY_TEST_WORKERS})..."
  python -c "
import json, sys
sys.path[:0] = ['/app/worker', '/app/vendor/CF-Ares']
import proxy_pool
st = proxy_pool.boot_test()
print(json.dumps({
  'active': st.get('active_count'),
  'total': st.get('count'),
  'ok': st.get('test_ok'),
  'fail': st.get('test_fail'),
  'preview': st.get('items_preview'),
}, ensure_ascii=False))
" || echo "⚠️  proxy boot test failed (workers will retry)"
else
  echo "🔎 proxy test skipped (no PROXY_POOL or PROXY_TEST_ENABLED=0)"
fi

echo "  auto: workers=${SOLVER_GATEWAY_WORKERS} max=${SOLVER_GATEWAY_WORKERS_MAX} soft=${SOLVER_WATCHDOG_SOFT_MB} hard=${SOLVER_WATCHDOG_HARD_MB}"

# Dump env at boot (redact API keys / passwords / tokens)
echo "========== env (secrets redacted) =========="
python - <<'PY'
import os
import re

# Key name patterns to fully redact (API keys / tokens / passwords)
SECRET_KEY = re.compile(
    r"(API[_-]?KEY|API[_-]?TOKEN|SECRET|PASSWORD|PASSWD|PASS\b|TOKEN|AUTH|"
    r"CREDENTIAL|PRIVATE[_-]?KEY|ACCESS[_-]?KEY|BEARER|JWT|HF_TOKEN|"
    r"GITHUB_TOKEN|PAT\b|WEBHOOK)",
    re.I,
)
# Always hide these exact names even if pattern misses
SECRET_NAMES = {
    "SOLVER_API_TOKEN",
    "TURNSTILE_SOLVER_TOKEN",
    "TURNSTILE_API_TOKEN",
    "CAPSOLVER_API_KEY",
    "CAPSOLVER_KEY",
    "TWOCAPTCHA_API_KEY",
    "CAPTCHA_API_KEY",
    "MOEMAIL_API_KEY",
    "SPACE_HF_TOKEN",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
}

# User:pass@host → ***:***@host  (proxy credentials)
CREDS_IN_URL = re.compile(r"(://)([^:/@\s]+):([^@/\s]+)(@)")

# Long hex/base64-looking secrets in values
LONG_SECRET = re.compile(r"\b([A-Za-z0-9_\-]{32,})\b")


def redact_value(key: str, val: str) -> str:
    if key in SECRET_NAMES or SECRET_KEY.search(key):
        if not val:
            return "(empty)"
        return f"(set, len={len(val)})"
    # redact embedded credentials in proxy lists / URLs
    out = CREDS_IN_URL.sub(r"\1***:***\4", val)
    # if key looks like proxy pool, also mask bare user:pass@
    if re.search(r"PROXY|PROXIES", key, re.I):
        out = re.sub(r"\b([^:/\s]+):([^@/\s]+)@", r"***:***@", out)
    return out


# Prefer solver-related keys first, then the rest (stable sort)
def sort_key(k: str):
    pref = (
        "SOLVER",
        "TURNSTILE",
        "CF_ARES",
        "PROXY",
        "PORT",
        "HOST",
        "PLAYWRIGHT",
        "SPACE",
        "PYTHON",
    )
    for i, p in enumerate(pref):
        if k.upper().startswith(p) or p in k.upper():
            return (0, i, k)
    return (1, 99, k)


items = sorted(os.environ.items(), key=lambda kv: sort_key(kv[0]))
# skip noisy shell/path junk unless relevant
SKIP_PREFIX = (
    "LS_",
    "OLDPWD",
    "PWD",
    "SHLVL",
    "TERM",
    "HOME",
    "USER",
    "SHELL",
    "LANG",
    "LC_",
    "GPG_",
    "SSH_",
    "XDG_",
    "LESS",
    "MAIL",
    "LOGNAME",
)
for k, v in items:
    if any(k.startswith(p) or k == p.rstrip("_") for p in SKIP_PREFIX):
        # keep PATH / PYTHONPATH
        if k not in ("PATH", "PYTHONPATH", "PYTHONHOME"):
            if k != "PATH" and k != "PYTHONPATH":
                continue
    if k in ("_",):
        continue
    print(f"  {k}={redact_value(k, v)}")
print("==========================================")
PY

echo "🚀 Starting solver-gateway..."
# Cap workers on HF when free RAM is moderate (avoid thrash + EPIPE)
# Gateway still re-plans from live /proc/meminfo.
exec /app/gateway/solver-gateway \
  --host "${SOLVER_GATEWAY_HOST}" \
  --port "${SOLVER_GATEWAY_PORT}" \
  --workers "${SOLVER_GATEWAY_WORKERS}" \
  --work-dir /app/logs
