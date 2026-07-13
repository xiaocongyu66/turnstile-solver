from __future__ import annotations

import atexit
import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import tarfile
import threading
import time
from typing import Iterable
from urllib.parse import parse_qsl, unquote, urlparse
from urllib.request import Request, urlopen


USER_AGENT = "grok-free-register/proxy-relay"


@dataclass(frozen=True)
class BuiltinProxyRelayConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    proxy_scheme: str = "http"
    work_dir: str = "logs/proxy-relay"
    sing_box_bin: str = ""
    auto_install: bool = True
    start_port: int = 19080
    max_nodes: int = 48
    start_timeout: int = 8

    @property
    def work_path(self):
        return Path(self.work_dir).expanduser()


@dataclass
class RelayNode:
    link: str
    local_port: int
    kernel: str
    proxy: str
    config_path: Path
    log_path: Path
    process: subprocess.Popen | None = None

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def to_dict(self):
        return {
            "link": self.link,
            "share_link": self.link,
            "local_port": self.local_port,
            "localPort": self.local_port,
            "kernel": self.kernel,
            "proxy": self.proxy,
            "pid": self.process.pid if self.process else None,
        }


class BuiltinProxyRelay:
    def __init__(self, config: BuiltinProxyRelayConfig, logger=None):
        self.config = config
        self.logger = logger or (lambda _msg: None)
        self._nodes: dict[str, RelayNode] = {}
        self._lock = threading.Lock()
        self._sing_box_bin: str | None = None
        atexit.register(self.stop_all)

    def state(self):
        with self._lock:
            self._drop_dead_nodes_locked()
            return {"ok": True, "nodes": [node.to_dict() for node in self._nodes.values()]}

    def import_link(self, share_link: str, *, kernel: str = "sing-box", local_port: int | str | None = None):
        if not self.config.enabled:
            raise RuntimeError("built-in proxy relay is disabled")
        if (kernel or "sing-box").strip().lower().replace("_", "-") not in {"auto", "sing-box", "singbox"}:
            raise RuntimeError("built-in proxy relay currently supports sing-box only")
        link = (share_link or "").strip()
        if not link:
            raise RuntimeError("empty share link")
        with self._lock:
            self._drop_dead_nodes_locked()
            existing = self._nodes.get(link)
            if existing and existing.alive():
                return existing.to_dict()
            if len(self._nodes) >= max(1, self.config.max_nodes):
                raise RuntimeError(f"built-in proxy relay reached max nodes: {self.config.max_nodes}")

            outbound = share_link_to_sing_box_outbound(link)
            outbound["tag"] = "proxy"
            port = int(local_port) if str(local_port or "").strip() else self._allocate_port_locked()
            proxy = _proxy_url(self.config.proxy_scheme, self.config.host, port)
            digest = hashlib.sha256(link.encode()).hexdigest()[:16]
            node_dir = self.config.work_path / "nodes" / digest
            node_dir.mkdir(parents=True, exist_ok=True)
            config_path = node_dir / "sing-box.json"
            log_path = node_dir / "sing-box.log"
            config_path.write_text(
                json.dumps(self._sing_box_config(port, outbound), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            process = self._start_sing_box(config_path, log_path)
            node = RelayNode(
                link=link,
                local_port=port,
                kernel="sing-box",
                proxy=proxy,
                config_path=config_path,
                log_path=log_path,
                process=process,
            )
            self._nodes[link] = node
            return node.to_dict()

    def prune(self, active_proxies: Iterable[str]):
        active = {str(item).strip() for item in active_proxies if str(item).strip()}
        with self._lock:
            for link, node in list(self._nodes.items()):
                if node.proxy not in active:
                    self._stop_node(node)
                    self._nodes.pop(link, None)

    def stop_link(self, share_link: str):
        link = (share_link or "").strip()
        if not link:
            return
        with self._lock:
            node = self._nodes.pop(link, None)
            if node:
                self._stop_node(node)

    def stop_all(self):
        with self._lock:
            for link, node in list(self._nodes.items()):
                self._stop_node(node)
                self._nodes.pop(link, None)

    def runtime_hint(self):
        if self._sing_box_bin:
            return f"sing-box={self._sing_box_bin}"
        configured = (self.config.sing_box_bin or "").strip()
        if configured:
            return f"sing-box={configured}"
        found = shutil.which("sing-box")
        if found:
            return f"sing-box={found}"
        if self.config.auto_install:
            return f"sing-box auto-install to {self.config.work_path / 'bin'}"
        return "sing-box not found; install sing-box or set PROXY_RELAY_AUTO_INSTALL=1"

    def _sing_box_config(self, port: int, outbound: dict):
        return {
            "log": {"level": "warn", "timestamp": True},
            "inbounds": [
                {
                    "type": "mixed",
                    "tag": "mixed-in",
                    "listen": self.config.host,
                    "listen_port": port,
                }
            ],
            "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
            "route": {"final": "proxy"},
        }

    def _start_sing_box(self, config_path: Path, log_path: Path):
        binary = self._ensure_sing_box_binary()
        log_file = log_path.open("wb")
        try:
            process = subprocess.Popen(
                [binary, "run", "-c", str(config_path)],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            log_file.close()
        port = int(json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["listen_port"])
        if not _wait_for_port(self.config.host, port, process, self.config.start_timeout):
            tail = _read_tail(log_path)
            self._stop_process(process)
            raise RuntimeError(f"sing-box failed to listen on {self.config.host}:{port}: {tail}")
        return process

    def _ensure_sing_box_binary(self):
        if self._sing_box_bin:
            return self._sing_box_bin
        configured = (self.config.sing_box_bin or "").strip()
        if configured:
            path = Path(configured).expanduser()
            if path.exists():
                self._sing_box_bin = str(path)
                return self._sing_box_bin
            found = shutil.which(configured)
            if found:
                self._sing_box_bin = found
                return found
        found = shutil.which("sing-box")
        if found:
            self._sing_box_bin = found
            return found
        cached = self.config.work_path / "bin" / "sing-box"
        if cached.exists():
            self._sing_box_bin = str(cached)
            return self._sing_box_bin
        if self.config.auto_install:
            self._sing_box_bin = install_sing_box(self.config.work_path / "bin")
            return self._sing_box_bin
        raise RuntimeError("sing-box not found")

    def _allocate_port_locked(self):
        used = {node.local_port for node in self._nodes.values()}
        port = max(1, int(self.config.start_port))
        # Prefer a wider scan window; skip ports that still have a TIME_WAIT or
        # orphan listener from a previous register process.
        for candidate in range(port, min(65535, port + 5000)):
            if candidate in used:
                continue
            if not _port_available(self.config.host, candidate):
                continue
            # Double-check bind succeeds (catches races with other processes).
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((self.config.host, candidate))
            except OSError:
                continue
            return candidate
        raise RuntimeError("no free local relay port")

    def _drop_dead_nodes_locked(self):
        for link, node in list(self._nodes.items()):
            if not node.alive():
                self._nodes.pop(link, None)

    def _stop_node(self, node: RelayNode):
        if node.process:
            self._stop_process(node.process)

    @staticmethod
    def _stop_process(process):
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def share_link_to_sing_box_outbound(link: str):
    parsed = urlparse((link or "").strip())
    scheme = parsed.scheme.lower()
    if scheme == "vmess":
        return _vmess_outbound(link)
    if scheme == "vless":
        return _vless_outbound(parsed)
    if scheme == "trojan":
        return _trojan_outbound(parsed)
    if scheme == "ss":
        return _shadowsocks_outbound(link)
    if scheme in {"hy2", "hysteria2"}:
        return _hysteria2_outbound(parsed)
    if scheme == "tuic":
        return _tuic_outbound(parsed)
    if scheme == "anytls":
        return _anytls_outbound(parsed)
    # Authenticated SOCKS5 cannot be used directly by Chromium/patchright;
    # relay through sing-box mixed inbound as local HTTP.
    if scheme in {"socks", "socks5", "socks5h"}:
        return _socks_outbound(parsed)
    raise RuntimeError(f"unsupported share link scheme: {scheme or 'unknown'}")


def _socks_outbound(parsed):
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        raise RuntimeError("socks proxy requires host and port")
    outbound = {
        "type": "socks",
        "server": host,
        "server_port": int(port),
        "version": "5",
    }
    if parsed.username:
        outbound["username"] = unquote(parsed.username)
    if parsed.password:
        outbound["password"] = unquote(parsed.password)
    return outbound


def install_sing_box(target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    goos, goarch = _sing_box_platform()
    latest = _github_json("https://api.github.com/repos/SagerNet/sing-box/releases/latest")
    version = str(latest.get("tag_name") or "").lstrip("v")
    if not version:
        raise RuntimeError("unable to resolve latest sing-box version")
    asset_pattern = re.compile(
        rf"sing-box-{re.escape(version)}-{re.escape(goos)}-{re.escape(goarch)}(?:v\d+)?\.tar\.gz$"
    )
    assets = latest.get("assets") if isinstance(latest, dict) else []
    asset = next(
        (
            item
            for item in assets
            if isinstance(item, dict) and asset_pattern.search(str(item.get("name") or ""))
        ),
        None,
    )
    if not asset:
        raise RuntimeError(f"no sing-box release asset for {goos}/{goarch}")
    download_url = asset.get("browser_download_url")
    if not download_url:
        raise RuntimeError("sing-box release asset has no download URL")
    archive_path = target_dir / str(asset["name"])
    _download_file(str(download_url), archive_path)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = [member for member in archive.getmembers() if Path(member.name).name == "sing-box"]
        if not members:
            raise RuntimeError("sing-box binary not found in release archive")
        member = members[0]
        member.name = "sing-box"
        archive.extract(member, target_dir)
    binary = target_dir / "sing-box"
    binary.chmod(binary.stat().st_mode | 0o755)
    return str(binary)


def _vmess_outbound(link: str):
    body = link.split("://", 1)[1].split("#", 1)[0]
    data = json.loads(_b64decode_text(body))
    outbound = {
        "type": "vmess",
        "server": str(data.get("add") or ""),
        "server_port": _port(data.get("port")),
        "uuid": str(data.get("id") or ""),
        "security": str(data.get("scy") or "auto"),
        "alter_id": int(str(data.get("aid") or "0") or 0),
    }
    tls = _tls_options(
        security=str(data.get("tls") or ""),
        server_name=str(data.get("sni") or data.get("host") or ""),
        insecure=_truthy(data.get("allowInsecure")),
        alpn=str(data.get("alpn") or ""),
        fingerprint=str(data.get("fp") or ""),
    )
    if tls:
        outbound["tls"] = tls
    transport = _transport_options(
        str(data.get("net") or "tcp"),
        path=str(data.get("path") or ""),
        host=str(data.get("host") or ""),
        service_name=str(data.get("path") or data.get("serviceName") or ""),
    )
    if transport:
        outbound["transport"] = transport
    _require_fields(outbound, "server", "server_port", "uuid")
    return outbound


def _vless_outbound(parsed):
    query = _query(parsed)
    outbound = {
        "type": "vless",
        "server": _host(parsed),
        "server_port": _port(parsed.port),
        "uuid": unquote(parsed.username or ""),
    }
    flow = _normalize_vless_flow(query.get("flow") or "")
    if flow:
        outbound["flow"] = flow
    tls = _tls_from_query(query)
    if tls:
        outbound["tls"] = tls
    transport = _transport_from_query(query)
    if transport:
        outbound["transport"] = transport
    _require_fields(outbound, "server", "server_port", "uuid")
    return outbound


def _trojan_outbound(parsed):
    query = _query(parsed)
    outbound = {
        "type": "trojan",
        "server": _host(parsed),
        "server_port": _port(parsed.port),
        "password": unquote(parsed.username or ""),
    }
    tls = _tls_from_query(query, default_enabled=True)
    if tls:
        outbound["tls"] = tls
    transport = _transport_from_query(query)
    if transport:
        outbound["transport"] = transport
    _require_fields(outbound, "server", "server_port", "password")
    return outbound


def _shadowsocks_outbound(link: str):
    parsed = _parse_shadowsocks_link(link)
    outbound = {
        "type": "shadowsocks",
        "server": parsed["server"],
        "server_port": parsed["server_port"],
        "method": parsed["method"],
        "password": parsed["password"],
    }
    _require_fields(outbound, "server", "server_port", "method", "password")
    return outbound


def _hysteria2_outbound(parsed):
    query = _query(parsed)
    outbound = {
        "type": "hysteria2",
        "server": _host(parsed),
        "server_port": _port(parsed.port),
        "password": unquote(parsed.username or ""),
    }
    tls = _tls_from_query(query, default_enabled=True)
    if tls:
        outbound["tls"] = tls
    obfs_type = query.get("obfs")
    obfs_password = query.get("obfs-password") or query.get("obfs_password")
    if obfs_type and obfs_password:
        outbound["obfs"] = {"type": obfs_type, "password": obfs_password}
    _require_fields(outbound, "server", "server_port", "password")
    return outbound


def _tuic_outbound(parsed):
    query = _query(parsed)
    outbound = {
        "type": "tuic",
        "server": _host(parsed),
        "server_port": _port(parsed.port),
        "uuid": unquote(parsed.username or ""),
        "password": unquote(parsed.password or query.get("password") or ""),
    }
    if query.get("congestion_control"):
        outbound["congestion_control"] = query["congestion_control"]
    tls = _tls_from_query(query, default_enabled=True)
    if tls:
        outbound["tls"] = tls
    _require_fields(outbound, "server", "server_port", "uuid", "password")
    return outbound


def _anytls_outbound(parsed):
    query = _query(parsed)
    outbound = {
        "type": "anytls",
        "server": _host(parsed),
        "server_port": _port(parsed.port),
        "password": unquote(parsed.username or ""),
    }
    tls = _tls_from_query(query, default_enabled=True)
    if tls:
        outbound["tls"] = tls
    _require_fields(outbound, "server", "server_port", "password")
    return outbound


def _tls_from_query(query, default_enabled=False):
    security = (query.get("security") or query.get("tls") or "").lower()
    enabled = default_enabled or security in {"tls", "reality"} or bool(query.get("sni") or query.get("serverName"))
    if security == "none" and not default_enabled:
        enabled = False
    if not enabled:
        return None
    if default_enabled and not security:
        security = "tls"
    return _tls_options(
        security=security,
        server_name=query.get("sni") or query.get("serverName") or query.get("peer") or "",
        insecure=_truthy(query.get("insecure") or query.get("allowInsecure") or query.get("skip-cert-verify")),
        alpn=query.get("alpn") or "",
        fingerprint=query.get("fp") or query.get("fingerprint") or "",
        public_key=query.get("pbk") or query.get("publicKey") or "",
        short_id=query.get("sid") or query.get("shortId") or "",
    )


def _tls_options(
    *,
    security="",
    server_name="",
    insecure=False,
    alpn="",
    fingerprint="",
    public_key="",
    short_id="",
):
    security = (security or "").lower()
    if security not in {"tls", "reality"} and not server_name and not insecure and not alpn:
        return None
    tls = {"enabled": True}
    if server_name:
        tls["server_name"] = server_name
    if insecure:
        tls["insecure"] = True
    if alpn:
        tls["alpn"] = [item for item in re.split(r"[,|]", alpn) if item]
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    if security == "reality" or public_key:
        reality = {"enabled": True}
        if public_key:
            reality["public_key"] = public_key
        if short_id:
            reality["short_id"] = short_id
        tls["reality"] = reality
    return tls


def _transport_from_query(query):
    network = query.get("type") or query.get("net") or "tcp"
    return _transport_options(
        network,
        path=query.get("path") or "",
        host=query.get("host") or "",
        service_name=query.get("serviceName") or query.get("service_name") or "",
    )


def _transport_options(network, *, path="", host="", service_name=""):
    network = (network or "tcp").lower()
    if network in {"tcp", "raw", ""}:
        return None
    if network in {"ws", "websocket"}:
        transport = {"type": "ws"}
        if path:
            transport["path"] = path
        if host:
            transport["headers"] = {"Host": host}
        return transport
    if network == "grpc":
        transport = {"type": "grpc"}
        if service_name:
            transport["service_name"] = service_name.lstrip("/")
        return transport
    if network in {"http", "h2"}:
        transport = {"type": "http"}
        if path:
            transport["path"] = path
        if host:
            transport["host"] = [host]
        return transport
    if network in {"httpupgrade", "http-upgrade"}:
        transport = {"type": "httpupgrade"}
        if path:
            transport["path"] = path
        if host:
            transport["host"] = host
        return transport
    return {"type": network}


def _normalize_vless_flow(flow: str):
    value = (flow or "").strip()
    if value.startswith("xtls-rprx-vision"):
        return "xtls-rprx-vision"
    return value


def _parse_shadowsocks_link(link: str):
    body = link.split("://", 1)[1].split("#", 1)[0]
    body = body.split("?", 1)[0]
    if "@" not in body:
        decoded = _b64decode_text(body)
        return _parse_shadowsocks_decoded(decoded)
    credentials, server_part = body.rsplit("@", 1)
    credentials = unquote(credentials)
    if ":" not in credentials:
        credentials = _b64decode_text(credentials)
    method, password = credentials.split(":", 1)
    server_url = urlparse("ss://" + server_part)
    return {
        "method": method,
        "password": password,
        "server": _host(server_url),
        "server_port": _port(server_url.port),
    }


def _parse_shadowsocks_decoded(decoded: str):
    parsed = urlparse("ss://" + decoded)
    userinfo = decoded.rsplit("@", 1)[0]
    method, password = unquote(userinfo).split(":", 1)
    return {
        "method": method,
        "password": password,
        "server": _host(parsed),
        "server_port": _port(parsed.port),
    }


def _query(parsed):
    return {key: value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}


def _host(parsed):
    host = parsed.hostname or ""
    return unquote(host.strip("[]"))


def _port(value):
    try:
        port = int(value)
    except Exception as exc:
        raise RuntimeError("missing proxy server port") from exc
    if not (0 < port < 65536):
        raise RuntimeError(f"invalid proxy server port: {port}")
    return port


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _require_fields(document, *fields):
    missing = [field for field in fields if document.get(field) in (None, "")]
    if missing:
        raise RuntimeError(f"missing required share link fields: {', '.join(missing)}")


def _b64decode_text(value: str):
    compact = re.sub(r"\s+", "", unquote(value or ""))
    padding = "=" * ((4 - len(compact) % 4) % 4)
    errors = []
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder((compact + padding).encode()).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            errors.append(exc)
    raise RuntimeError("invalid base64 share link payload") from errors[-1]


def _proxy_url(scheme, host, port):
    scheme = (scheme or "http").strip().lower()
    if scheme == "auto":
        scheme = "http"
    if scheme not in {"http", "socks4", "socks5", "socks5h"}:
        scheme = "http"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{int(port)}"


def _port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, int(port))) != 0


def _wait_for_port(host, port, process, timeout):
    deadline = time.time() + max(1, int(timeout))
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex((host, int(port))) == 0:
                return True
        time.sleep(0.1)
    return False


def _read_tail(path: Path, limit=2000):
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "ignore").strip()


def _github_json(url):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _download_file(url, path: Path):
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=120) as response:
        path.write_bytes(response.read())


def _sing_box_platform():
    system = platform.system().lower()
    if system == "darwin":
        goos = "darwin"
    elif system == "linux":
        goos = "linux"
    elif system == "windows":
        goos = "windows"
    else:
        raise RuntimeError(f"unsupported OS for sing-box auto-install: {system}")
    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armv7",
        "armv6l": "armv6",
        "i386": "386",
        "i686": "386",
    }
    goarch = arch_map.get(machine)
    if not goarch:
        raise RuntimeError(f"unsupported arch for sing-box auto-install: {machine}")
    return goos, goarch
