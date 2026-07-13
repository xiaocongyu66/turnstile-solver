#!/usr/bin/env python3
"""CF-Ares adapter aligned with grok_register.register usage.

Register pattern (source of truth):
  - sys.path → vendor/CF-Ares
  - AresClient(browser_engine, headless, timeout, proxy, chrome_path=CF_ARES_CHROME_PATH only)
  - Prefer client.get/post as HTTP transport (lazy browser on CF block)
  - On failure → curl_cffi with impersonate=chrome120
  - Does NOT force Playwright Chromium into undetected (causes driver version mismatch)

Solver usage:
  1) Warm session to accounts.x.ai via CF-Ares (same as register xAI HTTP path)
  2) Return cookies + UA for Playwright Turnstile inject
  3) Never rewrite vendor/CF-Ares

Env (same names as register where possible):
  CF_ARES / CF_ARES_XAI     0|1|fallback|always  (default 1)
  CF_ARES_BROWSER_ENGINE    auto|undetected|seleniumbase
  CF_ARES_HEADLESS          1
  CF_ARES_TIMEOUT           30
  CF_ARES_CHROME_PATH       optional; only this is passed to AresClient
  CF_ARES_PATH              vendor root override
  CF_ARES_IMPERSONATE       chrome120 (curl_cffi fallback)
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

# Mirror register paths
_HERE = Path(__file__).resolve()
_BUNDLED = _HERE.parents[1] / "vendor" / "CF-Ares"
_WORKER_VENDOR = _HERE.parent / "vendor" / "CF-Ares"


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
    # Prefer CF_ARES; accept CF_ARES_XAI like register
    return (
        os.environ.get("CF_ARES")
        or os.environ.get("CF_ARES_XAI")
        or "1"
    ).strip().lower()


def enabled() -> bool:
    return _mode() not in ("0", "false", "no", "off", "disabled")


def _always() -> bool:
    return _mode() == "always"


def _normalize_source_path(path: Path) -> Path:
    """Same as register._cf_ares_normalize_source_path."""
    if path.is_file():
        path = path.parent
    if path.name == "cf_ares":
        path = path.parent
    return path if (path / "cf_ares").is_dir() else path


def _add_import_path() -> None:
    """Same as register._cf_ares_add_import_path."""
    raw_paths = [
        _BUNDLED,
        _WORKER_VENDOR,
        Path("/app/vendor/CF-Ares"),
        Path("/app/worker/vendor/CF-Ares"),
    ]
    env_path = (os.environ.get("CF_ARES_PATH") or "").strip()
    if env_path:
        raw_paths.insert(0, Path(env_path).expanduser())
    for raw_path in raw_paths:
        try:
            candidate = _normalize_source_path(raw_path)
        except Exception:
            continue
        if not candidate.exists():
            continue
        text = str(candidate)
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)


def _client_class():
    """Same as register._cf_ares_client_class."""
    _add_import_path()
    try:
        from cf_ares import AresClient

        return AresClient
    except Exception:
        pass
    for name in list(sys.modules):
        if name == "cf_ares" or name.startswith("cf_ares."):
            sys.modules.pop(name, None)
    _add_import_path()
    from cf_ares import AresClient

    return AresClient


def available() -> bool:
    global _IMPORT_ERROR
    if not enabled():
        return False
    if _IMPORT_ERROR is not None and not _always() and _mode() not in (
        "1",
        "true",
        "yes",
        "on",
        "fallback",
    ):
        return False
    try:
        _client_class()
        return True
    except Exception as exc:
        _IMPORT_ERROR = exc
        log(f"import failed: {exc}")
        return False


def diagnose() -> dict[str, Any]:
    vendor = None
    for p in (
        Path(os.environ.get("CF_ARES_PATH") or ""),
        _BUNDLED,
        _WORKER_VENDOR,
        Path("/app/vendor/CF-Ares"),
        Path("/app/worker/vendor/CF-Ares"),
    ):
        if not p or str(p) == ".":
            continue
        try:
            n = _normalize_source_path(p.expanduser())
            if n.exists() and (n / "cf_ares").is_dir():
                vendor = n
                break
        except Exception:
            continue
    ok = False
    ver = ""
    err = ""
    try:
        AresClient = _client_class()
        import cf_ares

        ver = str(getattr(cf_ares, "__version__", "?"))
        ok = True
        del AresClient
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
        "engine": (os.environ.get("CF_ARES_BROWSER_ENGINE") or "auto"),
        "chrome_path": (os.environ.get("CF_ARES_CHROME_PATH") or "") or None,
    }


def get_client(proxy: Optional[str] = None):
    """
    Mirror register._cf_ares_get_client.

    Important: only pass chrome_path when CF_ARES_CHROME_PATH is set.
    Do NOT inject Playwright Chromium (breaks ChromeDriver version match).
    """
    global _IMPORT_ERROR
    if not available():
        raise RuntimeError(f"cf-ares unavailable: {_IMPORT_ERROR}")

    key = (proxy or "").strip() or "__direct__"
    with _LOCK:
        if key in _CLIENTS:
            return _CLIENTS[key]
        try:
            AresClient = _client_class()
        except Exception as exc:
            _IMPORT_ERROR = exc
            raise RuntimeError("cf-ares unavailable") from exc

        engine = (os.environ.get("CF_ARES_BROWSER_ENGINE") or "auto").strip() or "auto"
        kwargs: dict[str, Any] = {
            "browser_engine": engine,
            "headless": _env_bool("CF_ARES_HEADLESS", True),
            "timeout": _env_int("CF_ARES_TIMEOUT", 30),
            "debug": _env_bool("CF_ARES_DEBUG", False),
        }
        if proxy:
            kwargs["proxy"] = proxy
        # Register only sets chrome when env is set — same here
        chrome = (os.environ.get("CF_ARES_CHROME_PATH") or "").strip()
        if chrome and os.path.isfile(chrome):
            kwargs["chrome_path"] = chrome

        client = AresClient(**kwargs)
        _CLIENTS[key] = client
        log(
            f"AresClient ready engine={engine} proxy={proxy or 'direct'} "
            f"chrome={kwargs.get('chrome_path') or 'auto'}"
        )
        return client


def _looks_like_cloudflare_block(response) -> bool:
    """Same markers as register._looks_like_cloudflare_block."""
    status = getattr(response, "status_code", 200)
    if status not in (403, 503):
        return False
    text = getattr(response, "text", "") or ""
    lowered = text.lower()
    return any(
        m in lowered
        for m in (
            "cloudflare",
            "cf-browser-verification",
            "cf-im-under-attack",
            "challenge platform",
            "just a moment",
            "turnstile",
            "captcha",
            "error code: 1010",
        )
    )


def _curl_cffi_request(method: str, url: str, *, proxy: Optional[str] = None, **kwargs):
    """Same as register._curl_cffi_request."""
    from curl_cffi import requests as curl_requests

    request_kwargs = dict(kwargs)
    if proxy and "proxy" not in request_kwargs and "proxies" not in request_kwargs:
        request_kwargs["proxy"] = proxy
    impersonate = (os.environ.get("CF_ARES_IMPERSONATE") or "chrome120").strip()
    if impersonate and "impersonate" not in request_kwargs:
        request_kwargs["impersonate"] = impersonate
    return curl_requests.request(method.upper(), url, **request_kwargs)


def _cf_ares_http(method: str, url: str, *, proxy: Optional[str] = None, **kwargs):
    """
    Mirror register._cf_ares_request:
      client.get/post → on error → curl_cffi
    """
    with _LOCK:
        try:
            client = get_client(proxy=proxy)
            return getattr(client, method.lower())(url, **kwargs)
        except Exception as exc:
            log(f"AresClient {method} failed, curl_cffi fallback: {exc}")
            return _curl_cffi_request(method, url, proxy=proxy, **kwargs)


def _cookies_from_client(client, url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    cookies: dict[str, Any] = {}
    headers: dict[str, Any] = {}
    try:
        info = client.get_session_info(url)
        if isinstance(info, dict):
            c = info.get("cookies") or {}
            h = info.get("headers") or {}
            if isinstance(c, dict):
                cookies.update(c)
            if isinstance(h, dict):
                headers.update(h)
    except Exception as exc:
        log(f"get_session_info: {exc}")
    try:
        cobj = getattr(client, "cookies", None) or {}
        if isinstance(cobj, dict):
            cookies.update(cobj)
    except Exception:
        pass
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
    return cookies, headers


def _cookies_from_curl_response(response) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        jar = getattr(response, "cookies", None)
        if jar is None:
            return out
        if hasattr(jar, "items"):
            for k, v in jar.items():
                out[str(k)] = str(v)
        elif hasattr(jar, "get_dict"):
            out.update({str(k): str(v) for k, v in jar.get_dict().items()})
    except Exception:
        pass
    return out


def _to_playwright_cookies(cookies: dict[str, Any], host: str) -> list[dict[str, Any]]:
    pw: list[dict[str, Any]] = []
    for name, value in (cookies or {}).items():
        if isinstance(value, dict):
            pw.append(
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
            pw.append(
                {
                    "name": str(name),
                    "value": str(value),
                    "domain": host,
                    "path": "/",
                }
            )
    if host.endswith("x.ai"):
        extra = []
        for c in pw:
            c2 = dict(c)
            c2["domain"] = ".x.ai"
            extra.append(c2)
        pw.extend(extra)
    return pw


def warm_session(url: str, proxy: Optional[str] = None) -> dict[str, Any]:
    """
    Register-style warm: HTTP via CF-Ares (get), optional browser only if blocked.

    Order (matches register xAI HTTP path spirit):
      1) AresClient.get(url)  — lazy browser if CF page
      2) if still blocked / fail → curl_cffi impersonate
      3) optional explicit solve_challenge only if CF_ARES_SOLVE_CHALLENGE=1

    Returns Playwright cookie list + UA.
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
        "via": "",
        "has_cf_clearance": False,
    }
    t0 = time.time()
    cookies: dict[str, Any] = {}
    headers: dict[str, Any] = {}
    via = ""

    # --- Path 1: register-style AresClient.get ---
    if available():
        try:
            client = get_client(proxy=proxy)
            resp = client.get(page_url)
            status = getattr(resp, "status_code", None)
            result["status"] = status
            blocked = _looks_like_cloudflare_block(resp)
            cookies, headers = _cookies_from_client(client, page_url)
            via = "ares_get"
            if blocked:
                log(f"ares get still looks like CF block status={status}, try solve_challenge")
                # Path 1b: explicit challenge (optional, default on for solver)
                if _env_bool("CF_ARES_SOLVE_CHALLENGE", True):
                    try:
                        resp2 = client.solve_challenge(
                            page_url,
                            max_retries=_env_int("CF_ARES_MAX_RETRIES", 2),
                        )
                        result["status"] = getattr(resp2, "status_code", status)
                        cookies, headers = _cookies_from_client(client, page_url)
                        via = "solve_challenge"
                        blocked = False
                    except Exception as exc:
                        log(f"solve_challenge failed (like register continues): {exc}")
                        via = "ares_get_blocked"
            else:
                via = "ares_get"
            result["via"] = via
        except Exception as exc:
            log(f"ares path failed: {exc}")
            result["ares_error"] = str(exc)[:200]

    # --- Path 2: curl_cffi fallback (register._curl_cffi_request) ---
    if not cookies or result.get("status") in (403, 503) or not result.get("via"):
        try:
            resp = _curl_cffi_request("GET", page_url, proxy=proxy, timeout=_env_int("CF_ARES_TIMEOUT", 30))
            result["status"] = getattr(resp, "status_code", None)
            cc = _cookies_from_curl_response(resp)
            if cc:
                cookies.update(cc)
            if not via:
                via = "curl_cffi"
            elif via.startswith("ares"):
                via = via + "+curl_cffi"
            result["via"] = via
            # if still challenge body, mark weak
            if _looks_like_cloudflare_block(resp):
                result["still_blocked"] = True
                log(f"curl_cffi still CF-like status={result['status']}")
        except Exception as exc:
            log(f"curl_cffi fallback failed: {exc}")
            if not result.get("ares_error"):
                result["error"] = str(exc)[:300]

    ua = (
        headers.get("user-agent")
        or headers.get("User-Agent")
        or ""
    )
    if not ua:
        # match impersonate default
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    result["user_agent"] = str(ua)

    pw = _to_playwright_cookies(cookies, host)
    result["cookies"] = pw
    result["cookie_names"] = [c["name"] for c in pw]
    names_l = {n.lower() for n in result["cookie_names"]}
    result["has_cf_clearance"] = "cf_clearance" in names_l or any(
        "cf_clearance" in n for n in names_l
    )
    # Success criteria: got cookies or clean 2xx without block flag
    result["ok"] = bool(pw) or (
        result.get("status") in (200, 301, 302, 304) and not result.get("still_blocked")
    )
    result["elapsed_s"] = round(time.time() - t0, 3)
    result["via"] = result.get("via") or via or "none"
    log(
        f"warm ok={result['ok']} status={result.get('status')} "
        f"cookies={len(pw)} cf_clearance={result['has_cf_clearance']} "
        f"via={result['via']} {result['elapsed_s']}s"
    )
    return result


def solve_challenge(url: str, proxy: Optional[str] = None, **_kwargs) -> dict[str, Any]:
    """Alias — browser_worker calls this name; implement register-style warm."""
    return warm_session(url, proxy=proxy)


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
