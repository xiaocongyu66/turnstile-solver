#!/usr/bin/env python3
"""Multi-proxy pool + share-link / SOCKS-auth → local HTTP relay for Chromium.

Env (HF Secrets / docker):
  PROXY_POOL / PROXY_POOL_LIST / PROXIES / PROXY_LIST
      comma / newline / ; / | separated mixed formats:
        http://u:p@host:port
        socks5://u:p@host:port
        host:port
        vmess://... vless://... trojan://... ss://... hy2://...
  PROXY_POOL_FILE          optional file (one proxy per line)
  PROXY_POOL_STRATEGY      round_robin | random   (default round_robin)
  PROXY_RELAY_ENABLED      1 (default) convert share-links / socks5-auth
  PROXY_RELAY_AUTO_INSTALL 1 auto-download sing-box if missing
  PROXY_RELAY_WORK_DIR     /tmp/solver-proxy-relay
  PROXY_RELAY_START_PORT   19080
  PROXY_RELAY_MAX_NODES    48
  SOLVER_PROXY             single override (also CF_ARES_PROXY / HTTPS_PROXY)
"""
from __future__ import annotations

import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urlparse

from proxy_relay import BuiltinProxyRelay, BuiltinProxyRelayConfig

_LOCK = threading.Lock()
_CACHE: dict = {
    "sig": None,
    "items": (),
    "index": 0,
    "relay": None,
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


def _split_proxy_text(raw: str) -> list[str]:
    if not raw:
        return []
    # Prefer line splits; also allow , ; |
    chunks: list[str] = []
    for part in re.split(r"[\n,;|]+", raw):
        line = (part or "").strip()
        if line and not line.startswith("#"):
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
    """Chromium cannot use SOCKS5 with user/pass; share-links need sing-box."""
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
        # keep share-link / socks-auth as-is for relay import
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
    # de-dupe preserve order
    seen = set()
    out = []
    for x in lines:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _sig(raw_lines: list[str]) -> str:
    return f"{len(raw_lines)}:{hash(tuple(raw_lines))}"


def load_browser_proxies(*, force: bool = False) -> list[str]:
    """Return Chromium-safe proxy URLs (may start local relays)."""
    raw = _load_raw_lines()
    sig = _sig(raw)
    with _LOCK:
        if not force and _CACHE.get("sig") == sig and _CACHE.get("items") is not None:
            return list(_CACHE["items"])
        items: list[str] = []
        for line in raw:
            p = _to_browser_proxy(line)
            if p and p not in items:
                items.append(p)
        _CACHE["sig"] = sig
        _CACHE["items"] = tuple(items)
        _CACHE["index"] = 0
        log(f"loaded {len(items)} browser proxy(ies) from {len(raw)} line(s)")
        return list(items)


def pick_proxy(*, explicit: str = "") -> Optional[str]:
    """Pick one proxy. Request-level `explicit` wins; else pool strategy."""
    if (explicit or "").strip():
        p = _to_browser_proxy(explicit.strip())
        return p or explicit.strip()
    items = load_browser_proxies()
    if not items:
        # fall back single env
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
    # Playwright only supports http/socks5 server schemes
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
    items = load_browser_proxies()
    return {
        "count": len(items),
        "strategy": (os.environ.get("PROXY_POOL_STRATEGY") or "round_robin"),
        "relay_enabled": _env_bool("PROXY_RELAY_ENABLED", True),
        "items_preview": [p[:48] for p in items[:5]],
    }
