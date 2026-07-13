#!/usr/bin/env python3
"""Multi-proxy pool + share-link / SOCKS-auth → local HTTP relay + xAI reachability test.

Env (HF Secrets / docker):
  PROXY_POOL / PROXY_POOL_LIST / PROXIES / PROXY_LIST
      comma / newline / ; / | separated mixed formats:
        http://u:p@host:port
        socks5://u:p@host:port
        host:port
        vmess://... vless://... trojan://... ss://... hy2://... hysteria2://...
        base64 subscription blob (decoded to multi-line share links)
  PROXY_POOL_FILE          optional file (one proxy per line)
  PROXY_POOL_STRATEGY      round_robin | random   (default round_robin)
  PROXY_RELAY_ENABLED      1 (default) convert share-links / socks5-auth via sing-box
  PROXY_RELAY_AUTO_INSTALL 1 auto-download sing-box if missing
  PROXY_RELAY_WORK_DIR     /tmp/solver-proxy-relay
  SOLVER_PROXY             single override (also CF_ARES_PROXY / HTTPS_PROXY)

  # Auto test (reach accounts.x.ai through each proxy)
  PROXY_TEST_ENABLED       1 (default) test before use
  PROXY_TEST_URLS          default https://accounts.x.ai/sign-up?redirect=grok-com
  PROXY_TEST_TIMEOUT       12 seconds
  PROXY_TEST_WORKERS       8 concurrent probes
  PROXY_TEST_ACCEPT_STATUS 200-399 (also tolerate 403/503 with CF body)
  PROXY_TEST_REQUIRE_OK    0 if 1 and none pass → empty pool (fail closed)
  PROXY_TEST_CACHE_SEC     300 retest interval
  PROXY_TEST_STATE_FILE    /tmp/solver-proxy-test.json  shared by workers
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlparse

from proxy_relay import BuiltinProxyRelay, BuiltinProxyRelayConfig

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {
    "sig": None,
    "raw_sig": None,
    "items": (),          # all converted browser proxies
    "active": (),         # tested-ok subset (or all if test off)
    "index": 0,
    "relay": None,
    "test": {},           # last test summary
    "tested_at": 0.0,
}
_SHARE_SCHEMES = (
    "vmess",
    "vless",
    "trojan",
    "ss",
    "hy2",
    "hysteria2",
    "tuic",
    "anytls",
    "socks",
    "socks5",
    "socks5h",
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def log(msg: str) -> None:
    import sys

    sys.stderr.write(f"[proxy-pool] {msg}\n")
    sys.stderr.flush()


def _env_proxy_text() -> str:
    return (
        os.environ.get("PROXY_POOL")
        or os.environ.get("PROXY_POOL_LIST")
        or os.environ.get("PROXIES")
        or os.environ.get("PROXY_LIST")
        or os.environ.get("SOLVER_PROXY")
        or os.environ.get("CF_ARES_PROXY")
        or ""
    ).strip()


def _maybe_decode_subscription(text: str) -> str:
    """If PROXY_POOL looks like base64 subscription, decode to multi-line links.

    HF Secrets often paste the whole base64 blob as one line. Expand to
    hysteria2:// / vmess:// / ... lines so relay can convert each node.
    """
    s = (text or "").strip()
    if not s:
        return s
    # already share-link / URL list
    low = s.lower()
    if any(
        x in low
        for x in (
            "hysteria2://",
            "hy2://",
            "vmess://",
            "vless://",
            "trojan://",
            "ss://",
            "socks5://",
            "http://",
            "https://",
        )
    ):
        return s
    # base64-ish single blob (no spaces, long)
    compact = re.sub(r"\s+", "", s)
    if len(compact) < 40:
        return s
    if not re.fullmatch(r"[A-Za-z0-9+/_-]+=*", compact):
        return s
    try:
        import base64

        # urlsafe or standard
        pad = "=" * (-len(compact) % 4)
        try:
            data = base64.urlsafe_b64decode(compact + pad)
        except Exception:
            data = base64.b64decode(compact + pad)
        decoded = data.decode("utf-8", errors="replace").strip()
        if "://" in decoded or decoded.count("\n") >= 1:
            log(f"decoded base64 subscription → {len(decoded.splitlines())} line(s)")
            return decoded
    except Exception as exc:
        log(f"base64 subscription decode skip: {exc}")
    return s


def _split_proxy_text(raw: str) -> list[str]:
    if not raw:
        return []
    raw = _maybe_decode_subscription(raw)
    chunks: list[str] = []
    for part in re.split(r"[\n,;|]+", raw):
        line = (part or "").strip()
        if not line or line.startswith("#"):
            continue
        # each piece may itself be a base64 blob
        if "://" not in line and len(line) > 40 and re.fullmatch(r"[A-Za-z0-9+/_-]+=*", line):
            for sub in _split_proxy_text(_maybe_decode_subscription(line)):
                if sub not in chunks:
                    chunks.append(sub)
            continue
        chunks.append(line)
    return chunks


def _share_link_scheme(line: str) -> str:
    try:
        return (urlparse(line.strip()).scheme or "").lower()
    except Exception:
        return ""


def _telegram_socks_to_url(text: str) -> Optional[str]:
    try:
        parsed = urlparse((text or "").strip())
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if "t.me" not in host and "telegram.me" not in host:
        return None
    if "socks" not in path:
        return None
    from urllib.parse import parse_qsl

    qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    server = (qs.get("server") or "").strip()
    port = (qs.get("port") or "").strip()
    user = (qs.get("user") or "").strip()
    password = (qs.get("pass") or qs.get("password") or "").strip()
    if not server or not port:
        return None
    if user or password:
        return f"socks5h://{quote(user, safe='')}:{quote(password, safe='')}@{server}:{port}"
    return f"socks5h://{server}:{port}"


def _needs_relay(proxy: str) -> bool:
    try:
        parsed = urlparse((proxy or "").strip())
    except Exception:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme in {"vmess", "vless", "trojan", "ss", "hy2", "hysteria2", "tuic", "anytls"}:
        return True
    if scheme in {"socks", "socks5", "socks5h"} and (parsed.username or parsed.password):
        return True
    return False


def _normalize_direct(proxy: str) -> Optional[str]:
    p = (proxy or "").strip()
    if not p or p.startswith("#"):
        return None
    if "t.me/socks" in p or "telegram.me/socks" in p:
        p = _telegram_socks_to_url(p) or p
    lowered = p.lower()
    if "://" not in lowered:
        if ":" not in p:
            return None
        p = f"http://{p}"
        lowered = p.lower()
    if not lowered.startswith(
        ("http://", "https://", "socks4://", "socks5://", "socks5h://")
    ):
        return None
    try:
        u = urlparse(p)
        if not u.hostname:
            return None
    except Exception:
        return None
    return p


def _get_relay() -> Optional[BuiltinProxyRelay]:
    if not _env_bool("PROXY_RELAY_ENABLED", True):
        return None
    if _CACHE.get("relay") is not None:
        return _CACHE["relay"]
    cfg = BuiltinProxyRelayConfig(
        enabled=True,
        host=(os.environ.get("PROXY_RELAY_HOST") or "127.0.0.1").strip(),
        proxy_scheme=(os.environ.get("PROXY_RELAY_PROXY_SCHEME") or "http").strip().lower()
        or "http",
        work_dir=(
            os.environ.get("PROXY_RELAY_WORK_DIR") or "/tmp/solver-proxy-relay"
        ).strip(),
        sing_box_bin=(os.environ.get("PROXY_RELAY_SING_BOX_BIN") or "").strip(),
        auto_install=_env_bool("PROXY_RELAY_AUTO_INSTALL", True),
        start_port=_env_int("PROXY_RELAY_START_PORT", 19080),
        max_nodes=_env_int("PROXY_RELAY_MAX_NODES", 48),
        start_timeout=_env_int("PROXY_RELAY_START_TIMEOUT", 12),
    )
    relay = BuiltinProxyRelay(cfg, logger=lambda m: log(str(m)))
    _CACHE["relay"] = relay
    log(f"relay ready ({relay.runtime_hint()})")
    return relay


def _to_browser_proxy(raw: str) -> Optional[str]:
    """Normalize one line to a Chromium-safe proxy URL (local HTTP if relayed)."""
    line = (raw or "").strip()
    if not line or line.startswith("#"):
        return None
    if "t.me/socks" in line or "telegram.me/socks" in line:
        line = _telegram_socks_to_url(line) or line

    scheme = _share_link_scheme(line)
    if scheme in _SHARE_SCHEMES or _needs_relay(line):
        if scheme in {"vmess", "vless", "trojan", "ss", "hy2", "hysteria2", "tuic", "anytls"}:
            pass
        elif scheme in {"socks", "socks5", "socks5h"} and not _needs_relay(line):
            return _normalize_direct(line)
        if not _env_bool("PROXY_RELAY_ENABLED", True):
            log(f"skip (relay disabled): {line[:48]}...")
            return None
        try:
            relay = _get_relay()
            if relay is None:
                return None
            node = relay.import_link(line)
            proxy = (node or {}).get("proxy") or ""
            if proxy:
                log(f"relayed → {proxy} ({line[:40]}...)")
                return proxy
        except Exception as exc:
            log(f"relay fail: {exc} ({line[:48]}...)")
            return None
        return None

    return _normalize_direct(line)


def _load_raw_lines() -> list[str]:
    lines: list[str] = []
    text = _env_proxy_text()
    if text:
        lines.extend(_split_proxy_text(text))
    file_path = (os.environ.get("PROXY_POOL_FILE") or "").strip()
    if file_path:
        try:
            for raw in Path(file_path).expanduser().read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                s = raw.strip()
                if s and not s.startswith("#"):
                    lines.append(s)
        except OSError as exc:
            log(f"PROXY_POOL_FILE read fail: {exc}")
    seen = set()
    out = []
    for x in lines:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _sig(raw_lines: list[str]) -> str:
    return f"{len(raw_lines)}:{hash(tuple(raw_lines))}"


# ── xAI reachability test ──────────────────────────────────────────


def _test_urls() -> list[str]:
    raw = (
        os.environ.get("PROXY_TEST_URLS")
        or os.environ.get("PROXY_AUTO_TEST_URLS")
        or ""
    ).strip()
    if raw:
        urls = [u.strip() for u in re.split(r"[\n,;]+", raw) if u.strip()]
        if urls:
            return urls
    # Must reach CF challenge CDN, not only x.ai (403 on x.ai is not enough for Turnstile)
    # Prefer CF CDN first (Turnstile needs it); then xAI
    return [
        "https://challenges.cloudflare.com/turnstile/v0/api.js",
        "https://accounts.x.ai/sign-up?redirect=grok-com",
    ]


def _parse_status_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for part in re.split(r"[,;\s]+", (text or "").strip()):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                ranges.append((int(a), int(b)))
            except ValueError:
                continue
        else:
            try:
                v = int(part)
                ranges.append((v, v))
            except ValueError:
                continue
    return ranges or [(200, 399)]


def _status_ok(code: int, ranges: list[tuple[int, int]]) -> bool:
    for a, b in ranges:
        if a <= code <= b:
            return True
    # Cloudflare interstitial often means proxy reached the edge
    if code in (403, 503):
        return True
    return False


def _state_path() -> Path:
    raw = (
        os.environ.get("PROXY_TEST_STATE_FILE")
        or "/tmp/solver-proxy-test.json"
    ).strip()
    return Path(raw).expanduser()


def _probe_one(proxy: str, url: str, timeout: float) -> dict[str, Any]:
    """Return {ok, status, ms, error, url, method}."""
    t0 = time.time()
    result: dict[str, Any] = {
        "proxy": proxy,
        "url": url,
        "ok": False,
        "status": 0,
        "ms": 0,
        "error": "",
        "method": "",
    }
    # Prefer curl (handles http + socks5 cleanly in container)
    try:
        cmd = [
            "curl",
            "-sS",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "-L",
            "--max-time",
            str(max(1, int(timeout))),
            "--connect-timeout",
            str(max(1, min(10, int(timeout)))),
            "-A",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "-x",
            proxy,
            url,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 3,
            check=False,
        )
        result["method"] = "curl"
        code_s = (proc.stdout or "").strip()
        try:
            code = int(code_s) if code_s.isdigit() else 0
        except ValueError:
            code = 0
        result["status"] = code
        result["ms"] = int((time.time() - t0) * 1000)
        if proc.returncode != 0 and code == 0:
            result["error"] = (proc.stderr or f"curl exit {proc.returncode}")[:160]
            return result
        result["ok"] = code > 0  # refined by caller with accept ranges
        return result
    except FileNotFoundError:
        pass
    except Exception as exc:
        result["error"] = f"curl: {exc}"[:160]

    # Fallback: requests / urllib (http proxies mainly)
    try:
        import requests

        result["method"] = "requests"
        r = requests.get(
            url,
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                )
            },
        )
        result["status"] = int(r.status_code)
        result["ms"] = int((time.time() - t0) * 1000)
        result["ok"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)[:160]
        result["ms"] = int((time.time() - t0) * 1000)
        return result


def test_proxy(proxy: str) -> dict[str, Any]:
    """Test one browser proxy: must reach CF Turnstile CDN, then xAI if listed."""
    timeout = float(_env_int("PROXY_TEST_TIMEOUT", 12))
    ranges = _parse_status_ranges(
        os.environ.get("PROXY_TEST_ACCEPT_STATUS")
        or os.environ.get("PROXY_AUTO_TEST_ACCEPT_STATUS")
        or "200-399"
    )
    urls = _test_urls()
    # Hard requirement for Turnstile: challenges.cloudflare.com must load
    require_cf = _env_bool("PROXY_TEST_REQUIRE_CF_CDN", True)
    cf_urls = [u for u in urls if "challenges.cloudflare.com" in u]
    other_urls = [u for u in urls if "challenges.cloudflare.com" not in u]
    if require_cf and not cf_urls:
        cf_urls = ["https://challenges.cloudflare.com/turnstile/v0/api.js"]

    last: dict[str, Any] = {"proxy": proxy, "ok": False, "error": "no url"}
    cf_ok = not require_cf
    for url in cf_urls:
        r = _probe_one(proxy, url, timeout)
        code = int(r.get("status") or 0)
        # api.js often returns 200; also accept 304
        r["ok"] = code in (200, 301, 302, 304) or (code > 0 and _status_ok(code, ranges) and code < 400)
        last = r
        last["cf_cdn"] = True
        if r["ok"]:
            cf_ok = True
            break
        last["error"] = last.get("error") or f"cf cdn status={code}"

    if require_cf and not cf_ok:
        last["ok"] = False
        last["error"] = (last.get("error") or "cf cdn unreachable")[:160]
        return last

    # Optional: also prove x.ai edge is reachable (403 CF interstitial is OK)
    for url in other_urls:
        r = _probe_one(proxy, url, timeout)
        r["ok"] = bool(r.get("status")) and _status_ok(int(r.get("status") or 0), ranges)
        if r["ok"]:
            r["cf_cdn"] = cf_ok
            return r
        last = r
        last["cf_cdn"] = cf_ok

    # CF CDN ok is enough even if x.ai probe flaky
    if cf_ok:
        return {
            "proxy": proxy,
            "ok": True,
            "status": last.get("status") or 200,
            "ms": last.get("ms") or 0,
            "error": "",
            "url": cf_urls[0] if cf_urls else "",
            "cf_cdn": True,
            "method": last.get("method") or "curl",
        }
    last["ok"] = False
    return last


def _load_state_file() -> Optional[dict[str, Any]]:
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _save_state_file(payload: dict[str, Any]) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        log(f"state write fail: {exc}")


def _file_lock_path() -> Path:
    return _state_path().with_suffix(".lock")


def _with_file_lock(fn):
    """Simple flock so multi-worker boot does one test only."""
    import fcntl

    lock_path = _file_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def test_proxies(
    proxies: list[str],
    *,
    force: bool = False,
) -> tuple[list[str], dict[str, Any]]:
    """Concurrent test; returns (ok_list, summary). Uses shared state file."""
    if not proxies:
        summary = {
            "total": 0,
            "ok": 0,
            "fail": 0,
            "active": [],
            "tested_at": time.time(),
            "urls": _test_urls(),
            "skipped": True,
            "reason": "empty pool",
        }
        return [], summary

    cache_sec = max(30, _env_int("PROXY_TEST_CACHE_SEC", 300))
    raw_sig = _sig(proxies)

    def _do() -> tuple[list[str], dict[str, Any]]:
        if not force:
            st = _load_state_file()
            if st and st.get("raw_sig") == raw_sig:
                age = time.time() - float(st.get("tested_at") or 0)
                if age < cache_sec and isinstance(st.get("active"), list):
                    active = [str(x) for x in st["active"] if x]
                    log(
                        f"reuse test cache age={int(age)}s ok={len(active)}/{st.get('total', '?')}"
                    )
                    return active, st

        workers = max(1, min(32, _env_int("PROXY_TEST_WORKERS", 8)))
        log(f"testing {len(proxies)} proxy(ies) → xAI  workers={workers} ...")
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(test_proxy, p): p for p in proxies}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception as exc:
                    r = {
                        "proxy": futs[fut],
                        "ok": False,
                        "error": str(exc)[:160],
                        "status": 0,
                        "ms": 0,
                    }
                results.append(r)
                mark = "OK" if r.get("ok") else "FAIL"
                log(
                    f"  [{mark}] status={r.get('status')} {r.get('ms')}ms "
                    f"{(r.get('proxy') or '')[:40]} "
                    f"{r.get('error') or ''}"
                )

        active = [str(r["proxy"]) for r in results if r.get("ok") and r.get("proxy")]
        # preserve original order
        order = {p: i for i, p in enumerate(proxies)}
        active.sort(key=lambda p: order.get(p, 9999))

        summary: dict[str, Any] = {
            "total": len(proxies),
            "ok": len(active),
            "fail": len(proxies) - len(active),
            "active": active,
            "tested_at": time.time(),
            "raw_sig": raw_sig,
            "urls": _test_urls(),
            "results": [
                {
                    "proxy": (r.get("proxy") or "")[:80],
                    "ok": bool(r.get("ok")),
                    "status": r.get("status"),
                    "ms": r.get("ms"),
                    "error": (r.get("error") or "")[:120],
                    "url": r.get("url"),
                }
                for r in results
            ],
        }
        _save_state_file(summary)
        log(f"test done: ok={len(active)} fail={summary['fail']} / {len(proxies)}")
        return active, summary

    try:
        return _with_file_lock(_do)
    except Exception as exc:
        # flock unavailable? run unlocked
        log(f"file lock fallback: {exc}")
        return _do()


def load_browser_proxies(*, force: bool = False, test: Optional[bool] = None) -> list[str]:
    """Return usable browser proxies (tested against xAI when enabled)."""
    raw = _load_raw_lines()
    sig = _sig(raw)
    do_test = _env_bool("PROXY_TEST_ENABLED", True) if test is None else bool(test)

    with _LOCK:
        cache_hit = (
            not force
            and _CACHE.get("raw_sig") == sig
            and _CACHE.get("active") is not None
            and _CACHE.get("items") is not None
        )
        if cache_hit:
            return list(_CACHE["active"] or _CACHE["items"])

        items: list[str] = []
        for line in raw:
            p = _to_browser_proxy(line)
            if p and p not in items:
                items.append(p)
        _CACHE["raw_sig"] = sig
        _CACHE["items"] = tuple(items)
        _CACHE["index"] = 0
        log(f"loaded {len(items)} browser proxy(ies) from {len(raw)} line(s)")

    if not items:
        with _LOCK:
            _CACHE["active"] = ()
            _CACHE["test"] = {"total": 0, "ok": 0, "fail": 0}
        return []

    if not do_test:
        with _LOCK:
            _CACHE["active"] = tuple(items)
            _CACHE["test"] = {
                "total": len(items),
                "ok": len(items),
                "fail": 0,
                "skipped": True,
            }
        return list(items)

    active, summary = test_proxies(items, force=force)
    require = _env_bool("PROXY_TEST_REQUIRE_OK", False)
    if not active and not require:
        # fail-open: keep all converted proxies but mark test failed
        log("WARN: no proxy passed xAI test — using untested pool (set PROXY_TEST_REQUIRE_OK=1 to fail closed)")
        use = items
    else:
        use = active

    with _LOCK:
        _CACHE["active"] = tuple(use)
        _CACHE["test"] = summary
        _CACHE["tested_at"] = float(summary.get("tested_at") or time.time())
        _CACHE["sig"] = sig
    return list(use)


def boot_test() -> dict[str, Any]:
    """Called from entrypoint: convert + test, print summary, return stats."""
    items = load_browser_proxies(force=True, test=_env_bool("PROXY_TEST_ENABLED", True))
    st = pool_stats()
    log(
        f"boot: active={st.get('active_count')} total={st.get('count')} "
        f"test_ok={st.get('test_ok')} test_fail={st.get('test_fail')}"
    )
    return st


def pick_proxy(*, explicit: str = "") -> Optional[str]:
    """Pick one proxy. Request-level `explicit` wins; else pool strategy."""
    if (explicit or "").strip():
        p = _to_browser_proxy(explicit.strip())
        # optional quick test for explicit?
        if p and _env_bool("PROXY_TEST_EXPLICIT", False):
            r = test_proxy(p)
            if not r.get("ok"):
                log(f"explicit proxy failed xAI test: {r}")
                return None
        return p or explicit.strip()
    items = load_browser_proxies()
    if not items:
        single = (
            os.environ.get("SOLVER_PROXY")
            or os.environ.get("CF_ARES_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or ""
        ).strip()
        if single:
            return _to_browser_proxy(single) or single
        return None
    strategy = (os.environ.get("PROXY_POOL_STRATEGY") or "round_robin").strip().lower()
    with _LOCK:
        if strategy == "random":
            return random.choice(items)
        idx = int(_CACHE.get("index", 0)) % len(items)
        _CACHE["index"] = (idx + 1) % len(items)
        return items[idx]


def playwright_proxy_dict(proxy_url: str) -> Optional[dict]:
    """Convert proxy URL to Playwright context proxy= dict."""
    if not proxy_url:
        return None
    try:
        u = urlparse(proxy_url)
    except Exception:
        return None
    if not u.scheme or not u.hostname:
        return None
    port = u.port
    if not port:
        port = 443 if u.scheme == "https" else 80
    server = f"{u.scheme}://{u.hostname}:{port}"
    if u.scheme in ("https",):
        server = f"http://{u.hostname}:{port}"
    if u.scheme == "socks5h":
        server = f"socks5://{u.hostname}:{port}"
    out: dict = {"server": server}
    if u.username:
        out["username"] = unquote(u.username)
    if u.password:
        out["password"] = unquote(u.password)
    return out


def pool_stats() -> dict:
    items_all = list(_CACHE.get("items") or ())
    if not items_all and not _CACHE.get("raw_sig"):
        # trigger load without re-forcing full test if cache empty
        load_browser_proxies()
        items_all = list(_CACHE.get("items") or ())
    active = list(_CACHE.get("active") or ())
    test = _CACHE.get("test") or {}
    return {
        "count": len(items_all),
        "active_count": len(active),
        "strategy": (os.environ.get("PROXY_POOL_STRATEGY") or "round_robin"),
        "relay_enabled": _env_bool("PROXY_RELAY_ENABLED", True),
        "test_enabled": _env_bool("PROXY_TEST_ENABLED", True),
        "test_ok": int(test.get("ok") or 0),
        "test_fail": int(test.get("fail") or 0),
        "test_urls": test.get("urls") or _test_urls(),
        "items_preview": [p[:48] for p in (active or items_all)[:5]],
    }


if __name__ == "__main__":
    # CLI: python proxy_pool.py  → boot test
    st = boot_test()
    print(json.dumps(st, ensure_ascii=False, indent=2))
