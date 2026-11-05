from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import config, earnapp, proxy, sheets, telegram
from .crypto import decrypt, encrypt
from .models import (
    Account,
    CookieMeta,
    DeviceUsage,
    EarnDevice,
    ProxySlot,
    SlotCheck,
    Snapshot,
    utcnow,
)

log = logging.getLogger("earnapp-monitor.jobs")

_TRUE_VALUES = {"1", "true", "ya", "yes", "on", "aktif", "active", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "tidak", "no", "off", "nonaktif", "inactive", "disable", "disabled"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _parse_enabled(raw: str) -> bool:
    value = (raw or "").strip().lower()
    if value in _FALSE_VALUES:
        return False
    return True


def _parse_bool(raw: str) -> bool:
    return (raw or "").strip().lower() in _TRUE_VALUES


def _parse_int(raw: str, default: int = 0) -> int:
    try:
        return int(float((raw or "").strip()))
    except (TypeError, ValueError):
        return default


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _save_report(report: dict) -> None:
    config.LAST_SYNC_FILE.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def load_last_sync() -> dict:
    if not config.LAST_SYNC_FILE.exists():
        return {}
    try:
        return json.loads(config.LAST_SYNC_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _set_account_status(account: Account, status: str, reason: str = "", alert: bool = True) -> None:
    previous = account.status
    account.status = status
    account.status_reason = reason or None
    if previous == status or not alert:
        return

    if status in {"BLOCKED", "ERROR"}:
        now = utcnow()
        last = _aware(account.last_alert_at)
        if last and (now - last).total_seconds() < config.settings.alert_cooldown:
            return
        account.last_alert_at = now
        telegram.alert_account(account.name, status, reason)
    elif status == "OK" and previous in {"BLOCKED", "ERROR", "DEGRADED"}:
        telegram.send(f"✅ <b>{account.name}</b> kembali normal (semua slot sehat)")


def _recompute_account(account: Account, alert: bool = True) -> None:
    if not account.enabled:
        _set_account_status(account, "PAUSED", "akun dinonaktifkan", False)
        return

    active = [slot for slot in account.slots if slot.enabled and not slot.blacklisted]
    if not active:
        _set_account_status(account, "PAUSED", "tidak ada slot proxy aktif", alert)
        return

    blocked = [slot for slot in active if slot.status == "BLOCKED"]
    ok = [slot for slot in active if slot.status == "OK"]
    bad = [slot for slot in active if slot.status == "UNHEALTHY"]

    if blocked:
        _set_account_status(account, "BLOCKED", blocked[0].status_reason or "slot bentrok", alert)
    elif len(ok) == len(active):
        _set_account_status(account, "OK", "", alert)
    elif ok:
        _set_account_status(account, "DEGRADED", f"{len(ok)}/{len(active)} slot sehat", alert)
    elif bad and len(bad) == len(active):
        _set_account_status(account, "ERROR", bad[0].status_reason or "semua slot gagal", alert)
    elif bad:
        _set_account_status(account, "DEGRADED", f"{len(bad)}/{len(active)} slot gagal", alert)
    else:
        _set_account_status(account, "UNCHECKED", "belum dicek", alert)


def _pick_slot(account: Account) -> ProxySlot | None:
    candidates = [s for s in account.slots if s.enabled and not s.blacklisted]
    if not candidates:
        return None
    healthy = [s for s in candidates if s.status == "OK"]
    pool = healthy or candidates
    return min(pool, key=lambda s: (s.usage_count, s.id))


def _proxy_url(slot: ProxySlot | None) -> str | None:
    if slot is None:
        return None
    parsed = proxy.parse_endpoint(decrypt(slot.endpoint_enc))
    if parsed is None:
        return None
    scheme = slot.scheme if slot.scheme in proxy.VALID_SCHEMES else "socks5"
    return proxy.build_url(parsed, scheme)


# --------------------------------------------------------------------------
# Sheet -> database
# --------------------------------------------------------------------------
def sync_from_sheet(session: Session) -> dict:
    report: dict = {
        "at": utcnow().isoformat(timespec="seconds"),
        "source": None,
        "rows": 0,
        "accounts_created": 0,
        "slots_created": 0,
        "slots_updated": 0,
        "rejected": [],
        "errors": [],
        "writeback": None,
    }

    try:
        sheet = sheets.read_sheet()
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"gagal membaca sheet: {exc}")
        _save_report(report)
        return report

    report["source"] = sheet.source
    report["rows"] = len(sheet.rows)

    accounts = {account.name.lower(): account for account in session.scalars(select(Account)).all()}
    slots = {slot.endpoint_key: slot for slot in session.scalars(select(ProxySlot)).all()}
    taken_ips = {slot.declared_ip: slot.endpoint_key for slot in slots.values() if slot.declared_ip}

    seen_keys: set[str] = set()
    seen_ips: dict[str, str] = {}
    seen_names: set[str] = set()

    for row in sheet.rows:
        name = row.get("account")
        raw_endpoint = row.get("endpoint")
        raw_ip = row.get("declared_ip")

        if not name and not raw_endpoint:
            continue
        if not name:
            report["rejected"].append(f"baris {row.number}: nama akun kosong")
            continue
        if not raw_endpoint:
            report["rejected"].append(f"baris {row.number} ({name}): proxy command kosong")
            continue

        parsed = proxy.parse_endpoint(raw_endpoint)
        if parsed is None:
            report["rejected"].append(
                f"baris {row.number} ({name}): format proxy tidak dikenali — harus host:port:user:pass"
            )
            continue

        key = proxy.endpoint_key(raw_endpoint)
        if key in seen_keys:
            report["rejected"].append(
                f"baris {row.number} ({name}): sesi proxy duplikat di sheet — baris dilewati"
            )
            continue
        seen_keys.add(key)

        if raw_ip:
            owner = seen_ips.get(raw_ip)
            if owner:
                report["rejected"].append(
                    f"baris {row.number} ({name}): IP {raw_ip} sudah dipakai '{owner}' di sheet"
                )
                continue
            seen_ips[raw_ip] = name

        account = accounts.get(name.lower())
        if account is None:
            account = Account(name=name, status="UNCHECKED")
            session.add(account)
            session.flush()
            accounts[name.lower()] = account
            report["accounts_created"] += 1
        seen_names.add(name.lower())

        account.group_name = row.get("group") or account.group_name
        account.local_ip = row.get("local_ip") or account.local_ip

        cookie = row.get("cookie")
        if cookie:
            account.cookie_enc = encrypt(cookie)
            account.auth_status = "NOT_SET"
            account.auth_reason = "cookie diperbarui dari sheet, belum dites"

        slot = slots.get(key)
        created = slot is None
        if created:
            slot = ProxySlot(account_id=account.id, endpoint_key=key)
            session.add(slot)
            slots[key] = slot

        slot.account_id = account.id
        slot.sheet_row = row.number
        slot.endpoint_enc = encrypt(raw_endpoint)
        slot.endpoint_hint = proxy.mask(raw_endpoint)
        slot.region = row.get("region") or None
        slot.group_name = row.get("group") or None
        slot.local_ip = row.get("local_ip") or None
        slot.usage_count = _parse_int(row.get("usage_count"), 0)
        slot.blacklisted = _parse_bool(row.get("blacklist"))
        slot.enabled = _parse_enabled(row.get("enabled"))

        if config.settings.proxy_scheme in proxy.VALID_SCHEMES:
            slot.scheme = config.settings.proxy_scheme

        if (slot.declared_ip_raw or None) != (raw_ip or None):
            slot.detected_ip = None
            slot.last_check = None
            slot.last_ok = None
            if slot.status != "BLOCKED":
                slot.status = "UNCHECKED"
                slot.status_reason = None
        slot.declared_ip_raw = raw_ip or None

        conflict = taken_ips.get(raw_ip) if raw_ip else None
        if raw_ip and conflict and conflict != key:
            slot.declared_ip = None
            slot.status = "BLOCKED"
            slot.status_reason = f"IP {raw_ip} sudah dipakai slot akun lain"
            report["rejected"].append(
                f"baris {row.number} ({name}): IP {raw_ip} bentrok dengan slot lain — tidak dijalankan"
            )
        else:
            slot.declared_ip = raw_ip or None
            if raw_ip:
                taken_ips[raw_ip] = key
            if slot.status == "BLOCKED" and (slot.status_reason or "").startswith("IP "):
                slot.status = "UNCHECKED"
                slot.status_reason = None

        if slot.blacklisted:
            slot.status = "BLACKLIST"
            slot.status_reason = "terdaftar di kolom blacklist"
        elif slot.status == "BLACKLIST":
            slot.status = "UNCHECKED"
            slot.status_reason = None

        report["slots_created" if created else "slots_updated"] += 1

    # Slot yang hilang dari sheet: nonaktifkan, jangan hapus (riwayat tetap ada).
    for key, slot in slots.items():
        if key in seen_keys:
            continue
        if slot.enabled:
            slot.enabled = False
            slot.status_reason = "tidak ada di sheet"
            if slot.status == "OK":
                slot.status = "PAUSED"

    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        report["errors"].append(f"gagal menyimpan ke database: {exc}")
        _save_report(report)
        return report

    for account in accounts.values():
        _recompute_account(account, alert=True)
    session.commit()

    writeback: dict[int, dict[str, str]] = {}
    for slot in slots.values():
        if not slot.sheet_row:
            continue
        writeback[slot.sheet_row] = {
            "status": slot.status,
            "detected_ip": slot.detected_ip or "",
            "healthy": "yes" if slot.status == "OK" else "no",
            "last_check": slot.last_check.isoformat(timespec="minutes") if slot.last_check else "",
        }
    ok, message = sheets.writeback(sheet, writeback)
    report["writeback"] = message
    if not ok and "gagal" in message:
        report["errors"].append(message)

    _save_report(report)
    return report


# --------------------------------------------------------------------------
# Polling slot + guard IP
# --------------------------------------------------------------------------
def poll_slots(session: Session) -> dict:
    now = utcnow()
    report: dict = {"at": now.isoformat(timespec="seconds"), "checked": 0, "ok": 0, "problems": []}

    session.execute(delete(SlotCheck).where(SlotCheck.created_at < now - timedelta(days=30)))

    accounts = {account.id: account for account in session.scalars(select(Account)).all()}
    slots = [
        slot
        for slot in session.scalars(select(ProxySlot).where(ProxySlot.enabled == True)).all()  # noqa: E712
        if not slot.blacklisted and accounts.get(slot.account_id) and accounts[slot.account_id].enabled
    ]

    tasks: list[tuple[int, str, str]] = []
    for slot in slots:
        endpoint = decrypt(slot.endpoint_enc)
        if not endpoint:
            slot.status = "UNHEALTHY"
            slot.status_reason = "kredensial proxy tidak bisa dibaca"
            continue
        tasks.append((slot.id, endpoint, slot.scheme))

    results: dict[int, dict] = {}
    if tasks:
        workers = max(1, config.settings.poll_concurrency)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(proxy.check, endpoint, scheme, config.settings.request_timeout): slot_id
                for slot_id, endpoint, scheme in tasks
            }
            for future in as_completed(futures):
                slot_id = futures[future]
                try:
                    results[slot_id] = future.result()
                except Exception as exc:  # noqa: BLE001
                    results[slot_id] = {
                        "ok": False,
                        "ip": None,
                        "scheme": "auto",
                        "latency_ms": None,
                        "note": f"error: {exc}",
                    }

    for slot in slots:
        result = results.get(slot.id)
        if result is None:
            continue
        report["checked"] += 1
        slot.last_check = now

        if not result["ok"]:
            slot.status = "UNHEALTHY"
            slot.status_reason = result["note"]
            slot.detected_ip = None
            session.add(
                SlotCheck(slot_id=slot.id, ip=None, ok=False, note=result["note"], created_at=now)
            )
            report["problems"].append(f"{slot.endpoint_hint}: {result['note']}")
            continue

        slot.detected_ip = result["ip"]
        slot.latency_ms = result["latency_ms"]
        slot.last_ok = now
        if result["scheme"] in proxy.VALID_SCHEMES:
            slot.scheme = result["scheme"]
        session.add(
            SlotCheck(
                slot_id=slot.id,
                ip=result["ip"],
                ok=True,
                latency_ms=result["latency_ms"],
                note="ok",
                created_at=now,
            )
        )
        report["ok"] += 1

        if slot.declared_ip_raw and result["ip"] != slot.declared_ip_raw:
            slot.status = "UNHEALTHY"
            slot.status_reason = f"IP keluar {result['ip']} ≠ sheet {slot.declared_ip_raw}"
            report["problems"].append(f"{slot.endpoint_hint}: {slot.status_reason}")
        else:
            slot.status = "OK"
            slot.status_reason = None

    # Guard: dua slot tidak boleh keluar dari IP yang sama.
    recent = [
        slot
        for slot in slots
        if slot.detected_ip and slot.last_check and _aware(slot.last_check) >= now - timedelta(hours=1)
    ]
    by_ip: dict[str, list[ProxySlot]] = {}
    for slot in recent:
        by_ip.setdefault(slot.detected_ip, []).append(slot)

    for ip, group in by_ip.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda s: (s.declared_ip != ip, s.id))
        keeper = group[0]
        for other in group[1:]:
            other.status = "BLOCKED"
            other.status_reason = f"IP {ip} juga dipakai slot #{keeper.id} ({keeper.endpoint_hint})"
            report["problems"].append(f"{other.endpoint_hint}: {other.status_reason}")

    session.commit()

    for account in accounts.values():
        if account.enabled:
            _recompute_account(account, alert=True)
    session.commit()

    if config.settings.scraper_mode != "off":
        for account in accounts.values():
            if account.enabled and account.cookie_enc and account.auth_status != "EXPIRED":
                _refresh_balance(session, account, alert=True)

    if config.settings.device_sync:
        for account in accounts.values():
            if account.enabled and account.cookie_enc and account.auth_status != "EXPIRED":
                try:
                    sync_earnapp(session, account, alert=True)
                except Exception:  # noqa: BLE001
                    log.exception("sync device akun %s gagal", account.name)

    if config.settings.sheet_writeback:
        _writeback_polled(session, accounts.values())

    return report


def _writeback_polled(session: Session, accounts) -> None:  # noqa: ANN001
    try:
        sheet = sheets.read_sheet()
    except Exception:  # noqa: BLE001
        return
    updates: dict[int, dict[str, str]] = {}
    for account in accounts:
        for slot in account.slots:
            if not slot.sheet_row:
                continue
            updates[slot.sheet_row] = {
                "status": slot.status,
                "detected_ip": slot.detected_ip or "",
                "healthy": "yes" if slot.status == "OK" else "no",
                "last_check": slot.last_check.isoformat(timespec="minutes") if slot.last_check else "",
            }
    sheets.writeback(sheet, updates)


# --------------------------------------------------------------------------
# Cookie / auth
# --------------------------------------------------------------------------
def _refresh_balance(session: Session, account: Account, alert: bool = True) -> tuple[bool, str]:
    cookie = decrypt(account.cookie_enc)
    if not cookie:
        account.auth_status = "NOT_SET"
        account.auth_reason = "cookie belum diisi"
        session.commit()
        return False, account.auth_reason

    slot = _pick_slot(account)
    proxy_url = _proxy_url(slot)

    try:
        data = earnapp.fetch_account(proxy_url, cookie, account.balance)
    except earnapp.AuthExpired as exc:
        account.auth_status = "EXPIRED"
        account.auth_reason = str(exc)
        account.auth_checked_at = utcnow()
        session.commit()
        if alert:
            telegram.send(f"🔑 <b>{account.name}</b> cookie kedaluwarsa — tempel ulang di dashboard")
        return False, str(exc)
    except earnapp.EarnAppError as exc:
        account.auth_status = "ERROR"
        account.auth_reason = str(exc)
        account.auth_checked_at = utcnow()
        session.commit()
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        account.auth_status = "ERROR"
        account.auth_reason = f"{type(exc).__name__}: {exc}"
        account.auth_checked_at = utcnow()
        session.commit()
        return False, account.auth_reason

    account.auth_status = "OK"
    account.auth_reason = None
    account.auth_checked_at = utcnow()
    account.auth_ip = slot.detected_ip if slot else None
    account.balance = data.get("balance")
    account.lifetime = data.get("lifetime")
    account.nodes_online = data.get("nodes_online")
    session.add(
        Snapshot(
            account_id=account.id,
            balance=account.balance,
            lifetime=account.lifetime,
            nodes_online=account.nodes_online,
        )
    )
    session.commit()
    return True, f"login OK · saldo ${account.balance or 0:.4f}"


def test_auth(session: Session, account: Account) -> tuple[bool, str]:
    """Dipakai tombol 'Test login' di dashboard."""
    cookie = decrypt(account.cookie_enc)
    if not cookie:
        return False, "Cookie belum diisi."

    if config.settings.scraper_mode == "off":
        # Validasi lewat API dashboard EarnApp; tidak butuh SCRAPER_MODE.
        if not config.settings.device_sync:
            return False, "SCRAPER_MODE=off dan DEVICE_SYNC=0: tidak ada jalur validasi cookie."
        report = sync_earnapp(session, account, alert=False)
        if account.auth_status == "OK":
            devices = report.get("devices") or 0
            parts = [f"login OK · {devices} device" if devices else "login OK"]
            if report.get("balance") is not None:
                parts.append(f"saldo ${report['balance']:.4f}")
            return True, " · ".join(parts)
        reason = account.auth_reason or "; ".join(report.get("errors") or [])
        return False, reason or "login gagal"

    slot = _pick_slot(account)
    if config.settings.scraper_mode != "mock":
        if slot is None:
            return False, "Tidak ada slot proxy aktif untuk akun ini."
        if slot.status != "OK":
            result = proxy.check(
                decrypt(slot.endpoint_enc), slot.scheme, config.settings.request_timeout
            )
            slot.last_check = utcnow()
            if result["ok"]:
                slot.status = "OK"
                slot.status_reason = None
                slot.detected_ip = result["ip"]
                slot.latency_ms = result["latency_ms"]
                slot.scheme = result["scheme"]
            else:
                slot.status = "UNHEALTHY"
                slot.status_reason = result["note"]
            session.commit()
        if slot.status != "OK":
            return False, f"Proxy gagal: {slot.status_reason}"

    return _refresh_balance(session, account, alert=False)


def _ensure_proxy_ready(session: Session, account: Account) -> tuple[ProxySlot | None, str | None]:
    """Pastikan ada slot proxy yang bisa dipakai; cek ulang kalau statusnya belum OK."""
    slot = _pick_slot(account)
    if slot is None:
        return None, "tidak ada slot proxy aktif"

    if slot.status == "OK":
        return slot, None

    result = proxy.check(decrypt(slot.endpoint_enc), slot.scheme, config.settings.request_timeout)
    slot.last_check = utcnow()
    if not result["ok"]:
        slot.status = "UNHEALTHY"
        slot.status_reason = result["note"]
        session.commit()
        return None, f"proxy gagal: {result['note']}"

    slot.detected_ip = result["ip"]
    slot.latency_ms = result["latency_ms"]
    slot.last_ok = utcnow()
    if result["scheme"] in proxy.VALID_SCHEMES:
        slot.scheme = result["scheme"]
    if slot.declared_ip_raw and result["ip"] != slot.declared_ip_raw:
        slot.status = "UNHEALTHY"
        slot.status_reason = f"IP keluar {result['ip']} ≠ sheet {slot.declared_ip_raw}"
    else:
        slot.status = "OK"
        slot.status_reason = None
    session.commit()
    return slot, None


def sync_earnapp(session: Session, account: Account, alert: bool = True) -> dict:
    """Tarik device, status online, usage harian, dan saldo dari API EarnApp."""
    report: dict = {
        "ok": False,
        "account": account.name,
        "devices": 0,
        "online": 0,
        "banned": 0,
        "usage_rows": 0,
        "errors": [],
    }

    cookie = decrypt(account.cookie_enc)
    if not cookie:
        account.auth_reason = "cookie belum diisi"
        session.commit()
        report["errors"].append("cookie belum diisi")
        return report

    slot, problem = _ensure_proxy_ready(session, account)
    if slot is None:
        account.auth_status = "ERROR"
        account.auth_reason = problem
        account.auth_checked_at = utcnow()
        session.commit()
        report["errors"].append(problem or "proxy tidak siap")
        return report

    try:
        data = earnapp.fetch_dashboard(
            _proxy_url(slot),
            cookie,
            timeout=config.settings.request_timeout,
            usage_days=config.settings.device_usage_days,
        )
    except earnapp.AuthExpired as exc:
        account.auth_status = "EXPIRED"
        account.auth_reason = str(exc)
        account.auth_checked_at = utcnow()
        session.commit()
        report["errors"].append(str(exc))
        if alert:
            telegram.send(f"🔑 <b>{account.name}</b> cookie kedaluwarsa — login ulang lewat helper")
        return report
    except earnapp.EarnAppError as exc:
        account.auth_status = "ERROR"
        account.auth_reason = str(exc)
        account.auth_checked_at = utcnow()
        session.commit()
        report["errors"].append(str(exc))
        return report
    except Exception as exc:  # noqa: BLE001
        account.auth_status = "ERROR"
        account.auth_reason = f"{type(exc).__name__}: {exc}"
        account.auth_checked_at = utcnow()
        session.commit()
        report["errors"].append(account.auth_reason)
        return report

    now = utcnow()
    existing = {device.uuid: device for device in account.devices}
    statuses: dict = data.get("statuses") or {}
    seen: set[str] = set()
    banned_alerts: list[dict] = []
    online_count = 0
    banned_count = 0

    for item in data.get("devices") or []:
        uuid = str(item.get("uuid") or "").strip()
        if not uuid:
            continue
        seen.add(uuid)
        device = existing.get(uuid)
        if device is None:
            device = EarnDevice(account_id=account.id, uuid=uuid)
            session.add(device)

        banned = item.get("banned") if isinstance(item.get("banned"), dict) else None
        reason = str(banned.get("reason") or "").strip() if banned else ""
        banned_ip = str(banned.get("ip") or "").strip() if banned else ""
        prev_reason = device.banned_reason
        prev_ip = device.banned_ip

        device.title = str(item.get("title") or device.title or "")[:160]
        device.country = str(item.get("country") or "")[:8] or None
        device.rate = earnapp._num(item.get("rate"))
        device.earned = earnapp._num(item.get("earned"))
        device.earned_total = earnapp._num(item.get("earned_total"))
        device.uptime_ms = earnapp._int(item.get("uptime"))
        device.total_uptime_ms = earnapp._int(item.get("total_uptime"))
        ips = item.get("ips") if isinstance(item.get("ips"), list) else []
        device.ips_json = json.dumps([str(ip) for ip in ips])
        device.banned_reason = reason or None
        device.banned_ip = banned_ip or None

        if banned_ip and (not prev_reason or prev_reason != reason or prev_ip != banned_ip):
            banned_alerts.append(
                {
                    "title": device.title or uuid,
                    "reason": reason or "banned",
                    "ip": banned_ip,
                }
            )
        if banned_ip:
            banned_count += 1

        status = statuses.get(uuid)
        if status:
            device.online = True
            device.online_known = True
            online_count += 1
            last_seen_ms = earnapp._int(status[0]) if status else None
            if last_seen_ms:
                device.last_seen = datetime.fromtimestamp(last_seen_ms / 1000, tz=timezone.utc)
        else:
            device.online = False
            device.online_known = bool(statuses)
        device.updated_at = now

    # Device yang sudah tidak muncul di API dianggap dihapus dari akun.
    for uuid, device in existing.items():
        if uuid not in seen:
            session.delete(device)

    # Usage harian per device, dibatasi DEVICE_USAGE_DAYS hari terakhir.
    usage_days = max(1, config.settings.device_usage_days)
    cutoff_day = (datetime.now(timezone.utc).date() - timedelta(days=usage_days - 1)).isoformat()
    usage_rows = session.execute(
        select(DeviceUsage).where(DeviceUsage.account_id == account.id)
    ).scalars().all()
    usage_index = {(row.device_uuid, row.day): row for row in usage_rows}

    for uuid, days in (data.get("usage") or {}).items():
        if uuid not in seen:
            continue
        for day, ms in days.items():
            if day < cutoff_day:
                continue
            row = usage_index.get((uuid, day))
            if row is None:
                row = DeviceUsage(account_id=account.id, device_uuid=uuid, day=day, ms=ms)
                session.add(row)
            else:
                row.ms = ms
                row.updated_at = now
            report["usage_rows"] += 1

    for key, row in usage_index.items():
        if key[0] not in seen or key[1] < cutoff_day:
            session.delete(row)

    account.auth_status = "OK"
    account.auth_reason = None
    account.auth_checked_at = now
    account.auth_ip = slot.detected_ip
    if data.get("balance") is not None:
        account.balance = data["balance"]
    if data.get("lifetime") is not None:
        account.lifetime = data["lifetime"]
    account.nodes_online = online_count
    session.add(
        Snapshot(
            account_id=account.id,
            balance=account.balance,
            lifetime=account.lifetime,
            nodes_online=online_count,
        )
    )
    session.commit()

    if alert:
        for item in banned_alerts:
            telegram.send(
                f"⛔ <b>{account.name}</b> · device <code>{item['title']}</code> kena block\n"
                f"Alasan: <code>{item['reason']}</code>\nIP: <code>{item['ip']}</code>"
            )

    report.update(
        {
            "ok": True,
            "devices": len(seen),
            "online": online_count,
            "banned": banned_count,
            "balance": account.balance,
            "lifetime": account.lifetime,
            "errors": list(data.get("errors") or []),
        }
    )
    return report


def sync_earnapp_all(session: Session, alert: bool = True) -> dict:
    """Sinkronkan data EarnApp untuk semua akun yang punya cookie."""
    accounts = session.execute(
        select(Account).where(Account.enabled == True).order_by(Account.id)  # noqa: E712
    ).scalars().all()

    summary: dict = {
        "at": utcnow().isoformat(timespec="seconds"),
        "accounts": 0,
        "ok": 0,
        "devices": 0,
        "online": 0,
        "banned": 0,
        "errors": [],
    }
    for account in accounts:
        if not account.cookie_enc:
            continue
        summary["accounts"] += 1
        try:
            result = sync_earnapp(session, account, alert=alert)
        except Exception as exc:  # noqa: BLE001
            log.exception("sync earnapp akun %s gagal", account.name)
            session.rollback()
            summary["errors"].append(f"{account.name}: {type(exc).__name__}: {exc}")
            continue
        if result.get("ok"):
            summary["ok"] += 1
            summary["devices"] += result.get("devices", 0)
            summary["online"] += result.get("online", 0)
            summary["banned"] += result.get("banned", 0)
        for error in result.get("errors") or []:
            summary["errors"].append(f"{account.name}: {error}")
    return summary


AUTH_COOKIE_NAMES = {"auth", "oauth-token", "oauth-refresh-token", "earnapp_user"}
_SESSION_HINTS = ("token", "auth", "jwt")
_CSRF_HINTS = ("csrf", "xsrf", "anti-forgery")


def is_auth_cookie(name: str, value: str = "") -> bool:
    """True kalau cookie ini menandakan sesi login EarnApp (bukan visitor/CSRF)."""
    lname = (name or "").strip().lower()
    val = value or ""
    if not lname or len(val) < 8:
        return False
    if any(hint in lname for hint in _CSRF_HINTS):
        return False
    if lname in AUTH_COOKIE_NAMES:
        return True
    return any(hint in lname for hint in _SESSION_HINTS) and len(val) >= 12


def cookie_names(cookie_header: str) -> list[str]:
    """Daftar nama cookie dari header `name=value; name2=value2`."""
    return [
        part.split("=", 1)[0].strip()
        for part in (cookie_header or "").split(";")
        if "=" in part
    ]


def has_auth_cookie(cookie_header: str) -> bool:
    return any(is_auth_cookie(name, "x" * 16) for name in cookie_names(cookie_header))


def _clean_cookie_value(value: object) -> str:
    return re.sub(r"[\r\n]+", "", str(value))


def _sanitize_cookie_items(cookies: list[dict] | None) -> list[dict]:
    """Ambil cookie earnapp.com yang belum kedaluwarsa, simpan metadata aslinya."""
    now = time.time()
    items: list[dict] = []
    seen: set[str] = set()
    for item in cookies or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not name or value is None:
            continue

        domain = str(item.get("domain") or "").strip().lower()
        if domain and not (domain == "earnapp.com" or domain.endswith(".earnapp.com")):
            continue

        # Playwright memakai `expires`; Cookie-Editor memakai `expirationDate`.
        expires = item.get("expirationDate")
        if not isinstance(expires, (int, float)):
            expires = item.get("expires")
        if isinstance(expires, (int, float)) and 0 < expires < now:
            continue  # cookie sudah kedaluwarsa

        lname = name.lower()
        if lname in seen:
            continue
        seen.add(lname)

        same_site = str(item.get("sameSite") or "no_restriction").lower()
        same_site = {
            "none": "no_restriction",
            "no_restriction": "no_restriction",
            "lax": "lax",
            "strict": "strict",
            "unspecified": "unspecified",
        }.get(same_site, "no_restriction")

        entry = {
            "domain": domain or ".earnapp.com",
            "hostOnly": bool(item.get("hostOnly", not (domain or "").startswith("."))),
            "httpOnly": bool(item.get("httpOnly", True)),
            "name": name,
            "path": str(item.get("path") or "/"),
            "sameSite": same_site,
            "secure": bool(item.get("secure", True)),
            "session": True,
            "storeId": str(item.get("storeId") or "0"),
            "value": _clean_cookie_value(value),
        }
        if isinstance(expires, (int, float)) and expires > 0:
            entry["expirationDate"] = float(expires)
            entry["session"] = False
        items.append(entry)
    return items


def cookie_export_json(cookie_header: str, meta: list[dict] | None = None) -> list[dict]:
    """Bentuk JSON kompatibel Cookie-Editor dari cookie yang tersimpan.

    Kalau metadata asli (expiry, httpOnly, path) tersedia, pakai itu; kalau tidak,
    susun dari header dengan status session.
    """
    sanitized = _sanitize_cookie_items(meta)
    if meta is not None:
        return sanitized

    result: list[dict] = []
    for part in (cookie_header or "").split(";"):
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if not name:
            continue
        result.append(
            {
                "domain": ".earnapp.com",
                "hostOnly": False,
                "httpOnly": True,
                "name": name,
                "path": "/",
                "sameSite": "no_restriction",
                "secure": True,
                "session": True,
                "storeId": "0",
                "value": value.strip(),
            }
        )
    return result


def _cookie_items_from_json(text: str) -> list[dict] | None:
    """Ubah export JSON Cookie-Editor / EditThisCookie menjadi metadata cookie."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None

    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            data = data["cookies"]
        else:
            data = [
                {"name": key, "value": value}
                for key, value in data.items()
                if isinstance(value, str)
            ]

    if not isinstance(data, list):
        return None
    return _sanitize_cookie_items(data)


def parse_cookie_input(value: str) -> tuple[str, list[dict]]:
    """Hasil: (header cookie, metadata asli kalau input berupa JSON).

    Header kosong berarti input tidak dikenali; route harus menolak dan tidak
    menghapus cookie lama.
    """
    text = (value or "").strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()

    metadata: list[dict] = []
    if text[:1] in "[{":
        items = _cookie_items_from_json(text)
        if items is None:
            return "", []
        metadata = items
        pairs = [(item["name"], item["value"]) for item in items]
    else:
        pairs = []
        for part in re.split(r"[;\r\n]+", text):
            part = part.strip()
            if "=" not in part:
                continue
            name, _, val = part.partition("=")
            name = name.strip()
            if name:
                pairs.append((name, val.strip()))

    cleaned: list[str] = []
    seen: set[str] = set()
    for name, val in pairs:
        lname = name.lower()
        if lname in {"cookie", "set-cookie", "host", "user-agent"}:
            continue
        if lname in seen:
            continue
        seen.add(lname)
        cleaned.append(f"{name}={val}")
    return "; ".join(cleaned), metadata


def normalize_cookie(value: str) -> str:
    """Rapikan cookie manual dan kembalikan header `name=value; ...`."""
    return parse_cookie_input(value)[0]


def set_cookie(
    session: Session,
    account: Account,
    cookie: str,
    cookies: list[dict] | None = None,
) -> None:
    cleaned = normalize_cookie(cookie)
    count = len(cookie_names(cleaned))
    account.cookie_enc = encrypt(cleaned) if cleaned else None
    account.auth_status = "NOT_SET"
    if cleaned:
        account.auth_reason = f"cookie manual ({count} cookie) disimpan, belum dites"
    else:
        account.auth_reason = "cookie dihapus"
    account.auth_ip = None

    meta = session.get(CookieMeta, account.id)
    if not cleaned:
        if meta is not None:
            session.delete(meta)
    else:
        items = _sanitize_cookie_items(cookies)
        if items:
            payload = encrypt(json.dumps(items, ensure_ascii=False))
            if meta is None:
                session.add(CookieMeta(account_id=account.id, payload_enc=payload))
            else:
                meta.payload_enc = payload
        elif meta is not None:
            session.delete(meta)
    session.commit()


def check_single(session: Session, slot: ProxySlot) -> tuple[bool, str]:
    """Cek satu slot dari tombol di dashboard."""
    now = utcnow()
    result = proxy.check(decrypt(slot.endpoint_enc), slot.scheme, config.settings.request_timeout)
    slot.last_check = now

    if result["ok"]:
        slot.detected_ip = result["ip"]
        slot.latency_ms = result["latency_ms"]
        slot.last_ok = now
        if result["scheme"] in proxy.VALID_SCHEMES:
            slot.scheme = result["scheme"]
        if slot.declared_ip_raw and result["ip"] != slot.declared_ip_raw:
            slot.status = "UNHEALTHY"
            slot.status_reason = f"IP keluar {result['ip']} ≠ sheet {slot.declared_ip_raw}"
        else:
            slot.status = "OK"
            slot.status_reason = None
        session.add(
            SlotCheck(
                slot_id=slot.id,
                ip=result["ip"],
                ok=True,
                latency_ms=result["latency_ms"],
                note="manual check",
                created_at=now,
            )
        )
    else:
        slot.status = "UNHEALTHY"
        slot.status_reason = result["note"]
        slot.detected_ip = None
        session.add(
            SlotCheck(slot_id=slot.id, ip=None, ok=False, note=result["note"], created_at=now)
        )

    session.commit()
    if slot.account is not None:
        _recompute_account(slot.account, alert=False)
        session.commit()
    return result["ok"], slot.status_reason or "ok"


def format_duration(hours: int | float | None) -> str:
    """Format jam uptime menjadi durasi ringkas tanpa komponen nol.

    Contoh: 168 -> "7D", 30 -> "1D6H", 5 -> "5H", 0 -> "-", 0.5 -> "<1H".
    """
    try:
        value = float(hours or 0)
    except (TypeError, ValueError):
        return "-"
    if value <= 0:
        return "-"
    if value < 1:
        return "<1H"
    total = int(round(value))
    if total < 1:
        return "<1H"
    days, remainder = divmod(total, 24)
    if days and remainder:
        return f"{days}D{remainder}H"
    if days:
        return f"{days}D"
    return f"{remainder}H"

