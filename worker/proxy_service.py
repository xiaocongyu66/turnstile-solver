#!/usr/bin/env python3
"""Standalone sing-box proxy service for HF / Docker.

Based on https://github.com/SagerNet/sing-box

Features:
  - Multi-protocol: hysteria2/hy2, vmess, vless, trojan, ss, socks5, http
  - Base64 subscription / multi-line PROXY_POOL
  - Custom DNS (sing-box dns module)
  - Single mixed inbound (HTTP+SOCKS) for global use
  - urltest selector across nodes (optional)
  - Runs as its own process; solver/register set HTTP_PROXY to this port

Env:
  PROXY_POOL / PROXIES / PROXY_LIST     share links or base64 sub
  PROXY_POOL_FILE                      one link per line
  PROXY_SERVICE_HOST                   default 127.0.0.1 (use 0.0.0.0 to expose)
  PROXY_SERVICE_PORT                   mixed inbound, default 7890
  PROXY_SERVICE_DNS                    comma list, default 1.1.1.1,8.8.8.8
  PROXY_SERVICE_DNS_STRATEGY           prefer_ipv4|prefer_ipv6|ipv4_only|ipv6_only
  PROXY_SERVICE_MODE                   urltest|round_robin|first  (default urltest)
  PROXY_SERVICE_URLTEST_URL            default https://www.gstatic.com/generate_204
  PROXY_SERVICE_URLTEST_INTERVAL      3m
  PROXY_SERVICE_WORK_DIR               /tmp/solver-proxy-service
  PROXY_SERVICE_SING_BOX               path to sing-box binary
  PROXY_RELAY_AUTO_INSTALL             1 download sing-box if missing
  PROXY_SERVICE_LOG_LEVEL              warn|info|debug
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# local imports (same dir)
_WORKER = Path(__file__).resolve().parent
if str(_WORKER) not in sys.path:
    sys.path.insert(0, str(_WORKER))

from proxy_relay import (  # noqa: E402
    install_sing_box,
    share_link_to_sing_box_outbound,
)


def log(msg: str) -> None:
    sys.stderr.write(f"[proxy-service] {msg}\n")
    sys.stderr.flush()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _maybe_b64(text: str) -> str:
    s = (text or "").strip()
    if not s or "://" in s:
        return s
    compact = re.sub(r"\s+", "", s)
    if len(compact) < 40 or not re.fullmatch(r"[A-Za-z0-9+/_-]+=*", compact):
        return s
    try:
        pad = "=" * (-len(compact) % 4)
        try:
            data = base64.urlsafe_b64decode(compact + pad)
        except Exception:
            data = base64.b64decode(compact + pad)
        decoded = data.decode("utf-8", errors="replace")
        if "://" in decoded:
            log(f"decoded base64 subscription ({len(decoded.splitlines())} lines)")
            return decoded
    except Exception as exc:
        log(f"base64 skip: {exc}")
    return s


def load_share_links() -> list[str]:
    text = (
        _env("PROXY_POOL")
        or _env("PROXY_POOL_LIST")
        or _env("PROXIES")
        or _env("PROXY_LIST")
        or _env("SOLVER_PROXY")
        or ""
    )
    file_path = _env("PROXY_POOL_FILE")
    chunks: list[str] = []
    if text:
        chunks.append(_maybe_b64(text))
    if file_path:
        try:
            chunks.append(Path(file_path).expanduser().read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            log(f"PROXY_POOL_FILE: {exc}")
    lines: list[str] = []
    seen: set[str] = set()
    for blob in chunks:
        blob = _maybe_b64(blob)
        for part in re.split(r"[\n,;|]+", blob):
            line = (part or "").strip()
            if not line or line.startswith("#"):
                continue
            if line not in seen:
                seen.add(line)
                lines.append(line)
    return lines


def _tag_for(i: int, link: str) -> str:
    try:
        frag = urlparse(link).fragment
        if frag:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", frag)[:32]
            if safe:
                return f"n{i}_{safe}"
    except Exception:
        pass
    return f"proxy-{i}"


def build_outbounds(links: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    outbounds: list[dict[str, Any]] = []
    tags: list[str] = []
    for i, link in enumerate(links, 1):
        try:
            ob = share_link_to_sing_box_outbound(link)
        except Exception as exc:
            log(f"skip link[{i}]: {exc} ({link[:48]}...)")
            continue
        tag = _tag_for(i, link)
        # avoid tag clash
        base = tag
        n = 2
        while any(o.get("tag") == tag for o in outbounds):
            tag = f"{base}_{n}"
            n += 1
        ob["tag"] = tag
        outbounds.append(ob)
        tags.append(tag)
        log(f"outbound {tag} type={ob.get('type')} server={ob.get('server')}:{ob.get('server_port')}")
    return outbounds, tags


def build_dns(servers: list[str], strategy: str) -> dict[str, Any]:
    """sing-box dns block — https://sing-box.sagernet.org/configuration/dns/"""
    dns_servers: list[dict[str, Any]] = []
    for s in servers:
        s = s.strip()
        if not s:
            continue
        # support udp://1.1.1.1 or plain IP or https://dns.google/dns-query
        if s.startswith("https://") or s.startswith("h3://"):
            dns_servers.append({"type": "https", "server": s.replace("https://", "").split("/")[0], "path": "/" + "/".join(s.split("/")[3:]) if s.count("/") > 2 else "/dns-query", "detour": "direct"})
            # simplify: use address form for older sing-box
            dns_servers[-1] = {"address": s, "detour": "direct"}
        elif s.startswith("tcp://"):
            dns_servers.append({"address": s, "detour": "direct"})
        elif s.startswith("udp://"):
            dns_servers.append({"address": s.replace("udp://", ""), "detour": "direct"})
        else:
            dns_servers.append({"address": s, "detour": "direct"})
    if not dns_servers:
        dns_servers = [
            {"address": "1.1.1.1", "detour": "direct"},
            {"address": "8.8.8.8", "detour": "direct"},
        ]
    # local resolver for bootstrap
    dns_servers.append({"address": "local", "detour": "direct", "tag": "local"})
    cfg: dict[str, Any] = {
        "servers": dns_servers,
        "strategy": strategy or "prefer_ipv4",
        "independent_cache": True,
    }
    return cfg


def build_config(
    *,
    links: list[str],
    host: str,
    port: int,
    dns_servers: list[str],
    dns_strategy: str,
    mode: str,
    urltest_url: str,
    urltest_interval: str,
    log_level: str,
) -> dict[str, Any]:
    node_outbounds, tags = build_outbounds(links)
    if not tags:
        raise RuntimeError("no valid proxy nodes from PROXY_POOL")

    outbounds: list[dict[str, Any]] = list(node_outbounds)
    mode = (mode or "urltest").lower()
    if mode == "urltest" and len(tags) > 1:
        outbounds.insert(
            0,
            {
                "type": "urltest",
                "tag": "proxy",
                "outbounds": tags,
                "url": urltest_url,
                "interval": urltest_interval,
                "tolerance": 100,
            },
        )
        final = "proxy"
    elif mode == "round_robin" and len(tags) > 1:
        # loadbalance if available, else selector of all
        outbounds.insert(
            0,
            {
                "type": "selector",
                "tag": "proxy",
                "outbounds": tags,
                "default": tags[0],
            },
        )
        final = "proxy"
    else:
        # first only — alias first node as proxy
        outbounds.insert(
            0,
            {
                "type": "selector",
                "tag": "proxy",
                "outbounds": tags,
                "default": tags[0],
            },
        )
        final = "proxy"

    outbounds.append({"type": "direct", "tag": "direct"})
    outbounds.append({"type": "block", "tag": "block"})
    outbounds.append({"type": "dns", "tag": "dns-out"})

    config: dict[str, Any] = {
        "log": {"level": log_level or "warn", "timestamp": True},
        "dns": build_dns(dns_servers, dns_strategy),
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": host,
                "listen_port": port,
                "sniff": True,
                "sniff_override_destination": False,
            }
        ],
        "outbounds": outbounds,
        "route": {
            "rules": [
                {"protocol": "dns", "outbound": "dns-out"},
                # private / localhost direct
                {
                    "ip_is_private": True,
                    "outbound": "direct",
                },
            ],
            "final": final,
            "auto_detect_interface": True,
        },
        "experimental": {
            "cache_file": {
                "enabled": True,
                "path": "cache.db",
            }
        },
    }
    return config


def find_sing_box(work_dir: Path, explicit: str = "") -> str:
    if explicit and Path(explicit).is_file():
        return explicit
    import shutil

    found = shutil.which("sing-box")
    if found:
        return found
    cached = work_dir / "bin" / "sing-box"
    if cached.is_file():
        return str(cached)
    if _env_bool("PROXY_RELAY_AUTO_INSTALL", True) or _env_bool("PROXY_SERVICE_AUTO_INSTALL", True):
        log("installing sing-box from GitHub releases (SagerNet/sing-box)...")
        return install_sing_box(work_dir / "bin")
    raise RuntimeError("sing-box not found; set PROXY_SERVICE_SING_BOX or enable auto-install")


def write_env_file(path: Path, host: str, port: int) -> None:
    """Write env snippet for other processes (global proxy)."""
    # Prefer 127.0.0.1 for clients even if listen is 0.0.0.0
    client_host = "127.0.0.1" if host in ("0.0.0.0", "::", "[::]") else host
    proxy_url = f"http://{client_host}:{port}"
    socks_url = f"socks5h://{client_host}:{port}"
    content = f"""# generated by proxy_service — source this file or export in entrypoint
export HTTP_PROXY={proxy_url}
export HTTPS_PROXY={proxy_url}
export ALL_PROXY={socks_url}
export http_proxy={proxy_url}
export https_proxy={proxy_url}
export all_proxy={socks_url}
export NO_PROXY=127.0.0.1,localhost,::1
export no_proxy=127.0.0.1,localhost,::1
export PROXY_SERVICE_URL={proxy_url}
export SOLVER_GLOBAL_PROXY={proxy_url}
"""
    path.write_text(content, encoding="utf-8")
    log(f"wrote env file {path} → HTTP_PROXY={proxy_url}")


def run_service() -> int:
    host = _env("PROXY_SERVICE_HOST", "127.0.0.1") or "127.0.0.1"
    port = _env_int("PROXY_SERVICE_PORT", 7890)
    work_dir = Path(_env("PROXY_SERVICE_WORK_DIR", "/tmp/solver-proxy-service")).expanduser()
    work_dir.mkdir(parents=True, exist_ok=True)
    dns_raw = _env("PROXY_SERVICE_DNS", "1.1.1.1,8.8.8.8,8.8.4.4")
    dns_servers = [x.strip() for x in re.split(r"[,;\s]+", dns_raw) if x.strip()]
    dns_strategy = _env("PROXY_SERVICE_DNS_STRATEGY", "prefer_ipv4") or "prefer_ipv4"
    mode = _env("PROXY_SERVICE_MODE", "urltest") or "urltest"
    urltest_url = _env("PROXY_SERVICE_URLTEST_URL", "https://www.gstatic.com/generate_204")
    urltest_interval = _env("PROXY_SERVICE_URLTEST_INTERVAL", "3m") or "3m"
    log_level = _env("PROXY_SERVICE_LOG_LEVEL", "warn") or "warn"

    links = load_share_links()
    if not links:
        log("ERROR: PROXY_POOL empty — cannot start proxy service")
        return 2

    log(f"nodes={len(links)} listen={host}:{port} dns={dns_servers} mode={mode}")
    try:
        config = build_config(
            links=links,
            host=host,
            port=port,
            dns_servers=dns_servers,
            dns_strategy=dns_strategy,
            mode=mode,
            urltest_url=urltest_url,
            urltest_interval=urltest_interval,
            log_level=log_level,
        )
    except Exception as exc:
        log(f"config build failed: {exc}")
        return 2

    config_path = work_dir / "sing-box.json"
    log_path = work_dir / "sing-box.log"
    env_path = work_dir / "proxy.env"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"config → {config_path} ({len(config.get('outbounds', []))} outbounds)")

    binary = find_sing_box(work_dir, _env("PROXY_SERVICE_SING_BOX") or _env("PROXY_RELAY_SING_BOX_BIN"))
    log(f"sing-box binary: {binary}")

    # validate config if supported
    try:
        chk = subprocess.run(
            [binary, "check", "-c", str(config_path)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(work_dir),
        )
        if chk.returncode != 0:
            log(f"sing-box check warn: {chk.stderr or chk.stdout}")
    except Exception as exc:
        log(f"sing-box check skip: {exc}")

    write_env_file(env_path, host, port)

    log_file = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        [binary, "run", "-c", str(config_path), "-D", str(work_dir)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(work_dir),
        start_new_session=True,
    )
    log(f"started pid={proc.pid} mixed={host}:{port} log={log_path}")

    # wait until port listens
    deadline = time.time() + _env_int("PROXY_SERVICE_START_TIMEOUT", 20)
    ok = False
    while time.time() < deadline:
        if proc.poll() is not None:
            log(f"sing-box exited early code={proc.returncode}; tail log:")
            try:
                print(log_path.read_text(errors="replace")[-2000:], file=sys.stderr)
            except Exception:
                pass
            return 1
        try:
            import socket

            with socket.create_connection(
                ("127.0.0.1" if host in ("0.0.0.0", "::") else host, port),
                timeout=1,
            ):
                ok = True
                break
        except OSError:
            time.sleep(0.3)
    if not ok:
        log("timeout waiting for mixed inbound")
        proc.terminate()
        return 1

    log(f"ready HTTP/SOCKS mixed on {host}:{port} (global via HTTP_PROXY)")
    # print machine-readable line for entrypoint
    print(f"PROXY_SERVICE_URL=http://127.0.0.1:{port}", flush=True)
    print(f"PROXY_SERVICE_ENV={env_path}", flush=True)

    stop = False

    def _stop(*_a):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stop:
        if proc.poll() is not None:
            log(f"sing-box died code={proc.returncode}")
            return proc.returncode or 1
        time.sleep(1)

    log("stopping...")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_file.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="sing-box based global proxy service")
    p.add_argument("cmd", nargs="?", default="run", choices=["run", "print-config", "check"])
    args = p.parse_args(argv)

    if args.cmd == "print-config":
        links = load_share_links()
        cfg = build_config(
            links=links,
            host=_env("PROXY_SERVICE_HOST", "127.0.0.1"),
            port=_env_int("PROXY_SERVICE_PORT", 7890),
            dns_servers=[x for x in re.split(r"[,;\s]+", _env("PROXY_SERVICE_DNS", "1.1.1.1,8.8.8.8")) if x],
            dns_strategy=_env("PROXY_SERVICE_DNS_STRATEGY", "prefer_ipv4"),
            mode=_env("PROXY_SERVICE_MODE", "urltest"),
            urltest_url=_env("PROXY_SERVICE_URLTEST_URL", "https://www.gstatic.com/generate_204"),
            urltest_interval=_env("PROXY_SERVICE_URLTEST_INTERVAL", "3m"),
            log_level=_env("PROXY_SERVICE_LOG_LEVEL", "warn"),
        )
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0

    return run_service()


if __name__ == "__main__":
    raise SystemExit(main())
