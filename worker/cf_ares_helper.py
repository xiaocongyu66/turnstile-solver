#!/usr/bin/env python3
"""Built-in CF-Ares helper for Turnstile solver.

Optional path:
  1) Warm Cloudflare clearance cookies via AresClient (browser + curl_cffi)
  2) Hand cookies / UA to Playwright for Turnstile inject

Env:
  CF_ARES=1|0|auto          default auto (enable if importable)
  CF_ARES_BROWSER_ENGINE    auto|undetected|seleniumbase
  CF_ARES_HEADLESS          1
  CF_ARES_TIMEOUT           45
  CF_ARES_CHROME_PATH       optional chrome binary
  CF_ARES_PATH              override vendor path
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

_LOCK = threading.Lock()
_IMPORT_ERROR: Optional[BaseException] = None
_CLIENTS: dict[str, Any] = {}


def log(msg: str) -> None:
    sys.stderr.write(f"[cf-ares] {msg}\n")
    sys.stderr.flush()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _vendor_path() -> Optional[Path]:
    raw = (os.environ.get("CF_ARES_PATH") or "").strip()
    candidates = []
    if raw:
        candidates.append(Path(raw).expanduser())
    # image layout: /app/vendor/CF-Ares
    here = Path(__file__).resolve()
    candidates.extend(
        [
            here.parents[1] / "vendor" / "CF-Ares",
            Path("/app/vendor/CF-Ares"),
            Path("/opt/vendor/CF-Ares"),
        ]
    )
    for c in candidates:
        if (c / "cf_ares").is_dir():
            return c
    return None


def add_import_path() -> bool:
    root = _vendor_path()
    if root is None:
        return False
    text = str(root)
    if text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)
    return True


def available() -> bool:
    global _IMPORT_ERROR
    mode = (os.environ.get("CF_ARES") or "auto").strip().lower()
    if mode in ("0", "false", "no", "off", "disabled"):
        return False
    if _IMPORT_ERROR is not None and mode != "1":
        return False
    if not add_import_path():
        if mode in ("1", "true", "yes", "on", "always"):
            log("CF_ARES forced but vendor path missing")
        return False
    try:
        from cf_ares import AresClient  # noqa: F401

        return True
    except Exception as exc:
        _IMPORT_ERROR = exc
        if mode in ("1", "true", "yes", "on", "always"):
            log(f"import failed: {exc}")
        return False


def _chrome_path() -> Optional[str]:
    for key in ("CF_ARES_CHROME_PATH", "SOLVER_CHROME_PATH", "CHROME_PATH"):
        p = (os.environ.get(key) or "").strip()
        if p and os.path.isfile(p):
            return p
    # prefer playwright modern chrome for CF-Ares too
    for pattern in (
        "/ms-playwright/chromium-*/chrome-linux64/chrome",
        "/ms-playwright/chromium-*/chrome-linux/chrome",
    ):
        import glob

        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    for c in ("/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"):
        if os.path.isfile(c):
            return c
    return None


def get_client(proxy: Optional[str] = None):
    """Cached AresClient per proxy key."""
    global _IMPORT_ERROR
    if not available():
        raise RuntimeError(f"cf-ares unavailable: {_IMPORT_ERROR}")
    key = (proxy or "").strip() or "__direct__"
    with _LOCK:
        if key in _CLIENTS:
            return _CLIENTS[key]
        from cf_ares import AresClient

        timeout = 45
        try:
            timeout = int((os.environ.get("CF_ARES_TIMEOUT") or "45").strip() or "45")
        except ValueError:
            timeout = 45
        kwargs: dict[str, Any] = {
            "browser_engine": (os.environ.get("CF_ARES_BROWSER_ENGINE") or "auto").strip()
            or "auto",
            "headless": _env_bool("CF_ARES_HEADLESS", True),
            "timeout": timeout,
            "debug": _env_bool("CF_ARES_DEBUG", False),
        }
        if proxy:
            kwargs["proxy"] = proxy
        chrome = _chrome_path()
        if chrome:
            kwargs["chrome_path"] = chrome
        client = AresClient(**kwargs)
        _CLIENTS[key] = client
        log(f"client ready proxy={proxy or 'direct'} chrome={chrome or 'default'}")
        return client


def warm_session(url: str, proxy: Optional[str] = None) -> dict[str, Any]:
    """
    Run CF challenge / page load and return cookies + headers for Playwright.

    Returns:
      {ok, cookies: [{name,value,domain,path}], user_agent, error?}
    """
    page_url = (url or "").strip() or "https://accounts.x.ai/sign-up"
    if "://" not in page_url:
        page_url = "https://accounts.x.ai/sign-up"
    out: dict[str, Any] = {"ok": False, "cookies": [], "user_agent": "", "url": page_url}
    try:
        client = get_client(proxy)
        # Prefer explicit solve; fall back to plain get
        try:
            resp = client.solve_challenge(page_url, max_retries=2)
            out["status"] = getattr(resp, "status_code", None)
        except Exception as exc:
            log(f"solve_challenge fail, try get: {exc}")
            resp = client.get(page_url)
            out["status"] = getattr(resp, "status_code", None)
            out["challenge_error"] = str(exc)[:200]

        # session cookies from manager if present
        cookies: dict[str, str] = {}
        try:
            info = client.get_session_info(page_url)
            if isinstance(info, dict):
                raw = info.get("cookies") or {}
                if isinstance(raw, dict):
                    cookies.update({str(k): str(v) for k, v in raw.items()})
                headers = info.get("headers") or {}
                if isinstance(headers, dict):
                    ua = headers.get("user-agent") or headers.get("User-Agent") or ""
                    if ua:
                        out["user_agent"] = str(ua)
        except Exception:
            pass
        # also from client.cookies if exposed
        try:
            cobj = getattr(client, "cookies", None)
            if isinstance(cobj, dict):
                cookies.update({str(k): str(v) for k, v in cobj.items()})
            elif cobj is not None and hasattr(cobj, "items"):
                for k, v in cobj.items():
                    cookies[str(k)] = str(v)
        except Exception:
            pass

        host = urlparse(page_url).hostname or "accounts.x.ai"
        pw_cookies = []
        for name, value in cookies.items():
            pw_cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": host,
                    "path": "/",
                }
            )
        out["cookies"] = pw_cookies
        out["cookie_names"] = list(cookies.keys())
        out["ok"] = True
        log(f"warm ok cookies={len(pw_cookies)} status={out.get('status')}")
        return out
    except Exception as exc:
        out["error"] = str(exc)[:300]
        log(f"warm fail: {exc}")
        return out


def close_all() -> None:
    with _LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for c in clients:
        try:
            c.close()
        except Exception:
            pass
