#!/usr/bin/env python3
"""Agent pool proxy untuk RDP.

Menggantikan ProxyfierClient: membuka listener di 192.168.18.200:10000-10027,
mengambil assignment IP dari dashboard (API /api/pool), lalu meneruskan koneksi
VM ke upstream Loki (HTTP CONNECT + Basic auth).

Protokol masuk yang didukung (auto-deteksi per koneksi):
  - HTTP proxy (CONNECT dan request absolut http://...)
  - SOCKS5 tanpa auth / username-password (kredensial apa pun diterima)

Hanya memakai standard library supaya gampang dipasang di RDP.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import select
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

LOG = logging.getLogger("pool-agent")
HERE = Path(__file__).resolve().parent
BUFFER = 65536


# --------------------------------------------------------------------------
# Konfigurasi
# --------------------------------------------------------------------------
@dataclass
class Config:
    api_url: str
    token: str
    machine: str
    bind_ip: str
    start_port: int
    num_ports: int
    refresh_sec: int = 60
    report_sec: int = 30
    probe_sec: int = 300
    probe_target: str = "api.ipify.org:443"
    probe_workers: int = 6
    connect_timeout: int = 20
    report: bool = True
    cache_file: Path = HERE / "pool_cache.json"

    @property
    def report_url(self) -> str:
        if self.api_url.rstrip("/").endswith("/api/pool"):
            return self.api_url.rstrip("/")[: -len("/api/pool")] + "/api/pool/report"
        return self.api_url.rstrip("/") + "/report"

    @property
    def ports(self) -> list[int]:
        return list(range(self.start_port, self.start_port + self.num_ports))


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def load_config(path: Path | None) -> Config:
    raw: dict = {}
    candidates: list[Path] = []
    if path:
        candidates.append(path)
    elif _env("POOL_AGENT_CONFIG"):
        candidates.append(Path(_env("POOL_AGENT_CONFIG")))
    else:
        candidates.append(HERE / "config.json")
    for candidate in candidates:
        if candidate.exists():
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SystemExit(f"gagal membaca {candidate}: {exc}")
            break

    def pick(env_name: str, key: str, default):
        value = _env(env_name)
        if value:
            return value
        return raw.get(key, default)

    api_url = str(pick("POOL_API_URL", "api_url", "http://127.0.0.1:8080/api/pool"))
    token = str(pick("POOL_API_TOKEN", "token", ""))
    if not token:
        raise SystemExit(
            "token agent kosong. Isi config.json (field token) atau set POOL_API_TOKEN."
        )
    return Config(
        api_url=api_url,
        token=token,
        machine=str(pick("POOL_MACHINE", "machine", "RDP1")),
        bind_ip=str(pick("POOL_BIND_IP", "bind_ip", "0.0.0.0")),
        start_port=int(pick("POOL_START_PORT", "start_port", 10000)),
        num_ports=int(pick("POOL_NUM_PORTS", "num_ports", 28)),
        refresh_sec=int(pick("POOL_REFRESH_SEC", "refresh_sec", 60)),
        report_sec=int(pick("POOL_REPORT_SEC", "report_sec", 30)),
        probe_sec=int(pick("POOL_PROBE_SEC", "probe_sec", 300)),
        probe_target=str(pick("POOL_PROBE_TARGET", "probe_target", "api.ipify.org:443")),
        probe_workers=int(pick("POOL_PROBE_WORKERS", "probe_workers", 6)),
        connect_timeout=int(pick("POOL_CONNECT_TIMEOUT", "connect_timeout", 20)),
        report=str(pick("POOL_REPORT", "report", "1")).lower() not in {"0", "false", "no"},
        cache_file=Path(str(pick("POOL_CACHE_FILE", "cache_file", str(HERE / "pool_cache.json")))),
    )


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------
def _request(url: str, token: str, payload: dict | None = None, timeout: int = 20) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body) if body else {}


# --------------------------------------------------------------------------
# State per port
# --------------------------------------------------------------------------
@dataclass
class Assignment:
    port: int
    ip: str | None
    region: str | None
    username: str
    password: str
    host: str
    upstream_port: int
    expires_at: str | None = None
    group: str | None = None

    @classmethod
    def from_payload(cls, item: dict) -> "Assignment | None":
        upstream = item.get("upstream") or {}
        host = str(upstream.get("host") or "")
        try:
            upstream_port = int(upstream.get("port") or 0)
            port = int(item.get("port"))
        except (TypeError, ValueError):
            return None
        if not host or not upstream_port:
            return None
        return cls(
            port=port,
            ip=item.get("ip"),
            region=item.get("region"),
            username=str(upstream.get("username") or ""),
            password=str(upstream.get("password") or ""),
            host=host,
            upstream_port=upstream_port,
            expires_at=item.get("expires_at"),
            group=item.get("group"),
        )


@dataclass
class PortState:
    port: int
    assignment: Assignment | None = None
    active: int = 0
    total: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0
    last_ok: float | None = None
    latency_ms: int | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    error_source: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def note_ok(self, latency_ms: int | None = None) -> None:
        self.last_ok = time.time()
        if latency_ms is not None:
            self.latency_ms = latency_ms
        self.last_error = None
        self.error_source = None

    def note_error(self, exc: object, source: str = "client") -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"[:240]
        self.last_error_at = time.time()
        self.error_source = source

    def snapshot(self) -> dict:
        with self.lock:
            failed = bool(
                self.last_error
                and (self.last_ok is None or (self.last_error_at or 0) > self.last_ok)
            )
            return {
                "port": self.port,
                "ok": not failed and self.last_ok is not None,
                "latency_ms": self.latency_ms,
                "active_conns": self.active,
                "rx_bytes": self.rx_bytes,
                "tx_bytes": self.tx_bytes,
                "error": self.last_error if failed else None,
                "error_source": self.error_source if failed else None,
                "upstream_user": self.assignment.username if self.assignment else "",
            }


# --------------------------------------------------------------------------
# Socket helpers
# --------------------------------------------------------------------------
def _recv_until(sock: socket.socket, marker: bytes, limit: int = 65536) -> tuple[bytes, bytes]:
    data = b""
    while marker not in data and len(data) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(marker)
    return head, rest


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("koneksi ditutup")
        data += chunk
    return data


def _split_host_port(value: str, default_port: int = 80) -> tuple[str, int]:
    text = (value or "").strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":") or default_port
        return host, int(port)
    if text.count(":") == 1:
        host, port = text.split(":")
        return host, int(port or default_port)
    return text, default_port


def open_tunnel(assignment: Assignment, host: str, port: int, timeout: int) -> tuple[socket.socket, bytes]:
    """Buka CONNECT ke upstream Loki, kembalikan socket + sisa byte."""
    upstream = socket.create_connection((assignment.host, assignment.upstream_port), timeout=timeout)
    upstream.settimeout(timeout)
    auth = base64.b64encode(
        f"{assignment.username}:{assignment.password}".encode("utf-8")
    ).decode("ascii")
    request = (
        f"CONNECT {host}:{port} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Proxy-Authorization: Basic {auth}\r\n"
        f"User-Agent: pool-agent/1.0\r\n"
        f"\r\n"
    ).encode("ascii")
    upstream.sendall(request)
    head, rest = _recv_until(upstream, b"\r\n\r\n", 65536)
    first = head.split(b"\r\n", 1)[0] if head else b""
    if b" 200 " not in first:
        upstream.close()
        detail = first.decode("latin-1", "replace")[:120] or "koneksi ditutup upstream tanpa balasan"
        raise OSError(f"upstream menolak CONNECT: {detail}")
    upstream.settimeout(None)
    return upstream, rest


def relay(client: socket.socket, upstream: socket.socket, state: PortState) -> None:
    sockets = [client, upstream]
    idle = 0
    while True:
        try:
            ready, _, _ = select.select(sockets, [], [], 30)
        except (OSError, ValueError):
            return
        if not ready:
            idle += 1
            if idle >= 10:  # 5 menit tanpa trafik
                return
            continue
        idle = 0
        for sock in ready:
            try:
                chunk = sock.recv(BUFFER)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            other = upstream if sock is client else client
            try:
                other.sendall(chunk)
            except OSError:
                return
            with state.lock:
                if sock is client:
                    state.rx_bytes += len(chunk)
                else:
                    state.tx_bytes += len(chunk)


# --------------------------------------------------------------------------
# Handler HTTP proxy
# --------------------------------------------------------------------------
_HOP_HEADERS = {
    "proxy-connection",
    "proxy-authorization",
    "connection",
    "keep-alive",
    "upgrade",
}


def handle_http(client: socket.socket, state: PortState, config: Config, first: bytes) -> None:
    assignment = state.assignment
    if assignment is None:
        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 21\r\n\r\nno proxy assigned yet")
        return

    # `first` hanya hasil peek (tidak dikonsumsi), jadi jangan digabung lagi.
    head, rest = _recv_until(client, b"\r\n\r\n", 65536)
    raw = head
    try:
        text = raw.decode("latin-1")
        lines = text.split("\r\n")
        method, target, version = lines[0].split(" ", 2)
    except (ValueError, UnicodeDecodeError):
        client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        return

    if method.upper() == "CONNECT":
        host, port = _split_host_port(target, 443)
        started = time.time()
        upstream, leftover = open_tunnel(assignment, host, port, config.connect_timeout)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if leftover:
            client.sendall(leftover)
        state.note_ok(int((time.time() - started) * 1000))
        relay(client, upstream, state)
        upstream.close()
        return

    split = urlsplit(target)
    if split.scheme in {"http", "https"} and split.hostname:
        host = split.hostname
        port = split.port or (443 if split.scheme == "https" else 80)
        path = split.path or "/"
        if split.query:
            path += "?" + split.query
        first_line = f"{method} {path} {version}"
        headers = []
        has_host = False
        for line in lines[1:]:
            if not line:
                continue
            name = line.split(":", 1)[0].strip().lower()
            if name in _HOP_HEADERS:
                continue
            if name == "host":
                has_host = True
            headers.append(line)
        if not has_host:
            headers.append(f"Host: {host}")
        request = (first_line + "\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode("latin-1")
    else:
        host_header = next(
            (line.split(":", 1)[1].strip() for line in lines[1:] if line.lower().startswith("host:")),
            "",
        )
        if not host_header:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return
        host, port = _split_host_port(host_header, 80)
        request = raw

    started = time.time()
    upstream, leftover = open_tunnel(assignment, host, port, config.connect_timeout)
    state.note_ok(int((time.time() - started) * 1000))
    upstream.sendall(request)
    if rest:
        upstream.sendall(rest)
    if leftover:
        client.sendall(leftover)
    relay(client, upstream, state)
    upstream.close()


# --------------------------------------------------------------------------
# Handler SOCKS5
# --------------------------------------------------------------------------
def _socks5(client: socket.socket, state: PortState, config: Config) -> None:
    assignment = state.assignment
    if assignment is None:
        client.sendall(b"\x05\x01\x00")
        return
    version, nmethods = _recv_exact(client, 2)
    if version != 5:
        return
    methods = _recv_exact(client, nmethods)
    if 0x00 in methods:
        client.sendall(b"\x05\x00")
    elif 0x02 in methods:
        client.sendall(b"\x05\x02")
        _ver, ulen = _recv_exact(client, 2)
        _recv_exact(client, ulen)
        plen = _recv_exact(client, 1)[0]
        _recv_exact(client, plen)
        client.sendall(b"\x01\x00")
    else:
        client.sendall(b"\x05\xff")
        return

    _ver, cmd, _rsv, atyp = _recv_exact(client, 4)
    if cmd != 0x01:
        client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    if atyp == 0x01:
        host = socket.inet_ntoa(_recv_exact(client, 4))
    elif atyp == 0x03:
        length = _recv_exact(client, 1)[0]
        host = _recv_exact(client, length).decode("idna")
    elif atyp == 0x04:
        host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
    else:
        client.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    port = struct.unpack(">H", _recv_exact(client, 2))[0]

    try:
        started = time.time()
        upstream, leftover = open_tunnel(assignment, host, port, config.connect_timeout)
    except OSError:
        client.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
    state.note_ok(int((time.time() - started) * 1000))
    if leftover:
        client.sendall(leftover)
    relay(client, upstream, state)
    upstream.close()


def handle_client(client: socket.socket, state: PortState, config: Config) -> None:
    with state.lock:
        state.active += 1
        state.total += 1
    try:
        client.settimeout(20)
        first = client.recv(1, socket.MSG_PEEK)
        if not first:
            return
        client.settimeout(None)
        if first == b"\x05":
            _socks5(client, state, config)
        else:
            handle_http(client, state, config, first)
    except OSError as exc:
        state.note_error(exc, "client")
    except Exception as exc:  # noqa: BLE001
        state.note_error(exc)
        LOG.debug("koneksi port %s error: %s", state.port, exc)
    finally:
        with state.lock:
            state.active = max(0, state.active - 1)
        try:
            client.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Listener + refresh + report
# --------------------------------------------------------------------------
_running = threading.Event()
_probe_now = threading.Event()
_states: dict[int, PortState] = {}


def refresh_once(config: Config) -> dict[int, Assignment]:
    url = f"{config.api_url}?machine={urllib.parse.quote(config.machine)}"
    payload = _request(url, config.token, timeout=20)
    assignments: dict[int, Assignment] = {}
    for item in payload.get("ports") or []:
        assignment = Assignment.from_payload(item)
        if assignment is not None:
            assignments[assignment.port] = assignment
    try:
        config.cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return assignments


def _apply_assignments(assignments: dict[int, Assignment]) -> bool:
    changed = False
    for port, state in _states.items():
        new = assignments.get(port)
        old = state.assignment
        if new is None:
            if old is not None:
                LOG.info("port %s dilepas (tidak ada assignment)", port)
                state.assignment = None
                changed = True
            continue
        if old is None or (old.username, old.password, old.host, old.upstream_port) != (
            new.username,
            new.password,
            new.host,
            new.upstream_port,
        ):
            LOG.info(
                "port %s -> %s (%s)",
                port,
                new.ip or "?",
                new.region or "-",
            )
            state.latency_ms = None
            changed = True
        state.assignment = new
    return changed


def refresh_loop(config: Config) -> None:
    while not _running.is_set():
        try:
            assignments = refresh_once(config)
            if _apply_assignments(assignments):
                _probe_now.set()
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            LOG.warning("gagal ambil assignment: %s", exc)
        _running.wait(config.refresh_sec)


# --------------------------------------------------------------------------
# Probe kesehatan sesi
# --------------------------------------------------------------------------
def _probe_one(config: Config, assignment: Assignment) -> tuple[bool, int | None, str | None]:
    host, port_text = _split_host_port(config.probe_target, 443)
    started = time.time()
    try:
        upstream, _leftover = open_tunnel(assignment, host, port_text, config.connect_timeout)
        upstream.close()
        return True, int((time.time() - started) * 1000), None
    except OSError as exc:
        return False, None, f"{type(exc).__name__}: {exc}"[:240]


def probe_all(config: Config) -> None:
    targets: list[tuple[PortState, Assignment]] = []
    for state in list(_states.values()):
        assignment = state.assignment
        if assignment is not None:
            targets.append((state, assignment))
    if not targets:
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed

    workers = max(1, min(config.probe_workers, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_probe_one, config, assignment): (state, assignment)
            for state, assignment in targets
        }
        for future in as_completed(futures):
            state, assignment = futures[future]
            ok, latency, error = future.result()
            with state.lock:
                # Abaikan hasil probe kalau assignment sudah berganti.
                current = state.assignment
                if current is None or current.username != assignment.username:
                    continue
                if ok:
                    state.note_ok(latency)
                else:
                    state.note_error(error or "probe gagal", "probe")
                    LOG.info("probe port %s gagal: %s", state.port, error)


def probe_loop(config: Config) -> None:
    while not _running.is_set():
        wait = config.probe_sec if config.probe_sec > 0 else 60
        _probe_now.wait(wait)
        if _running.is_set():
            return
        _probe_now.clear()
        probe_all(config)


def report_loop(config: Config) -> None:
    while not _running.is_set():
        _running.wait(config.report_sec)
        if _running.is_set():
            return
        payload = {
            "machine": config.machine,
            "version": 1,
            "ports": [state.snapshot() for state in _states.values()],
        }
        try:
            _request(config.report_url, config.token, payload, timeout=15)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOG.debug("gagal kirim report: %s", exc)


def run(config: Config) -> int:
    LOG.info(
        "agent %s: bind %s ports %s-%s, refresh %ss",
        config.machine,
        config.bind_ip,
        config.start_port,
        config.start_port + config.num_ports - 1,
        config.refresh_sec,
    )
    try:
        assignments = refresh_once(config)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("API belum bisa dihubungi (%s); coba cache lokal", exc)
        assignments = {}
        if config.cache_file.exists():
            try:
                cached = json.loads(config.cache_file.read_text(encoding="utf-8"))
                for item in cached.get("ports") or []:
                    assignment = Assignment.from_payload(item)
                    if assignment is not None:
                        assignments[assignment.port] = assignment
                LOG.info("memakai %s assignment dari cache", len(assignments))
            except (OSError, ValueError):
                pass

    listeners: list[socket.socket] = []
    for port in config.ports:
        state = PortState(port=port, assignment=assignments.get(port))
        _states[port] = state
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        else:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((config.bind_ip, port))
            server.listen(256)
        except OSError as exc:
            LOG.error("port %s tidak bisa dipakai: %s", port, exc)
            server.close()
            continue
        listeners.append(server)
        threading.Thread(target=_accept_loop, args=(server, state, config), daemon=True).start()

    if not listeners:
        LOG.error("tidak ada port yang berhasil dibuka")
        return 1
    LOG.info("%s port listening; assignment aktif: %s", len(listeners), sum(1 for s in _states.values() if s.assignment))

    threading.Thread(target=refresh_loop, args=(config,), daemon=True).start()
    if config.report:
        threading.Thread(target=report_loop, args=(config,), daemon=True).start()
    if config.probe_sec > 0:
        threading.Thread(target=probe_loop, args=(config,), daemon=True).start()
        _probe_now.set()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        LOG.info("berhenti")
    finally:
        _running.set()
        for server in listeners:
            try:
                server.close()
            except OSError:
                pass
    return 0


def _accept_loop(server: socket.socket, state: PortState, config: Config) -> None:
    while not _running.is_set():
        try:
            client, _addr = server.accept()
        except OSError:
            return
        threading.Thread(
            target=handle_client, args=(client, state, config), daemon=True
        ).start()


# --------------------------------------------------------------------------
# Selftest
# --------------------------------------------------------------------------
def selftest(config: Config, ports: list[int] | None = None, target: str = "api.ipify.org:443") -> int:
    assignments = refresh_once(config)
    selected = ports or sorted(assignments)
    if not selected:
        print("Tidak ada assignment dari API.")
        return 1
    host, port_text = _split_host_port(target, 443)
    failures = 0
    for number in selected:
        assignment = assignments.get(number)
        if assignment is None:
            print(f"{number}: tidak ada assignment")
            failures += 1
            continue
        started = time.time()
        try:
            upstream, _ = open_tunnel(assignment, host, port_text, config.connect_timeout)
            upstream.close()
            print(f"{number}: OK  {assignment.ip or '?'}  {assignment.region or '-'}  "
                  f"{int((time.time() - started) * 1000)} ms")
        except OSError as exc:
            failures += 1
            print(f"{number}: GAGAL  {assignment.ip or '?'}  {assignment.region or '-'}  {exc}")
    print(f"\n{len(selected) - failures}/{len(selected)} port bisa CONNECT ke {target}")
    return 0 if failures == 0 else 2


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Pool proxy agent (pengganti ProxyfierClient)")
    parser.add_argument("--config", type=Path, default=None, help="file config.json")
    parser.add_argument("--selftest", action="store_true", help="tes CONNECT tiap port lalu keluar")
    parser.add_argument("--ports", default="", help="batasi port selftest, mis. 10000,10001")
    parser.add_argument("--target", default="api.ipify.org:443", help="target CONNECT untuk selftest")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config(args.config)
    if args.selftest:
        ports = [int(item) for item in args.ports.split(",") if item.strip()]
        return selftest(config, ports or None, args.target)
    return run(config)


if __name__ == "__main__":
    sys.exit(main())
