from __future__ import annotations

import hashlib
import time
from urllib.parse import quote, urlparse

import httpx

IP_CHECK_URL = "https://api.ipify.org?format=json"
VALID_SCHEMES = {"socks5", "socks5h", "http", "https"}


def parse_endpoint(value: str) -> dict | None:
    """Terima 'host:port:user:pass', 'host:port', atau URL proxy lengkap."""
    text = (value or "").strip()
    if not text:
        return None

    if "://" in text:
        url = urlparse(text)
        if not url.hostname or not url.port:
            return None
        return {
            "host": url.hostname,
            "port": int(url.port),
            "user": url.username or "",
            "password": url.password or "",
        }

    parts = text.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
    elif len(parts) == 2:
        host, port = parts
        user = password = ""
    else:
        return None

    host = host.strip()
    if not host:
        return None
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        return None
    if not 0 < port_number < 65536:
        return None
    return {"host": host, "port": port_number, "user": user, "password": password}


def endpoint_key(value: str) -> str:
    return hashlib.sha256((value or "").strip().lower().encode("utf-8")).hexdigest()


def mask(value: str) -> str:
    parsed = parse_endpoint(value)
    if parsed is None:
        return (value[:28] + "…") if len(value) > 28 else value
    label = f"{parsed['host']}:{parsed['port']}"
    if parsed["user"]:
        label += f":{parsed['user'][:8]}…"
    return label


def build_url(parsed: dict, scheme: str) -> str:
    auth = ""
    if parsed["user"]:
        auth = f"{quote(parsed['user'], safe='')}:{quote(parsed['password'], safe='')}@"
    return f"{scheme}://{auth}{parsed['host']}:{parsed['port']}"


def check(value: str, scheme: str = "auto", timeout: int = 20) -> dict:
    """Tes satu proxy. Tidak menyentuh DB; hasil dikembalikan sebagai dict."""
    parsed = parse_endpoint(value)
    if parsed is None:
        return {
            "ok": False,
            "ip": None,
            "scheme": scheme,
            "latency_ms": None,
            "note": "format proxy tidak dikenali (harus host:port:user:pass)",
        }

    schemes = [scheme] if scheme in VALID_SCHEMES else ["socks5", "http"]
    errors: list[str] = []

    for candidate in schemes:
        started = time.perf_counter()
        try:
            with httpx.Client(
                proxy=build_url(parsed, candidate), timeout=timeout, follow_redirects=True
            ) as client:
                response = client.get(IP_CHECK_URL)
                response.raise_for_status()
                ip = str(response.json().get("ip", "")).strip()
            if not ip:
                raise ValueError("respons IP kosong")
            return {
                "ok": True,
                "ip": ip,
                "scheme": candidate,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "note": "ok",
            }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{candidate}: {type(exc).__name__}")

    return {
        "ok": False,
        "ip": None,
        "scheme": schemes[0],
        "latency_ms": None,
        "note": "; ".join(errors)[:400],
    }
