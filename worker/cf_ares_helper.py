#!/usr/bin/env python3
"""CF-Ares integration (official API style).

Flow (same as CF-Ares README):
  1) AresClient(browser_engine=undetected|seleniumbase|auto)
  2) client.solve_challenge(url)  — browser breaks CF shield
  3) client.get_session_info(url) — cookies / headers
  4) Hand cookies + UA to Playwright for Turnstile widget inject
  5) Optional: curl_cffi session reuse via client.get()

Env:
  CF_ARES=1|0|auto
  CF_ARES_BROWSER_ENGINE=undetected|seleniumbase|auto
  CF_ARES_HEADLESS=1
  CF_ARES_TIMEOUT=60
  CF_ARES_MAX_RETRIES=2
  CF_ARES_CHROME_PATH=
  CF_ARES_PATH=              vendor root containing cf_ares/
  CF_ARES_SESSION_DIR=/tmp/solver-cf-ares-sessions
  CF_ARES_DEBUG=0
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

_LOCK = threading.Lock()
_IMPORT_ERROR: Optional[BaseException] = None
_CLIENTS: dict[str, Any] = {}
_ARES_MOD = None


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


def _vendor_candidates() -> list[Path]:
    raw = (os.environ.get("CF_ARES_PATH") or "").strip()
    here = Path(__file__).resolve()
    out: list[Path] = []
    if raw:
        p = Path(raw).expanduser()
        out.append(p)
        if p.name == "cf_ares":
            out.append(p.parent)
    out.extend(
        [
            here.parents[1] / "vendor" / "CF-Ares",
            here.parent / "vendor" / "CF-Ares",
            Path("/app/vendor/CF-Ares"),
            Path("/app/worker/vendor/CF-Ares"),
            Path("/opt/vendor/CF-Ares"),
            # lowercase variants
            Path("/app/vendor/cf-ares"),
            Path("/app/vendor/cf_ares"),
        ]
    )
    # de-dupe
    seen: set[str] = set()
    uniq: list[Path] = []
    for c in out:
        k = str(c)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def _vendor_path() -> Optional[Path]:
    for c in _vendor_candidates():
        if (c / "cf_ares" / "__init__.py").is_file():
            return c
        if (c / "cf_ares").is_dir():
            return c
        if c.is_dir() and c.name == "cf_ares" and (c / "__init__.py").is_file():
            return c.parent
    return None


def add_import_path() -> bool:
    """Prefer vendored tree; fall back to site-packages (pip install cf-ares)."""
    root = _vendor_path()
    if root is not None:
        text = str(root)
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
        return True
    try:
        import cf_ares  # noqa: F401

        return True
    except Exception:
        return False


def _import_ares():
    """Import AresClient + exceptions; cache module."""
    global _ARES_MOD, _IMPORT_ERROR
    if _ARES_MOD is not None:
        return _ARES_MOD
    if not add_import_path():
        raise RuntimeError(
            "cf-ares not found (vendor missing and pip package not installed). "
            f"candidates={[str(p) for p in _vendor_candidates()[:6]]}"
        )
    try:
        from cf_ares import AresClient  # type: ignore
        from cf_ares.exceptions import (  # type: ignore
            CloudflareChallengeFailed,
            CloudflareSessionExpired,
        )
    except Exception:
        # older layouts
        from cf_ares import AresClient  # type: ignore

        try:
            from cf_ares import CloudflareChallengeFailed, CloudflareSessionExpired  # type: ignore
        except Exception:

            class CloudflareChallengeFailed(Exception):
                pass

            class CloudflareSessionExpired(Exception):
                pass

    _ARES_MOD = {
        "AresClient": AresClient,
        "CloudflareChallengeFailed": CloudflareChallengeFailed,
        "CloudflareSessionExpired": CloudflareSessionExpired,
    }
    return _ARES_MOD


def available() -> bool:
    global _IMPORT_ERROR
    mode = (os.environ.get("CF_ARES") or "auto").strip().lower()
    if mode in ("0", "false", "no", "off", "disabled"):
        return False
    if _IMPORT_ERROR is not None and mode not in ("1", "true", "yes", "on", "always"):
        return False
    try:
        _import_ares()
        return True
    except Exception as exc:
        _IMPORT_ERROR = exc
        if mode in ("1", "true", "yes", "on", "always"):
            vp = _vendor_path()
            log(
                f"unavailable: {exc} | vendor={vp} "
                f"exists_app={Path('/app/vendor/CF-Ares').exists()} "
                f"exists_worker={Path('/app/worker/vendor/CF-Ares').exists()}"
            )
        return False


def diagnose() -> dict[str, Any]:
    """For entrypoint / logs."""
    vp = _vendor_path()
    ok = False
    err = ""
    try:
        _import_ares()
        ok = True
    except Exception as exc:
        err = str(exc)[:300]
    return {
        "available": ok,
        "vendor_path": str(vp) if vp else None,
        "exists_app_vendor": Path("/app/vendor/CF-Ares").exists(),
        "exists_worker_vendor": Path("/app/worker/vendor/CF-Ares").exists(),
        "error": err,
        "engine": (os.environ.get("CF_ARES_BROWSER_ENGINE") or "undetected"),
        "mode": (os.environ.get("CF_ARES") or "auto"),
    }


def _chrome_path() -> Optional[str]:
    for key in ("CF_ARES_CHROME_PATH", "SOLVER_CHROME_PATH", "CHROME_PATH"):
        p = (os.environ.get(key) or "").strip()
        # Prefer modern playwright chrome over ancient system 108 for undetected
        if p and os.path.isfile(p) and "ms-playwright" in p:
            return p
    import glob

    for pattern in (
        "/ms-playwright/chromium-*/chrome-linux64/chrome",
        "/ms-playwright/chromium-*/chrome-linux/chrome",
        "/ms-playwright/chromium-*/chrome-linux64/chromium",
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


def _session_dir() -> Path:
    d = Path(
        (os.environ.get("CF_ARES_SESSION_DIR") or "/tmp/solver-cf-ares-sessions").strip()
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_client(proxy: Optional[str] = None, *, fresh: bool = False):
    """
    Create / cache AresClient.

    Official style:
      client = AresClient(browser_engine="undetected", headless=True, proxy=...)
    """
    global _IMPORT_ERROR
    mod = _import_ares()
    AresClient = mod["AresClient"]
    key = (proxy or "").strip() or "__direct__"
    with _LOCK:
        if not fresh and key in _CLIENTS:
            return _CLIENTS[key]
        if fresh and key in _CLIENTS:
            try:
                _CLIENTS[key].close()
            except Exception:
                pass
            _CLIENTS.pop(key, None)

        engine = (os.environ.get("CF_ARES_BROWSER_ENGINE") or "undetected").strip() or "undetected"
        # map "auto" → undetected (official default path)
        if engine in ("auto", ""):
            engine = "undetected"

        kwargs: dict[str, Any] = {
            "browser_engine": engine,
            "headless": _env_bool("CF_ARES_HEADLESS", True),
            "timeout": _env_int("CF_ARES_TIMEOUT", 60),
            "debug": _env_bool("CF_ARES_DEBUG", False),
            "max_retries": _env_int("CF_ARES_MAX_RETRIES", 2),
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
            f"chrome={chrome or 'default'} headless={kwargs['headless']}"
        )
        return client


def _cookies_to_playwright(cookies: Any, host: str) -> list[dict[str, Any]]:
    pw: list[dict[str, Any]] = []
    if not cookies:
        return pw
    if isinstance(cookies, dict):
        items = cookies.items()
    elif hasattr(cookies, "items"):
        try:
            items = list(cookies.items())
        except Exception:
            items = []
    else:
        items = []
    for name, value in items:
        if value is None:
            continue
        # selenium-style dict cookie
        if isinstance(value, dict):
            c = {
                "name": str(value.get("name") or name),
                "value": str(value.get("value") or ""),
                "domain": str(value.get("domain") or host),
                "path": str(value.get("path") or "/"),
            }
            if value.get("httpOnly") is not None:
                c["httpOnly"] = bool(value["httpOnly"])
            if value.get("secure") is not None:
                c["secure"] = bool(value["secure"])
            pw.append(c)
        else:
            pw.append(
                {
                    "name": str(name),
                    "value": str(value),
                    "domain": host,
                    "path": "/",
                }
            )
    return pw


def solve_challenge(
    url: str,
    proxy: Optional[str] = None,
    *,
    max_retries: Optional[int] = None,
    save_session: bool = True,
) -> dict[str, Any]:
    """
    Official flow:
      response = client.solve_challenge(url)
      session_info = client.get_session_info(url)
      client.save_session(...)

    Returns dict for Playwright handoff.
    """
    page_url = (url or "").strip() or "https://accounts.x.ai/sign-up"
    if "://" not in page_url:
        page_url = "https://accounts.x.ai/sign-up"
    host = urlparse(page_url).hostname or "accounts.x.ai"
    out: dict[str, Any] = {
        "ok": False,
        "url": page_url,
        "cookies": [],
        "user_agent": "",
        "status": None,
        "cookie_names": [],
    }
    retries = max_retries if max_retries is not None else _env_int("CF_ARES_MAX_RETRIES", 2)
    t0 = time.time()
    try:
        mod = _import_ares()
        CloudflareChallengeFailed = mod["CloudflareChallengeFailed"]
        client = get_client(proxy)

        # 1) explicit challenge
        try:
            resp = client.solve_challenge(page_url, max_retries=max(1, retries))
            out["status"] = getattr(resp, "status_code", None)
            out["challenge"] = "solve_challenge"
        except CloudflareChallengeFailed as exc:
            log(f"solve_challenge failed: {exc}; retry with get()")
            # 2) fallback: get() may still trigger internal CF handling
            try:
                resp = client.get(page_url)
                out["status"] = getattr(resp, "status_code", None)
                out["challenge"] = "get_fallback"
                out["challenge_error"] = str(exc)[:200]
            except Exception as exc2:
                out["error"] = f"solve_challenge: {exc}; get: {exc2}"[:300]
                log(out["error"])
                return out
        except Exception as exc:
            # some builds don't raise typed exception
            log(f"solve_challenge error: {exc}; try get()")
            try:
                resp = client.get(page_url)
                out["status"] = getattr(resp, "status_code", None)
                out["challenge"] = "get_only"
                out["challenge_error"] = str(exc)[:200]
            except Exception as exc2:
                out["error"] = str(exc2)[:300]
                return out

        # 3) session_info (official)
        cookies: dict[str, Any] = {}
        headers: dict[str, Any] = {}
        try:
            info = client.get_session_info(page_url)
            if isinstance(info, dict):
                raw_c = info.get("cookies") or {}
                if isinstance(raw_c, dict):
                    cookies.update(raw_c)
                raw_h = info.get("headers") or {}
                if isinstance(raw_h, dict):
                    headers.update(raw_h)
        except Exception as exc:
            log(f"get_session_info: {exc}")

        # merge client.cookies if present
        try:
            cobj = getattr(client, "cookies", None)
            if isinstance(cobj, dict):
                cookies.update(cobj)
            elif cobj is not None and hasattr(cobj, "get_dict"):
                cookies.update(cobj.get_dict())
            elif cobj is not None and hasattr(cobj, "items"):
                for k, v in cobj.items():
                    cookies[str(k)] = v
        except Exception:
            pass

        # browser engine cookies (more complete for Playwright)
        try:
            eng = getattr(client, "_browser_engine", None)
            if eng is not None and hasattr(eng, "get_cookies"):
                bc = eng.get_cookies()
                if isinstance(bc, dict):
                    cookies.update(bc)
        except Exception:
            pass

        ua = (
            headers.get("user-agent")
            or headers.get("User-Agent")
            or headers.get("User-agent")
            or ""
        )
        if not ua:
            try:
                eng = getattr(client, "_browser_engine", None)
                if eng is not None and hasattr(eng, "get_headers"):
                    eh = eng.get_headers() or {}
                    ua = eh.get("user-agent") or eh.get("User-Agent") or ""
            except Exception:
                pass
        out["user_agent"] = str(ua) if ua else ""

        # 4) save_session (official)
        if save_session:
            try:
                path = _session_dir() / f"session_{(proxy or 'direct').replace('/', '_')[:40]}.json"
                if hasattr(client, "save_session"):
                    client.save_session(str(path))
                    out["session_file"] = str(path)
            except Exception as exc:
                log(f"save_session: {exc}")

        pw_cookies = _cookies_to_playwright(cookies, host)
        # also set domain variants for .x.ai
        extra = []
        for c in pw_cookies:
            if "x.ai" in host and not str(c.get("domain", "")).startswith("."):
                c2 = dict(c)
                c2["domain"] = ".x.ai"
                extra.append(c2)
        pw_cookies.extend(extra)

        out["cookies"] = pw_cookies
        out["cookie_names"] = [c["name"] for c in pw_cookies]
        out["ok"] = bool(pw_cookies) or (out.get("status") in (200, 301, 302, 304))
        out["elapsed_s"] = round(time.time() - t0, 3)
        log(
            f"challenge done ok={out['ok']} status={out.get('status')} "
            f"cookies={len(pw_cookies)} elapsed={out['elapsed_s']}s "
            f"via={out.get('challenge')}"
        )
        return out
    except Exception as exc:
        out["error"] = str(exc)[:300]
        out["elapsed_s"] = round(time.time() - t0, 3)
        log(f"solve_challenge fatal: {exc}")
        return out


def warm_session(url: str, proxy: Optional[str] = None) -> dict[str, Any]:
    """Alias used by browser_worker — full official challenge path."""
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


if __name__ == "__main__":
    import json

    print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
    if available():
        r = solve_challenge(
            os.environ.get("CF_ARES_TEST_URL") or "https://accounts.x.ai/sign-up",
            proxy=(os.environ.get("SOLVER_PROXY") or "").strip() or None,
        )
        print(json.dumps({k: r.get(k) for k in ("ok", "status", "cookie_names", "error", "elapsed_s")}, indent=2))
