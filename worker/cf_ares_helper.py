#!/usr/bin/env python3
"""Thin CF-Ares adapter for the Turnstile worker.

Does NOT reimplement CF-Ares. Calls vendor/CF-Ares the same way as
grok_register and the official README:

    from cf_ares import AresClient
    client = AresClient(browser_engine="undetected", proxy=..., headless=True)
    client.solve_challenge(url)
    info = client.get_session_info(url)   # cookies + headers
    # → hand cookies/UA to Playwright for Turnstile inject

Env:
  CF_ARES=1|0|auto          default 1
  CF_ARES_BROWSER_ENGINE    undetected|seleniumbase|auto  (default undetected)
  CF_ARES_HEADLESS          1
  CF_ARES_TIMEOUT           60
  CF_ARES_MAX_RETRIES       3
  CF_ARES_CHROME_PATH       optional Chrome binary
  CF_ARES_PATH              vendor root that contains cf_ares/ package
  CF_ARES_DEBUG             0
"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

_LOCK = threading.Lock()
_CLIENTS: dict[str, Any] = {}
_IMPORT_ERROR: Optional[BaseException] = None
_PATH_READY = False


def log(msg: str) -> None:
    sys.stderr.write(f"[cf-ares] {msg}\n")
    sys.stderr.flush()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _mode() -> str:
    return (os.environ.get("CF_ARES") or "1").strip().lower()


def enabled() -> bool:
    return _mode() not in ("0", "false", "no", "off", "disabled")


def _vendor_roots() -> list[Path]:
    """Candidate directories that contain the cf_ares package."""
    roots: list[Path] = []
    raw = (os.environ.get("CF_ARES_PATH") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        roots.append(p)
        if p.name == "cf_ares":
            roots.append(p.parent)
    here = Path(__file__).resolve()
    # worker/cf_ares_helper.py → ../vendor/CF-Ares  or  worker/vendor/CF-Ares
    roots.extend(
        [
            here.parents[1] / "vendor" / "CF-Ares",
            here.parent / "vendor" / "CF-Ares",
            Path("/app/vendor/CF-Ares"),
            Path("/app/worker/vendor/CF-Ares"),
            Path("/opt/vendor/CF-Ares"),
        ]
    )
    # unique preserve order
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        k = str(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _find_vendor() -> Optional[Path]:
    for root in _vendor_roots():
        if (root / "cf_ares" / "__init__.py").is_file():
            return root
        if (root / "cf_ares").is_dir():
            return root
    return None


def ensure_import_path() -> bool:
    """Put vendor/CF-Ares on sys.path (same idea as register._cf_ares_add_import_path)."""
    global _PATH_READY
    if _PATH_READY:
        return True
    root = _find_vendor()
    if root is not None:
        text = str(root)
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
        _PATH_READY = True
        return True
    # pip-installed cf-ares
    try:
        import cf_ares  # noqa: F401

        _PATH_READY = True
        return True
    except Exception:
        return False


def available() -> bool:
    """True if CF-Ares can be imported and is enabled."""
    global _IMPORT_ERROR
    if not enabled():
        return False
    if _IMPORT_ERROR is not None and _mode() not in ("1", "true", "yes", "on", "always"):
        return False
    if not ensure_import_path():
        if _mode() in ("1", "true", "yes", "on", "always"):
            log(
                f"vendor missing; tried={[str(p) for p in _vendor_roots()[:5]]} "
                f"app={Path('/app/vendor/CF-Ares').exists()}"
            )
        return False
    try:
        from cf_ares import AresClient  # noqa: F401

        return True
    except Exception as exc:
        _IMPORT_ERROR = exc
        log(f"import failed: {exc}")
        return False


def diagnose() -> dict[str, Any]:
    vendor = _find_vendor()
    ok = False
    err = ""
    ver = ""
    try:
        if ensure_import_path():
            import cf_ares

            ver = str(getattr(cf_ares, "__version__", "?"))
            from cf_ares import AresClient  # noqa: F401

            ok = True
    except Exception as exc:
        err = str(exc)[:300]
    return {
        "enabled": enabled(),
        "available": ok and enabled(),
        "version": ver,
        "vendor_path": str(vendor) if vendor else None,
        "exists_app_vendor": Path("/app/vendor/CF-Ares").exists(),
        "exists_worker_vendor": Path("/app/worker/vendor/CF-Ares").exists(),
        "error": err,
        "engine": (os.environ.get("CF_ARES_BROWSER_ENGINE") or "undetected"),
    }


def _chrome_path() -> Optional[str]:
    """Prefer modern Chromium for undetected-chromedriver."""
    import glob

    for key in ("CF_ARES_CHROME_PATH", "SOLVER_CHROME_PATH", "CHROME_PATH"):
        p = (os.environ.get(key) or "").strip()
        if p and os.path.isfile(p) and "ms-playwright" in p:
            return p
    for pattern in (
        "/ms-playwright/chromium-*/chrome-linux64/chrome",
        "/ms-playwright/chromium-*/chrome-linux/chrome",
    ):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    for key in ("CF_ARES_CHROME_PATH", "SOLVER_CHROME_PATH", "CHROME_PATH"):
        p = (os.environ.get(key) or "").strip()
        if p and os.path.isfile(p):
            return p
    for c in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if os.path.isfile(c):
            return c
    return None


def get_client(proxy: Optional[str] = None):
    """
    Lazy AresClient per proxy key — same pattern as register._cf_ares_get_client.

        client = AresClient(
            browser_engine="undetected",
            headless=True,
            proxy=proxy,
            timeout=60,
            chrome_path=...,
        )
    """
    global _IMPORT_ERROR
    if not available():
        raise RuntimeError(f"cf-ares unavailable: {_IMPORT_ERROR}")

    key = (proxy or "").strip() or "__direct__"
    with _LOCK:
        if key in _CLIENTS:
            return _CLIENTS[key]

        from cf_ares import AresClient

        engine = (os.environ.get("CF_ARES_BROWSER_ENGINE") or "undetected").strip()
        if engine in ("", "auto"):
            engine = "undetected"

        kwargs: dict[str, Any] = {
            "browser_engine": engine,
            "headless": _env_bool("CF_ARES_HEADLESS", True),
            "timeout": _env_int("CF_ARES_TIMEOUT", 60),
            "max_retries": _env_int("CF_ARES_MAX_RETRIES", 3),
            "debug": _env_bool("CF_ARES_DEBUG", False),
        }
        if proxy:
            kwargs["proxy"] = proxy
        chrome = _chrome_path()
        if chrome:
            kwargs["chrome_path"] = chrome

        client = AresClient(**kwargs)
        _CLIENTS[key] = client
        log(
            f"AresClient ready engine={engine} proxy={proxy or 'direct'} "
            f"chrome={chrome or 'default'}"
        )
        return client


def _to_playwright_cookies(cookies: Any, host: str) -> list[dict[str, Any]]:
    """Normalize CF-Ares cookie dict → Playwright add_cookies() list."""
    if not cookies:
        return []
    items = []
    if isinstance(cookies, dict):
        items = list(cookies.items())
    elif hasattr(cookies, "items"):
        try:
            items = list(cookies.items())
        except Exception:
            items = []

    out: list[dict[str, Any]] = []
    for name, value in items:
        if isinstance(value, dict):
            out.append(
                {
                    "name": str(value.get("name") or name),
                    "value": str(value.get("value") or ""),
                    "domain": str(value.get("domain") or host),
                    "path": str(value.get("path") or "/"),
                }
            )
        else:
            if value is None:
                continue
            out.append(
                {
                    "name": str(name),
                    "value": str(value),
                    "domain": host,
                    "path": "/",
                }
            )
    # also set parent domain for accounts.x.ai
    if host.endswith(".x.ai") or host == "x.ai":
        extra = []
        for c in out:
            c2 = dict(c)
            c2["domain"] = ".x.ai"
            extra.append(c2)
        out.extend(extra)
    return out


def solve_challenge(
    url: str,
    proxy: Optional[str] = None,
    *,
    max_retries: Optional[int] = None,
) -> dict[str, Any]:
    """
    Official flow:

        response = client.solve_challenge(url)
        session_info = client.get_session_info(url)

    Returns Playwright-ready cookies + UA. Never raises to caller for soft fails;
    returns {ok: False, error: ...}.
    """
    page_url = (url or "").strip() or "https://accounts.x.ai/sign-up"
    if "://" not in page_url:
        page_url = "https://accounts.x.ai/sign-up"
    host = urlparse(page_url).hostname or "accounts.x.ai"

    result: dict[str, Any] = {
        "ok": False,
        "url": page_url,
        "cookies": [],
        "user_agent": "",
        "status": None,
        "cookie_names": [],
        "challenge": "",
    }
    t0 = time.time()

    try:
        client = get_client(proxy)
        retries = (
            max_retries
            if max_retries is not None
            else _env_int("CF_ARES_MAX_RETRIES", 3)
        )

        # 1) explicit challenge (README)
        try:
            from cf_ares.exceptions import CloudflareChallengeFailed
        except Exception:
            CloudflareChallengeFailed = Exception  # type: ignore

        try:
            resp = client.solve_challenge(page_url, max_retries=max(1, retries))
            result["status"] = getattr(resp, "status_code", None)
            result["challenge"] = "solve_challenge"
        except CloudflareChallengeFailed as exc:
            log(f"solve_challenge failed: {exc}; fallback client.get()")
            try:
                resp = client.get(page_url)
                result["status"] = getattr(resp, "status_code", None)
                result["challenge"] = "get_fallback"
                result["challenge_error"] = str(exc)[:200]
            except Exception as exc2:
                result["error"] = f"challenge failed: {exc}; get: {exc2}"[:300]
                result["elapsed_s"] = round(time.time() - t0, 3)
                return result
        except Exception as exc:
            log(f"solve_challenge error: {exc}; fallback client.get()")
            try:
                resp = client.get(page_url)
                result["status"] = getattr(resp, "status_code", None)
                result["challenge"] = "get_only"
                result["challenge_error"] = str(exc)[:200]
            except Exception as exc2:
                result["error"] = str(exc2)[:300]
                result["elapsed_s"] = round(time.time() - t0, 3)
                return result

        # 2) session_info (README)
        cookies: dict[str, Any] = {}
        headers: dict[str, Any] = {}
        try:
            info = client.get_session_info(page_url)
            if isinstance(info, dict):
                c = info.get("cookies") or {}
                h = info.get("headers") or {}
                if isinstance(c, dict):
                    cookies.update(c)
                if isinstance(h, dict):
                    headers.update(h)
        except Exception as exc:
            log(f"get_session_info: {exc}")

        # also merge client.cookies property
        try:
            cobj = getattr(client, "cookies", None) or {}
            if isinstance(cobj, dict):
                cookies.update(cobj)
        except Exception:
            pass

        # browser engine cookies (full set)
        try:
            eng = getattr(client, "_browser_engine", None)
            if eng is not None and hasattr(eng, "get_cookies"):
                bc = eng.get_cookies()
                if isinstance(bc, dict):
                    cookies.update(bc)
            if eng is not None and hasattr(eng, "get_headers"):
                bh = eng.get_headers() or {}
                if isinstance(bh, dict):
                    headers.update(bh)
        except Exception:
            pass

        ua = (
            headers.get("user-agent")
            or headers.get("User-Agent")
            or headers.get("User-agent")
            or ""
        )
        result["user_agent"] = str(ua) if ua else ""

        pw = _to_playwright_cookies(cookies, host)
        result["cookies"] = pw
        result["cookie_names"] = [c["name"] for c in pw]
        # success if we got clearance-ish cookies or 2xx
        names_l = {n.lower() for n in result["cookie_names"]}
        has_cf = any(
            n in names_l for n in ("cf_clearance", "cf_bm", "__cf_bm", "cf-chl-rc")
        )
        result["ok"] = bool(pw) or (result.get("status") in (200, 301, 302, 304))
        result["has_cf_clearance"] = has_cf or ("cf_clearance" in names_l)
        result["elapsed_s"] = round(time.time() - t0, 3)
        log(
            f"ok={result['ok']} status={result.get('status')} "
            f"cookies={len(pw)} cf_clearance={result['has_cf_clearance']} "
            f"via={result.get('challenge')} {result['elapsed_s']}s"
        )
        return result
    except Exception as exc:
        result["error"] = str(exc)[:300]
        result["elapsed_s"] = round(time.time() - t0, 3)
        log(f"fatal: {exc}")
        return result


def warm_session(url: str, proxy: Optional[str] = None) -> dict[str, Any]:
    """Alias used by browser_worker."""
    return solve_challenge(url, proxy=proxy)


def close_all() -> None:
    with _LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for c in clients:
        try:
            c.close()
        except Exception:
            pass


atexit.register(close_all)


if __name__ == "__main__":
    import json

    print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
