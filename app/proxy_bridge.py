"""Jembatan proxy lokal untuk browser.

Chromium tidak mendukung SOCKS5 dengan autentikasi. Modul ini membuat HTTP proxy
kecil di 127.0.0.1 yang meneruskan koneksi ke proxy upstream (SOCKS5 atau HTTP)
lengkap dengan username/password. Browser cukup memakai proxy lokal tanpa auth.

Mendukung dua bentuk permintaan dari browser:
- CONNECT host:port  -> tunnel dua arah (HTTPS)
- GET http://host/.. -> diteruskan sebagai HTTP biasa (tanpa TLS)
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import select
import socket
import struct
import threading
from urllib.parse import urlsplit

log = logging.getLogger("earnapp-monitor.bridge")

_BUFFER = 65536


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("upstream menutup koneksi")
        data += chunk
    return data


def _read_headers(sock: socket.socket) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data and len(data) < _BUFFER:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _split_host_port(target: str) -> tuple[str, int]:
    host, _, port_text = target.rpartition(":")
    if not host or not port_text.isdigit():
        raise OSError(f"target tidak valid: {target}")
    return host, int(port_text)


def _socks5_handshake(
    sock: socket.socket, host: str, port: int, user: str, password: str
) -> None:
    methods = b"\x00\x02" if user else b"\x00"
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
    version, method = _recv_exact(sock, 2)
    if version != 5:
        raise OSError("respons SOCKS5 tidak valid")
    if method == 2:
        username = user.encode("utf-8")
        secret = password.encode("utf-8")
        sock.sendall(
            b"\x01" + bytes([len(username)]) + username + bytes([len(secret)]) + secret
        )
        _, status = _recv_exact(sock, 2)
        if status != 0:
            raise OSError("autentikasi SOCKS5 ditolak")
    elif method != 0:
        raise OSError("metode autentikasi SOCKS5 tidak didukung")

    try:
        address = ipaddress.ip_address(host)
        if address.version == 4:
            target = b"\x01" + address.packed
        else:
            target = b"\x04" + address.packed
    except ValueError:
        encoded = host.encode("idna")
        target = b"\x03" + bytes([len(encoded)]) + encoded

    sock.sendall(b"\x05\x01\x00" + target + struct.pack("!H", port))
    version, reply, _, atyp = _recv_exact(sock, 4)
    if version != 5:
        raise OSError("respons SOCKS5 tidak valid")
    if reply != 0:
        raise OSError(f"SOCKS5 menolak koneksi (kode {reply})")
    if atyp == 1:
        _recv_exact(sock, 4)
    elif atyp == 3:
        length = _recv_exact(sock, 1)[0]
        _recv_exact(sock, length)
    elif atyp == 4:
        _recv_exact(sock, 16)
    _recv_exact(sock, 2)


def _proxy_auth_header(user: str, password: str) -> str:
    if not user:
        return ""
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Proxy-Authorization: Basic {token}\r\n"


def _http_connect_handshake(
    sock: socket.socket, host: str, port: int, user: str, password: str
) -> None:
    request = (
        f"CONNECT {host}:{port} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"{_proxy_auth_header(user, password)}"
        "\r\n"
    ).encode("latin1")
    sock.sendall(request)
    response = b""
    while b"\r\n\r\n" not in response and len(response) < _BUFFER:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    status_line = response.split(b"\r\n", 1)[0].decode("latin1", "replace")
    if " 200 " not in f"{status_line} ":
        raise OSError(f"proxy HTTP menolak CONNECT: {status_line[:120]}")


def _header_name(raw: bytes) -> bytes:
    return raw.split(b":", 1)[0].strip().lower()


class ProxyBridge:
    """HTTP proxy lokal -> proxy upstream (SOCKS5/HTTP) dengan auth."""

    def __init__(self, upstream: dict, scheme: str) -> None:
        self.host = upstream["host"]
        self.port = int(upstream["port"])
        self.user = upstream.get("user") or ""
        self.password = upstream.get("password") or ""
        self.scheme = "socks5" if scheme.startswith("socks") else "http"
        self.connections = 0
        self.errors = 0
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(64)
        self._server.settimeout(1.0)
        self.listen_port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> "ProxyBridge":
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections += 1
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _connect_upstream(self, host: str, port: int) -> socket.socket:
        upstream = socket.create_connection((self.host, self.port), timeout=30)
        upstream.settimeout(30)
        if self.scheme == "socks5":
            _socks5_handshake(upstream, host, port, self.user, self.password)
        else:
            _http_connect_handshake(upstream, host, port, self.user, self.password)
        return upstream

    def _handle(self, client: socket.socket) -> None:
        upstream_sock: socket.socket | None = None
        try:
            client.settimeout(30)
            head, rest = _read_headers(client)
            if not head:
                return
            request_line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
            parts = request_line.split(" ")
            if len(parts) < 3:
                raise OSError(f"request tidak valid: {request_line[:80]}")
            method = parts[0].upper()

            if method == "CONNECT":
                host, port = _split_host_port(parts[1])
                upstream_sock = self._connect_upstream(host, port)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream_sock.sendall(rest)
            else:
                upstream_sock = self._forward_plain(client, head, rest, method, parts[1])

            self._relay(client, upstream_sock)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            log.debug("bridge error: %s", exc)
            try:
                client.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
        finally:
            for sock in (client, upstream_sock):
                if sock is None:
                    continue
                try:
                    sock.close()
                except OSError:
                    pass

    def _forward_plain(
        self, client: socket.socket, head: bytes, rest: bytes, method: str, target: str
    ) -> socket.socket:
        """Teruskan permintaan HTTP biasa (non-CONNECT) ke upstream."""
        url = urlsplit(target)
        if url.scheme != "http" or not url.hostname:
            raise OSError(f"hanya proxy HTTP biasa yang didukung: {target[:80]}")
        host = url.hostname
        port = url.port or 80
        path = url.path or "/"
        if url.query:
            path = f"{path}?{url.query}"

        headers = head.split(b"\r\n")[1:]
        headers = [
            line
            for line in headers
            if _header_name(line) not in {b"proxy-authorization", b"proxy-connection", b"connection"}
        ]

        if self.scheme == "http":
            # HTTP proxy upstream: kirim absolute-form + auth, tanpa tunnel.
            upstream = socket.create_connection((self.host, self.port), timeout=30)
            upstream.settimeout(30)
            auth = _proxy_auth_header(self.user, self.password).encode("latin1")
            request = (
                f"{method} {target} HTTP/1.1\r\n".encode("latin1")
                + auth
                + b"\r\n".join(headers)
                + b"\r\nConnection: close\r\n\r\n"
                + rest
            )
            upstream.sendall(request)
            return upstream

        # SOCKS5 upstream: buka tunnel lalu kirim origin-form.
        upstream = self._connect_upstream(host, port)
        request = (
            f"{method} {path} HTTP/1.1\r\n".encode("latin1")
            + b"\r\n".join(headers)
            + b"\r\nConnection: close\r\n\r\n"
            + rest
        )
        upstream.sendall(request)
        return upstream

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        sockets = [client, upstream]
        for sock in sockets:
            sock.settimeout(None)
        while not self._stop.is_set():
            try:
                readable, _, errored = select.select(sockets, [], sockets, 1.0)
            except (OSError, ValueError):
                return
            if errored:
                return
            for source in readable:
                try:
                    data = source.recv(_BUFFER)
                except OSError:
                    return
                if not data:
                    return
                recipient = upstream if source is client else client
                try:
                    recipient.sendall(data)
                except OSError:
                    return


__all__ = ["ProxyBridge"]
