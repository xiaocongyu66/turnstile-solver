#!/usr/bin/env python3
"""
Standalone Turnstile browser worker for hybrid stack.

IPC: line-oriented JSON on stdin/stdout with solver-gateway (Go).

Does NOT depend on grok-free-register — self-contained Playwright solve path.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import glob
import json
import os
import resource
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("PYTHONMALLOC", "malloc")

DEFAULT_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
DEFAULT_PAGE = "https://accounts.x.ai/sign-up?redirect=grok-com"


def log(msg: str) -> None:
    sys.stderr.write(f"[browser-worker] {msg}\n")
    sys.stderr.flush()


def rss_mb() -> float:
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


def malloc_trim() -> None:
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass
    try:
        gc.collect(2)
    except Exception:
        pass


def _playwright_bundled_chrome() -> str | None:
    """Prefer modern Playwright-bundled Chromium (Turnstile rejects ancient 108)."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    except Exception:
        pass
    patterns = (
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
        os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell"),
        "/ms-playwright/chromium-*/chrome-linux/chrome",
        "/ms-playwright/chromium-*/chrome-linux/chromium",
        "/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
        "/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "/home/*/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
    )
    found: list[str] = []
    for pattern in patterns:
        found.extend(glob.glob(pattern))
    base = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/ms-playwright").strip()
    if os.path.isdir(base):
        found.extend(glob.glob(f"{base}/**/chrome", recursive=True))
        found.extend(glob.glob(f"{base}/**/chromium", recursive=True))
        found.extend(glob.glob(f"{base}/**/headless_shell", recursive=True))
    found = [p for p in found if os.path.isfile(p) and os.access(p, os.X_OK)]
    if found:
        return sorted(found)[-1]
    return None


def _system_chrome() -> str | None:
    for c in (
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/lib/chromium-browser/chromium-browser",
        "/usr/lib/chromium/chromium",
        "/usr/local/bin/chromium-browser",
    ):
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    if os.path.isfile("/etc/solver-chrome-path"):
        try:
            c = Path("/etc/solver-chrome-path").read_text(encoding="utf-8").strip()
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        except OSError:
            pass
    paths = glob.glob(os.path.expanduser("~/.cloakbrowser/chromium-*/chrome"))
    if paths:
        return sorted(paths)[-1]
    return None


def find_chrome() -> str | None:
    """Return chromium executable path, or None to let Playwright use its default.

    Order:
      1. Explicit env (SOLVER_CHROME_PATH / CHROME_PATH / PLAYWRIGHT_…)
      2. Playwright-bundled chromium under /ms-playwright (modern CF-friendly)
      3. System / Gitee chromium-browser (often 108 — last resort)
    """
    env = (
        os.environ.get("SOLVER_CHROME_PATH")
        or os.environ.get("CHROME_PATH")
        or os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or ""
    ).strip()
    force_system = (os.environ.get("SOLVER_FORCE_SYSTEM_CHROME") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    # Explicit path always wins, unless it's the old Gitee 108 and we have a modern bundle
    # and user did not force system chrome.
    if env and os.path.isfile(env):
        bundled = _playwright_bundled_chrome()
        if (
            not force_system
            and bundled
            and ("chromium-browser" in env or env.endswith("/chromium"))
            and "ms-playwright" not in env
        ):
            log(f"prefer playwright chromium over system {env} → {bundled}")
            return bundled
        return env
    bundled = _playwright_bundled_chrome()
    if bundled:
        return bundled
    return _system_chrome()


def read_cmd() -> Optional[dict[str, Any]]:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {"cmd": "ping"}
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        return {"cmd": "error", "error": f"bad json: {exc}"}


def write_resp(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class BrowserWorker:
    def __init__(
        self,
        *,
        worker_id: int,
        soft_mb: int,
        hard_mb: int,
        max_solves: int,
        timeout: float,
        concurrency: int = 1,
        headless: bool = True,
    ):
        self.worker_id = worker_id
        self.soft_mb = soft_mb
        self.hard_mb = hard_mb
        self.max_solves = max_solves
        self.timeout = timeout
        self.concurrency = max(1, int(concurrency))
        self.headless = headless
        self.solves = 0
        self.browser = None
        self.playwright = None
        self._sem = asyncio.Semaphore(self.concurrency)
        self._browser_lock = asyncio.Lock()

    async def ensure_browser(self) -> None:
        async with self._browser_lock:
            if self.browser is not None:
                try:
                    if self.browser.is_connected():
                        return
                except Exception:
                    pass
                await self._close_browser_unlocked()

            from playwright.async_api import async_playwright

            self.playwright = await async_playwright().start()
            exe = find_chrome()
            launch_kwargs: dict[str, Any] = {
                "headless": self.headless,
                "args": [
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-setuid-sandbox",
                ],
            }
            if exe:
                launch_kwargs["executable_path"] = exe
            # System chromium-browser (Ubuntu/Gitee) often needs these flags on HF/Docker
            if exe and ("chromium-browser" in exe or exe.endswith("/chromium")):
                launch_kwargs["args"] = list(launch_kwargs.get("args") or []) + [
                    "--single-process",  # reduce crash loops on constrained containers
                ]
                # remove single-process if it causes issues on large hosts — prefer stability first
                launch_kwargs["args"] = [
                    a for a in launch_kwargs["args"] if a != "--single-process"
                ]
                launch_kwargs["args"] += [
                    "--disable-software-rasterizer",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-features=TranslateUI",
                ]
            # If exe is None, Playwright uses its bundled chromium from
            # PLAYWRIGHT_BROWSERS_PATH / default cache.
            try:
                self.browser = await self.playwright.chromium.launch(**launch_kwargs)
            except Exception as exc:
                log(f"id={self.worker_id} launch failed ({exc}); retry without executable_path")
                launch_kwargs.pop("executable_path", None)
                self.browser = await self.playwright.chromium.launch(**launch_kwargs)
                exe = "playwright-default-retry"
            log(
                f"id={self.worker_id} browser launched exe={exe or 'playwright-default'} "
                f"rss={rss_mb():.1f}MB"
            )

    async def _close_browser_unlocked(self) -> None:
        b, self.browser = self.browser, None
        if b is not None:
            try:
                await b.close()
            except Exception:
                pass
        p, self.playwright = self.playwright, None
        if p is not None:
            try:
                await p.stop()
            except Exception:
                pass
        malloc_trim()
        log(f"id={self.worker_id} browser closed rss={rss_mb():.1f}MB")

    async def recycle(self) -> None:
        async with self._browser_lock:
            await self._close_browser_unlocked()
        self.solves = 0
        malloc_trim()

    def _need_recycle(self) -> bool:
        mb = rss_mb()
        if self.hard_mb > 0 and mb >= self.hard_mb:
            return True
        if self.soft_mb > 0 and mb >= self.soft_mb:
            return True
        if self.max_solves > 0 and self.solves >= self.max_solves:
            return True
        return False

    async def _solve_page(
        self,
        *,
        url: str,
        sitekey: str,
        proxy: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """Open page, inject Turnstile widget, wait for token."""
        assert self.browser is not None
        sitekey = (sitekey or "").strip() or DEFAULT_SITEKEY
        # Escape for JS string literals
        sk_js = sitekey.replace("\\", "\\\\").replace("'", "\\'")
        page_url = (url or "").strip() or DEFAULT_PAGE
        if "://" not in page_url:
            page_url = DEFAULT_PAGE
        # CF Turnstile domain-bind: for non-x.ai hosts, inject on about:blank is safer
        # than loading a page that does not host the sitekey (e.g. grok.com).
        use_blank = (os.environ.get("SOLVER_INJECT_BLANK") or "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        host = ""
        try:
            from urllib.parse import urlparse as _up

            host = (_up(page_url).hostname or "").lower()
        except Exception:
            host = ""
        if use_blank and host and "x.ai" not in host and "accounts.x.ai" not in host:
            # Keep page_url for logging; navigate to blank then set origin via set_content
            navigate_url = "about:blank"
        else:
            navigate_url = page_url

        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": ua,
            "ignore_https_errors": True,
            "locale": "en-US",
        }
        if proxy:
            from urllib.parse import urlparse

            u = urlparse(proxy)
            if u.scheme and u.hostname and u.port:
                server = f"{u.scheme}://{u.hostname}:{u.port}"
                px: dict[str, Any] = {"server": server}
                if u.username:
                    px["username"] = u.username
                if u.password:
                    px["password"] = u.password
                context_kwargs["proxy"] = px

        context = await self.browser.new_context(**context_kwargs)
        page = await context.new_page()
        trace: dict[str, Any] = {"page_url": page_url, "navigate": navigate_url, "sitekey": sitekey[:20]}
        t0 = time.time()
        try:
            try:
                if navigate_url == "about:blank":
                    await page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
                    # Synthetic origin page so Turnstile has a document + body
                    await page.set_content(
                        f"""<!doctype html><html><head><meta charset=utf-8>
<title>turnstile</title></head>
<body style="margin:0;background:#fff">
<div id="host"></div>
<script>window.__solver_page={json.dumps(page_url)};</script>
</body></html>""",
                        wait_until="domcontentloaded",
                    )
                else:
                    await page.goto(
                        navigate_url,
                        wait_until="domcontentloaded",
                        timeout=min(45000, int(self.timeout * 1000)),
                    )
            except Exception as goto_exc:
                log(f"id={self.worker_id} goto failed: {goto_exc}; try about:blank inject")
                await page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
                await page.set_content(
                    "<!doctype html><html><body style='margin:0'></body></html>",
                    wait_until="domcontentloaded",
                )
                trace["goto_fallback"] = str(goto_exc)[:200]
            trace["goto_s"] = round(time.time() - t0, 3)
            t1 = time.time()
            await page.evaluate(
                f"""() => {{
  if(!document.body){{
    document.documentElement.appendChild(document.createElement('body'));
  }}
  var d=document.createElement('div');
  d.className='cf-turnstile';
  d.setAttribute('data-sitekey','{sk_js}');
  d.style.cssText='position:fixed;top:10px;left:10px;z-index:99999;background:white;padding:12px;border:2px solid red;border-radius:6px;width:300px;height:70px';
  document.body.appendChild(d);
  var i=document.createElement('input');
  i.type='hidden'; i.name='cf-turnstile-response'; i.id='cf-turnstile-response';
  document.body.appendChild(i);
  function __r(){{
    if(!window.turnstile) return;
    try {{
      window.turnstile.render(d, {{
        sitekey: '{sk_js}',
        callback: function(t) {{
          var el=document.querySelector('input[name="cf-turnstile-response"]');
          if(!el){{ el=document.createElement('input'); el.type='hidden'; el.name='cf-turnstile-response'; document.body.appendChild(el); }}
          el.value=t;
          window.__cf_token=t;
        }},
        'error-callback': function(e) {{ window.__cf_err=String(e||'error'); }},
        'expired-callback': function() {{ window.__cf_err='expired'; }}
      }});
    }} catch (e) {{ window.__cf_err=String(e); }}
  }}
  if(window.turnstile){{ __r(); }}
  else {{
    var s=document.createElement('script');
    s.src='https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    s.async=true;
    s.onload=function(){{ setTimeout(__r, 400); }};
    s.onerror=function(){{ window.__cf_err='api.js load failed'; }};
    document.head.appendChild(s);
  }}
}}"""
            )
            trace["inject_s"] = round(time.time() - t1, 3)

            # mouse nudge on widget center (after short delay for iframe)
            await asyncio.sleep(0.8)
            try:
                box = await page.evaluate(
                    """() => {
                      const e = document.querySelector('.cf-turnstile, iframe[src*="challenges.cloudflare"]');
                      if (!e) return null;
                      const r = e.getBoundingClientRect();
                      return {x: r.left + r.width/2, y: r.top + r.height/2};
                    }"""
                )
                if box:
                    x, y = float(box["x"]), float(box["y"])
                    await page.mouse.move(max(0, x - 20), max(0, y - 6))
                    await page.mouse.move(x, y, steps=8)
                    await page.mouse.down()
                    await asyncio.sleep(0.06)
                    await page.mouse.up()
                    await page.mouse.click(x, y, delay=40)
            except Exception:
                pass

            t2 = time.time()
            token = ""
            deadline = time.time() + max(25.0, self.timeout - 5)
            while time.time() < deadline:
                try:
                    token = await page.evaluate(
                        """() => {
                          return window.__cf_token
                            || document.querySelector('input[name="cf-turnstile-response"]')?.value
                            || document.querySelector('#cf-turnstile-response')?.value
                            || '';
                        }"""
                    )
                    if not token:
                        err = await page.evaluate("() => window.__cf_err || ''")
                        if err:
                            trace["cf_err"] = str(err)[:200]
                except Exception:
                    token = ""
                if token and len(token) > 20:
                    break
                await asyncio.sleep(0.4)
            trace["wait_s"] = round(time.time() - t2, 3)
            if not token:
                try:
                    trace["has_turnstile"] = await page.evaluate("() => !!window.turnstile")
                    trace["iframe_n"] = await page.evaluate(
                        "() => document.querySelectorAll('iframe').length"
                    )
                except Exception:
                    pass
            return token or "", trace
        finally:
            try:
                await context.close()
            except Exception:
                pass

    async def solve(
        self,
        *,
        job_id: str,
        url: str,
        sitekey: str,
        action: str = "",
        cdata: str = "",
        proxy: str = "",
    ) -> dict[str, Any]:
        del action, cdata  # reserved for future
        t0 = time.time()
        recycled = False
        async with self._sem:
            try:
                if self._need_recycle():
                    await self.recycle()
                    recycled = True
                await self.ensure_browser()
                token, trace = await asyncio.wait_for(
                    self._solve_page(url=url, sitekey=sitekey, proxy=proxy),
                    timeout=self.timeout,
                )
                self.solves += 1
                elapsed = time.time() - t0
                if not token or len(str(token)) <= 10:
                    log(
                        f"id={self.worker_id} CAPTCHA_FAIL elapsed={elapsed:.1f}s "
                        f"trace={json.dumps(trace, ensure_ascii=False)[:400]}"
                    )
                    return {
                        "ok": False,
                        "id": job_id,
                        "error": "CAPTCHA_FAIL",
                        "elapsed_sec": round(elapsed, 3),
                        "rss_mb": round(rss_mb(), 2),
                        "recycled": recycled,
                        "trace": trace,
                    }
                log(f"id={self.worker_id} solved in {elapsed:.1f}s token={str(token)[:12]}...")
                return {
                    "ok": True,
                    "id": job_id,
                    "value": token,
                    "elapsed_sec": round(elapsed, 3),
                    "rss_mb": round(rss_mb(), 2),
                    "recycled": recycled,
                }
            except asyncio.TimeoutError:
                return {
                    "ok": False,
                    "id": job_id,
                    "error": "timeout",
                    "elapsed_sec": round(time.time() - t0, 3),
                    "rss_mb": round(rss_mb(), 2),
                    "recycled": recycled,
                }
            except Exception as exc:
                log(f"id={self.worker_id} solve exception: {exc}")
                return {
                    "ok": False,
                    "id": job_id,
                    "error": str(exc)[:400],
                    "elapsed_sec": round(time.time() - t0, 3),
                    "rss_mb": round(rss_mb(), 2),
                    "recycled": recycled,
                }
            finally:
                if self._need_recycle():
                    try:
                        await self.recycle()
                    except Exception:
                        pass
                else:
                    gc.collect()

    async def prefetch(self) -> dict[str, Any]:
        t0 = time.time()
        try:
            await self.ensure_browser()
            return {
                "ok": True,
                "cmd": "prefetch",
                "elapsed_sec": round(time.time() - t0, 3),
                "rss_mb": round(rss_mb(), 2),
            }
        except Exception as exc:
            return {
                "ok": False,
                "cmd": "prefetch",
                "error": str(exc)[:300],
                "elapsed_sec": round(time.time() - t0, 3),
                "rss_mb": round(rss_mb(), 2),
            }

    async def shutdown(self) -> None:
        await self.recycle()


async def amain(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Standalone hybrid Turnstile browser worker")
    p.add_argument("--worker-id", type=int, default=1)
    p.add_argument("--browser", default="chromium")
    p.add_argument("--headless", action="store_true", default=False)
    p.add_argument("--soft-mb", type=int, default=700)
    p.add_argument("--hard-mb", type=int, default=1100)
    p.add_argument("--max-solves", type=int, default=8)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--prefetch", action="store_true", default=False)
    p.add_argument("--proxy-file", default="")
    p.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("SOLVER_WORKER_TIMEOUT") or "90"),
    )
    args = p.parse_args(argv)
    headless = args.headless or (os.environ.get("TURNSTILE_SOLVER_HEADLESS") or "1").strip() not in {
        "0",
        "false",
        "no",
        "off",
    }

    worker = BrowserWorker(
        worker_id=args.worker_id,
        soft_mb=args.soft_mb,
        hard_mb=args.hard_mb,
        max_solves=args.max_solves,
        timeout=args.timeout,
        concurrency=args.concurrency,
        headless=headless,
    )
    log(
        f"ready id={args.worker_id} soft={args.soft_mb} hard={args.hard_mb} "
        f"max_solves={args.max_solves} conc={args.concurrency} backend=standalone"
    )

    # CLI --prefetch only warms the browser; NEVER write to stdout here.
    # Gateway owns the line protocol (IPC prefetch/solve). An unsolicited
    # prefetch JSON desyncs the next solve decode → empty-token CAPTCHA_FAIL.
    if args.prefetch:
        try:
            pref = await worker.prefetch()
            log(
                f"id={args.worker_id} cli-prefetch "
                f"ok={pref.get('ok')} rss={pref.get('rss_mb')} "
                f"err={pref.get('error') or ''}"
            )
        except Exception as exc:
            log(f"id={args.worker_id} cli-prefetch failed: {exc}")

    loop = asyncio.get_running_loop()
    while True:
        cmd = await loop.run_in_executor(None, read_cmd)
        if cmd is None:
            break
        name = str(cmd.get("cmd") or "").lower()
        if name in ("shutdown", "exit", "quit"):
            await worker.shutdown()
            write_resp({"ok": True, "cmd": "shutdown"})
            break
        if name == "ping":
            write_resp(
                {
                    "ok": True,
                    "cmd": "pong",
                    "rss_mb": round(rss_mb(), 2),
                    "solves": worker.solves,
                    "concurrency": worker.concurrency,
                }
            )
            continue
        if name == "prefetch":
            write_resp(await worker.prefetch())
            continue
        if name == "recycle":
            await worker.recycle()
            write_resp({"ok": True, "cmd": "recycle", "rss_mb": round(rss_mb(), 2), "recycled": True})
            continue
        if name == "error":
            write_resp({"ok": False, "error": cmd.get("error") or "bad command"})
            continue
        if name != "solve":
            write_resp({"ok": False, "error": f"unknown cmd: {name}"})
            continue
        resp = await worker.solve(
            job_id=str(cmd.get("id") or ""),
            url=str(cmd.get("url") or ""),
            sitekey=str(cmd.get("sitekey") or ""),
            action=str(cmd.get("action") or ""),
            cdata=str(cmd.get("cdata") or ""),
            proxy=str(cmd.get("proxy") or ""),
        )
        write_resp(resp)
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
