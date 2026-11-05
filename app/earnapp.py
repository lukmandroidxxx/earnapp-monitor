from __future__ import annotations

import hashlib
import random

import httpx

from . import config

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_DASHBOARD_REFERER = "https://earnapp.com/dashboard/me/passive-income/bandwidth"
_DASHBOARD_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://earnapp.com",
    "Referer": _DASHBOARD_REFERER,
    "X-Requested-With": "XMLHttpRequest",
}


class EarnAppError(RuntimeError):
    """Gagal mengambil data, tapi sesi masih dianggap valid."""


class AuthExpired(EarnAppError):
    """Cookie sesi tidak valid / kedaluwarsa."""


def _dig(payload: object, path: str) -> object | None:
    current = payload
    for part in path.split("."):
        if not part:
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _mock(cookie: str, balance: float | None) -> dict:
    seed = int(hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    base = balance if balance is not None else round(2.0 + (seed % 1500) / 100, 4)
    new_balance = round(base + rng.uniform(0.002, 0.03), 4)
    return {
        "balance": new_balance,
        "lifetime": round(new_balance * rng.uniform(4.0, 9.0), 4),
        "nodes_online": 1 + (seed % 5),
    }


def fetch_account(proxy_url: str | None, cookie: str, balance: float | None = None) -> dict:
    """Ambil saldo/earnings satu akun lewat proxy milik akun itu sendiri."""
    mode = config.settings.scraper_mode
    if mode == "off":
        raise EarnAppError(
            "Mode scraper mati. Set SCRAPER_MODE=real (atau mock untuk uji coba) di .env."
        )
    if mode == "mock":
        return _mock(cookie or "mock", balance)

    if not config.settings.earnapp_url:
        raise EarnAppError(
            "EARN_APP_BALANCE_URL belum diset. Tangkap request dashboard EarnApp "
            "(DevTools > Network) lalu isi URL-nya di .env."
        )
    if not cookie:
        raise AuthExpired("cookie sesi belum diisi")
    if not proxy_url:
        raise EarnAppError("proxy akun belum siap; request dibatalkan (tanpa fallback ke IP server)")

    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
    }
    if config.settings.earnapp_auth_style == "bearer":
        headers["Authorization"] = f"Bearer {cookie}"
    else:
        headers["Cookie"] = cookie

    try:
        with httpx.Client(
            proxy=proxy_url, timeout=config.settings.request_timeout, follow_redirects=True
        ) as client:
            response = client.get(config.settings.earnapp_url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        raise EarnAppError(f"request gagal: {type(exc).__name__}: {exc}") from exc

    if response.status_code in (401, 403):
        raise AuthExpired("cookie ditolak EarnApp (401/403) — tempel ulang cookie sesi")
    if response.status_code >= 400:
        raise EarnAppError(f"HTTP {response.status_code} dari EarnApp")

    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise EarnAppError("respons bukan JSON — endpoint mungkin berubah") from exc

    balance_value = _dig(payload, config.settings.earnapp_balance_path)
    if balance_value is None:
        raise EarnAppError(
            f"field '{config.settings.earnapp_balance_path}' tidak ada di respons. "
            "Sesuaikan EARN_APP_BALANCE_PATH."
        )

    lifetime = _dig(payload, config.settings.earnapp_lifetime_path)
    nodes = _dig(payload, config.settings.earnapp_nodes_path)

    def _num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return {
        "balance": _num(balance_value),
        "lifetime": _num(lifetime),
        "nodes_online": int(nodes) if isinstance(nodes, (int, float)) else None,
    }


def _num(value) -> float | None:  # noqa: ANN001
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:  # noqa: ANN001
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _api_params(extra: dict | None = None) -> dict:
    params = {"appid": "earnapp", "version": config.settings.earnapp_app_version}
    if extra:
        params.update(extra)
    return params


def _cookie_value(cookie: str, name: str) -> str | None:
    for part in (cookie or "").split(";"):
        key, sep, value = part.partition("=")
        if sep and key.strip().lower() == name.lower():
            return value.strip()
    return None


def _request_json(
    client: httpx.Client, method: str, path: str, params: dict | None = None, json_body=None
) -> object:  # noqa: ANN001
    try:
        response = client.request(method, path, params=params, json=json_body)
    except Exception as exc:  # noqa: BLE001
        raise EarnAppError(f"request gagal: {type(exc).__name__}: {exc}") from exc

    if response.status_code in (401, 403):
        raise AuthExpired("cookie ditolak EarnApp (401/403) — login ulang lewat helper")
    if response.status_code >= 400:
        raise EarnAppError(f"HTTP {response.status_code} dari {path}")

    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        raise EarnAppError(f"respons {path} bukan JSON — API mungkin berubah") from exc


def fetch_dashboard(
    proxy_url: str | None,
    cookie: str,
    timeout: int | None = None,
    usage_days: int | None = None,
) -> dict:
    """Ambil data dashboard EarnApp satu akun lewat proxy akun itu sendiri.

    Endpoint yang dipakai (semua butuh cookie sesi):
      - /session          -> cek cookie masih valid
      - /money            -> balance & earnings_total (lifetime)
      - /devices          -> daftar device + info banned/IP + uptime
      - /usage?step=daily -> pemakaian harian per device (milidetik)
      - /device_statuses  -> POST, status online & last seen per device

    Endpoint selain /devices bersifat opsional: kalau gagal, data lain tetap
    dikembalikan dan error dicatat di ``errors``.
    """

    usage_days = usage_days or config.settings.device_usage_days
    if not cookie:
        raise AuthExpired("cookie sesi belum diisi")
    if not proxy_url:
        raise EarnAppError("proxy akun belum siap; request dibatalkan (tanpa fallback ke IP server)")

    headers = dict(_DASHBOARD_HEADERS)
    if config.settings.earnapp_auth_style == "bearer":
        headers["Authorization"] = f"Bearer {cookie}"
    headers["Cookie"] = cookie
    csrf = _cookie_value(cookie, "xsrf-token") or _cookie_value(cookie, "XSRF-TOKEN")
    if csrf:
        headers["xsrf-token"] = csrf

    base = config.settings.earnapp_api_base
    result: dict = {
        "authenticated": False,
        "balance": None,
        "lifetime": None,
        "nodes_online": None,
        "money": {},
        "devices": [],
        "usage": {},
        "statuses": {},
        "errors": [],
    }

    try:
        with httpx.Client(
            proxy=proxy_url,
            timeout=timeout or config.settings.request_timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            session = _request_json(client, "GET", f"{base}/session", _api_params())
            if not isinstance(session, dict) or not session.get("authenticated"):
                raise AuthExpired("sesi EarnApp tidak terautentikasi — cookie kedaluwarsa")
            result["authenticated"] = True

            try:
                money = _request_json(client, "GET", f"{base}/money", _api_params())
                if isinstance(money, dict):
                    result["money"] = money
                    result["balance"] = _num(money.get("balance"))
                    result["lifetime"] = _num(money.get("earnings_total"))
            except EarnAppError as exc:
                result["errors"].append(f"money: {exc}")

            devices = _request_json(client, "GET", f"{base}/devices", _api_params())
            if not isinstance(devices, list):
                raise EarnAppError("respons /devices tidak berbentuk list")
            result["devices"] = [item for item in devices if isinstance(item, dict)]

            uuids = [str(item.get("uuid")) for item in result["devices"] if item.get("uuid")]

            if uuids:
                try:
                    statuses = _request_json(
                        client,
                        "POST",
                        f"{base}/device_statuses",
                        _api_params(),
                        {"devices": [{"uuid": uuid} for uuid in uuids]},
                    )
                    if isinstance(statuses, dict):
                        result["statuses"] = {
                            str(key): value for key, value in statuses.items() if isinstance(value, list)
                        }
                except EarnAppError as exc:
                    result["errors"].append(f"device_statuses: {exc}")

            try:
                usage = _request_json(
                    client, "GET", f"{base}/usage", _api_params({"step": "daily"})
                )
                if isinstance(usage, list):
                    for row in usage:
                        if not isinstance(row, dict):
                            continue
                        uuid = str(row.get("_id") or "")
                        data = row.get("data")
                        if not uuid or not isinstance(data, dict):
                            continue
                        clean: dict[str, int] = {}
                        for day, ms in data.items():
                            value = _int(ms)
                            if value is not None:
                                clean[str(day)] = max(0, value)
                        result["usage"][uuid] = clean
            except EarnAppError as exc:
                result["errors"].append(f"usage: {exc}")

            result["usage_days"] = max(1, int(usage_days))
    except AuthExpired:
        raise
    except EarnAppError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EarnAppError(f"request gagal: {type(exc).__name__}: {exc}") from exc

    online = sum(1 for value in result["statuses"].values() if value)
    result["nodes_online"] = online if result["statuses"] else None
    return result
