"""Import pool IP dari file xlsx (export Loki / sheet) + assignment ke port.

Modul ini sengaja tidak menyentuh FastAPI supaya bisa dites langsung lewat
Python. Semua akses DB memakai Session yang dikirim dari pemanggil.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, proxy
from .crypto import decrypt, encrypt
from .models import PoolEntry, PoolImport, PoolPort, utcnow

# --------------------------------------------------------------------------
# Konfigurasi mesin/port (mengikuti data.json LokiProxyTools)
# --------------------------------------------------------------------------
DEFAULT_POOL_CONFIG = {
    "machine": "RDP1",
    "bind_ip": "192.168.18.200",
    "start_port": 10000,
    "num_ports": 28,
    "max_ports_per_ip": 2,
    "group_rules": [
        {"start": 10000, "end": 10015, "group": "1"},
        {"start": 10016, "end": 10027, "group": "2"},
    ],
}

_POOL_CONFIG_FILE = config.CONFIG_DIR / "pool.json"


def load_pool_config() -> dict:
    data = dict(DEFAULT_POOL_CONFIG)
    if _POOL_CONFIG_FILE.exists():
        try:
            raw = json.loads(_POOL_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except (OSError, ValueError):
            pass
    return data


def group_for_port(port: int, rules: list[dict] | None = None) -> str | None:
    for rule in rules if rules is not None else DEFAULT_POOL_CONFIG["group_rules"]:
        try:
            if int(rule["start"]) <= port <= int(rule["end"]):
                return str(rule.get("group") or "")
        except (KeyError, TypeError, ValueError):
            continue
    return None


# --------------------------------------------------------------------------
# Parsing xlsx
# --------------------------------------------------------------------------
_ALIASES: list[tuple[str, set[str]]] = [
    ("command", {"proxy command", "proxy comand", "proxy cmd", "endpoint", "command"}),
    ("ip", {"proxy ip address", "proxy ip", "ip address", "ip publik", "ip"}),
    ("region", {"country region", "country/region", "region", "country", "lokasi"}),
    ("asn", {"asn"}),
    ("usage_at", {"usage time", "usage at", "usage", "add", "added"}),
    ("expires_at", {"expiration time", "expires at", "expired at", "expiry", "expires", "expired"}),
    ("status", {"status"}),
    ("group", {"group", "grup"}),
    ("account", {"account", "akun", "nama akun", "nama"}),
    ("local_ip", {"local ip", "ip lokal"}),
    ("count", {"count", "pemakaian", "used", "usage count"}),
    ("blacklist", {"blacklist", "black list", "blokir"}),
]

_TRUE_VALUES = {"1", "true", "yes", "y", "ya", "on", "aktif", "active", "ok", "v", "x", "yes "}
_STATUS_MAP = {
    "active": "ACTIVE",
    "aktif": "ACTIVE",
    "online": "ACTIVE",
    "offline": "OFFLINE",
    "expired": "EXPIRED",
    "expire": "EXPIRED",
    "expiring": "ACTIVE",
    "unknown": "UNKNOWN",
}


def _norm(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _role_for(header: object) -> str | None:
    value = _norm(header)
    if not value:
        return None
    for role, aliases in _ALIASES:
        if value in aliases:
            return role
    return None


def _parse_dt(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Excel serial date (1899-12-30 epoch).
        try:
            base = datetime(1899, 12, 30, tzinfo=timezone.utc)
            return base + timedelta(days=float(value))
        except (OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return _norm(value) in _TRUE_VALUES


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _norm_group(value: object) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
    except ValueError:
        pass
    return text


def _split_region(value: str | None) -> tuple[str | None, str | None, str | None, str | None]:
    """'US-Oregon-Portland' -> ('US', 'Oregon', 'Portland', nilai asli)."""
    if not value:
        return None, None, None, None
    parts = [part.strip() for part in value.split("-") if part.strip()]
    if not parts:
        return None, None, None, value
    code = parts[0].upper() if len(parts[0]) == 2 else None
    state = parts[1] if len(parts) > 1 else None
    city = "-".join(parts[2:]) if len(parts) > 2 else None
    return code, state, city, value


@dataclass
class ParsedEntry:
    endpoint: str
    key: str
    host: str
    port: int
    username: str
    password: str
    ip_address: str | None = None
    country_region: str | None = None
    asn: str | None = None
    status: str = "UNKNOWN"
    usage_at: datetime | None = None
    expires_at: datetime | None = None
    account_name: str | None = None
    group_name: str | None = None
    local_ip: str | None = None
    blacklisted: bool = False
    sheets: list[str] = field(default_factory=list)


@dataclass
class ParsedFile:
    filename: str
    entries: list[ParsedEntry]
    skipped: int = 0
    sheets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _merge_entry(target: ParsedEntry, incoming: ParsedEntry) -> None:
    for attr in (
        "ip_address",
        "country_region",
        "asn",
        "account_name",
        "group_name",
        "local_ip",
    ):
        if getattr(incoming, attr) and not getattr(target, attr):
            setattr(target, attr, getattr(incoming, attr))
    if incoming.usage_at and not target.usage_at:
        target.usage_at = incoming.usage_at
    if incoming.expires_at and not target.expires_at:
        target.expires_at = incoming.expires_at
    if incoming.status != "UNKNOWN" and target.status == "UNKNOWN":
        target.status = incoming.status
    target.blacklisted = target.blacklisted or incoming.blacklisted
    for sheet in incoming.sheets:
        if sheet not in target.sheets:
            target.sheets.append(sheet)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse_xlsx(data: bytes, filename: str = "") -> ParsedFile:
    """Baca sheet yang punya kolom 'Proxy Command' / 'Proxy IP'.

    Kalau file punya tab bernama "Pool" (sheet kerja pengguna), hanya tab itu
    (+ Blacklist/Whitelist) yang dipakai supaya riwayat lama di tab Record tidak
    ikut masuk. Export Loki biasa (satu sheet) tetap dibaca semuanya.
    """
    try:
        workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"file bukan xlsx yang valid: {exc}") from exc

    candidates: list[tuple[object, dict[str, int], list, bool, bool]] = []
    for worksheet in workbook.worksheets:
        rows = list(worksheet.iter_rows(values_only=True))
        columns: dict[str, int] = {}
        for index, row in enumerate(rows):
            if index > 30:
                break
            mapped: dict[str, int] = {}
            for col, cell in enumerate(row or ()):
                role = _role_for(cell)
                if role and role not in mapped:
                    mapped[role] = col
            if "command" in mapped:
                columns = mapped
                break
        if "command" not in columns:
            continue
        title = _norm(worksheet.title)
        candidates.append(
            (
                worksheet,
                columns,
                rows,
                "blacklist" in title,
                "whitelist" in title,
            )
        )

    selected = candidates
    pool_like = [item for item in candidates if _norm(item[0].title) == "pool"]
    if pool_like:
        selected = [
            item
            for item in candidates
            if _norm(item[0].title) == "pool"
            or _norm(item[0].title) in {"blacklist", "whitelist"}
        ]

    found: dict[str, ParsedEntry] = {}
    skipped = 0
    sheet_names: list[str] = []
    warnings: list[str] = []

    for worksheet, columns, rows, sheet_is_blacklist, sheet_is_whitelist in selected:
        sheet_names.append(worksheet.title)
        cmd_col = columns["command"]
        for row in rows:
            if not row or cmd_col >= len(row):
                skipped += 1
                continue
            raw_command = _clean_text(row[cmd_col])
            if not raw_command:
                # Baris kosong bukan error; hanya lewati.
                if any(_clean_text(cell) for cell in row):
                    skipped += 1
                continue
            parsed = proxy.parse_endpoint(raw_command)
            if parsed is None:
                skipped += 1
                continue
            key = proxy.endpoint_key(raw_command)

            def cell(role: str):
                idx = columns.get(role)
                if idx is None or idx >= len(row):
                    return None
                return row[idx]

            region_raw = _clean_text(cell("region"))
            ip_value = _clean_text(cell("ip"))
            status_text = _norm(cell("status"))
            entry = ParsedEntry(
                endpoint=raw_command,
                key=key,
                host=parsed["host"],
                port=int(parsed["port"]),
                username=parsed["user"],
                password=parsed["password"],
                ip_address=ip_value,
                country_region=region_raw,
                asn=_clean_text(cell("asn")),
                status=_STATUS_MAP.get(status_text, "UNKNOWN"),
                usage_at=_parse_dt(cell("usage_at")),
                expires_at=_parse_dt(cell("expires_at")),
                account_name=_clean_text(cell("account")),
                group_name=_norm_group(cell("group")),
                local_ip=_clean_text(cell("local_ip")),
                blacklisted=(
                    sheet_is_blacklist
                    or _parse_bool(cell("blacklist"))
                ),
                sheets=[worksheet.title],
            )
            if entry.expires_at and entry.expires_at < utcnow():
                entry.status = "EXPIRED"

            existing = found.get(key)
            if existing is None:
                found[key] = entry
            else:
                _merge_entry(existing, entry)

    workbook.close()
    if not sheet_names:
        warnings.append(
            "Tidak ada sheet dengan kolom 'Proxy Command'/'Proxy IP'. "
            "Format yang didukung: export Loki atau tab Pool."
        )
    return ParsedFile(
        filename=filename,
        entries=list(found.values()),
        skipped=skipped,
        sheets=sheet_names,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# Simpan ke database
# --------------------------------------------------------------------------
def import_parsed(session: Session, parsed: ParsedFile) -> dict:
    now = utcnow()
    report = {
        "filename": parsed.filename,
        "sheets": parsed.sheets,
        "seen": len(parsed.entries),
        "created": 0,
        "updated": 0,
        "skipped": parsed.skipped,
        "blacklisted": 0,
        "warnings": list(parsed.warnings),
    }

    if parsed.entries:
        by_key = {
            item.endpoint_key: item
            for item in session.scalars(
                select(PoolEntry).where(
                    PoolEntry.endpoint_key.in_([entry.key for entry in parsed.entries])
                )
            ).all()
        }
    else:
        by_key = {}

    for item in parsed.entries:
        code, state, city, _raw = _split_region(item.country_region)
        entry = by_key.get(item.key)
        created = entry is None
        if created:
            entry = PoolEntry(endpoint_key=item.key, host=item.host, port=item.port,
                              username=item.username)
            session.add(entry)
            by_key[item.key] = entry
            report["created"] += 1
        else:
            report["updated"] += 1

        entry.endpoint_enc = encrypt(item.endpoint)
        entry.endpoint_hint = proxy.mask(item.endpoint)
        entry.host = item.host
        entry.port = item.port
        entry.username = item.username
        entry.ip_address = item.ip_address or entry.ip_address
        entry.country_region = item.country_region or entry.country_region
        entry.country_code = code or entry.country_code
        entry.region_state = state or entry.region_state
        entry.city = city or entry.city
        entry.asn = item.asn or entry.asn
        entry.status = item.status if item.status != "UNKNOWN" else entry.status
        entry.usage_at = item.usage_at or entry.usage_at
        entry.expires_at = item.expires_at or entry.expires_at
        entry.account_name = item.account_name or entry.account_name
        entry.group_name = item.group_name or entry.group_name
        entry.local_ip = item.local_ip or entry.local_ip
        entry.blacklisted = bool(entry.blacklisted or item.blacklisted)
        entry.enabled = True
        entry.source_file = parsed.filename or entry.source_file
        entry.last_seen = now
        if created:
            entry.first_seen = now
        if entry.blacklisted:
            report["blacklisted"] += 1

    record = PoolImport(
        filename=parsed.filename,
        source_format=",".join(parsed.sheets)[:32],
        entries_seen=report["seen"],
        created=report["created"],
        updated=report["updated"],
        skipped=report["skipped"],
        blacklisted=report["blacklisted"],
        report_json=json.dumps(report, ensure_ascii=False)[:20000],
        imported_at=now,
    )
    session.add(record)
    session.commit()
    report["import_id"] = record.id
    return report


# --------------------------------------------------------------------------
# Assignment port
# --------------------------------------------------------------------------
def is_expired(entry: PoolEntry, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if entry.status == "EXPIRED":
        return True
    expires = _aware(entry.expires_at)
    return bool(expires and expires <= now)


def entries_usable(entry: PoolEntry, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if not entry.enabled or entry.blacklisted:
        return False
    if entry.status in {"EXPIRED", "OFFLINE"}:
        return False
    quarantined = _aware(entry.quarantine_until)
    if quarantined and quarantined > now:
        return False
    expires = _aware(entry.expires_at)
    if expires and expires <= now:
        return False
    return True


# Karantina sesi yang gagal: 5 menit, lalu 1 jam, 6 jam, 24 jam.
_QUARANTINE_STEPS = (300, 3600, 6 * 3600, 24 * 3600)


def mark_entry_ok(entry: PoolEntry, now: datetime | None = None) -> None:
    entry.fail_count = 0
    entry.last_ok_at = now or utcnow()
    entry.quarantine_until = None


def mark_entry_fail(entry: PoolEntry, now: datetime | None = None) -> None:
    now = now or utcnow()
    entry.fail_count = (entry.fail_count or 0) + 1
    entry.last_fail_at = now
    delay = _QUARANTINE_STEPS[min(entry.fail_count - 1, len(_QUARANTINE_STEPS) - 1)]
    entry.quarantine_until = now + timedelta(seconds=delay)


def ensure_ports(
    session: Session,
    machine: str,
    start_port: int,
    num_ports: int,
    rules: list[dict] | None = None,
) -> int:
    existing = {
        row.port: row
        for row in session.scalars(
            select(PoolPort).where(PoolPort.machine == machine)
        ).all()
    }
    created = 0
    for port in range(start_port, start_port + num_ports):
        row = existing.get(port)
        if row is None:
            row = PoolPort(machine=machine, port=port, group_name=group_for_port(port, rules))
            session.add(row)
            created += 1
        else:
            row.group_name = group_for_port(port, rules) or row.group_name
    if created:
        session.commit()
    return created


def auto_assign(
    session: Session,
    machine: str,
    start_port: int,
    num_ports: int,
    max_ports_per_ip: int = 2,
    rules: list[dict] | None = None,
    keep_existing: bool = True,
) -> dict:
    rules = rules if rules is not None else DEFAULT_POOL_CONFIG["group_rules"]
    ensure_ports(session, machine, start_port, num_ports, rules)

    now = utcnow()
    entries = session.scalars(select(PoolEntry)).all()
    usable = {entry.id: entry for entry in entries if entries_usable(entry, now)}

    ports = session.scalars(
        select(PoolPort)
        .where(PoolPort.machine == machine, PoolPort.port >= start_port,
               PoolPort.port < start_port + num_ports)
        .order_by(PoolPort.port)
    ).all()

    used_entry: set[int] = set()
    ip_usage: dict[str, int] = {}
    changed = 0

    def take(entry: PoolEntry) -> None:
        used_entry.add(entry.id)
        if entry.ip_address:
            ip_usage[entry.ip_address] = ip_usage.get(entry.ip_address, 0) + 1

    # Pass 1: pertahankan assignment lama yang masih valid.
    for row in ports:
        entry = usable.get(row.entry_id) if row.entry_id else None
        if entry is not None and keep_existing and entry.id not in used_entry:
            take(entry)
            continue
        row.entry_id = None

    # Pass 2: pilih entry untuk port kosong.
    for row in ports:
        if row.entry_id:
            continue
        group = group_for_port(row.port, rules) or row.group_name
        candidates = [
            entry
            for entry in usable.values()
            if entry.id not in used_entry
            and (not entry.ip_address or ip_usage.get(entry.ip_address, 0) < max_ports_per_ip)
        ]
        if not candidates:
            continue

        def score(entry: PoolEntry):
            return _entry_score(entry, group, ip_usage)

        chosen = min(candidates, key=score)
        row.entry_id = chosen.id
        row.assigned_at = now
        row.last_error = None
        take(chosen)
        changed += 1

    session.commit()
    return {
        "machine": machine,
        "ports": len(ports),
        "changed": changed,
        "assigned": sum(1 for row in ports if row.entry_id),
        "unassigned": sum(1 for row in ports if not row.entry_id),
        "usable_entries": len(usable),
    }


def _entry_score(
    entry: PoolEntry,
    group: str | None,
    ip_usage: dict[str, int],
) -> tuple:
    group_rank = 2
    if group and entry.group_name:
        group_rank = 0 if entry.group_name == group else 1
    verified = 0 if entry.last_ok_at else 1
    expiry = _aware(entry.expires_at).timestamp() if entry.expires_at else 0.0
    return (
        group_rank,
        verified,
        ip_usage.get(entry.ip_address or "", 0),
        entry.fail_count or 0,
        -expiry,
        entry.id,
    )


def rotate_port(
    session: Session,
    machine: str,
    port: int,
    failed_entry_id: int | None = None,
    rules: list[dict] | None = None,
    max_ports_per_ip: int = 2,
) -> int | None:
    """Ganti assignment satu port ke sesi lain yang sehat.

    Sesi yang gagal dikarantina (lihat mark_entry_fail). Tidak commit; pemanggil
    yang mengatur transaksi.
    """
    now = utcnow()
    row = session.scalar(
        select(PoolPort).where(PoolPort.machine == machine, PoolPort.port == port)
    )
    if row is None:
        return None

    if failed_entry_id is not None:
        bad = session.get(PoolEntry, failed_entry_id)
        if bad is not None:
            mark_entry_fail(bad, now)

    rows = session.scalars(
        select(PoolPort).where(PoolPort.machine == machine)
    ).all()
    used = {item.entry_id for item in rows if item.entry_id and item.port != port}
    ip_usage: dict[str, int] = {}
    for item in rows:
        if item.port == port or not item.entry_id:
            continue
        entry = item.entry
        if entry is not None and entry.ip_address:
            ip_usage[entry.ip_address] = ip_usage.get(entry.ip_address, 0) + 1

    entries = session.scalars(select(PoolEntry)).all()
    group = group_for_port(port, rules) or row.group_name
    candidates = [
        entry
        for entry in entries
        if entries_usable(entry, now)
        and entry.id not in used
        and (not entry.ip_address or ip_usage.get(entry.ip_address, 0) < max_ports_per_ip)
    ]
    if not candidates:
        row.entry_id = None
        row.assigned_at = now
        row.last_ok_at = None
        return None

    chosen = min(candidates, key=lambda entry: _entry_score(entry, group, ip_usage))
    row.entry_id = chosen.id
    row.group_name = group or row.group_name
    row.assigned_at = now
    row.last_ok_at = None
    row.latency_ms = None
    row.last_error = None
    return chosen.id


def assign_port(session: Session, machine: str, port: int, entry_id: int | None) -> None:
    row = session.scalar(
        select(PoolPort).where(PoolPort.machine == machine, PoolPort.port == port)
    )
    if row is None:
        row = PoolPort(machine=machine, port=port)
        session.add(row)
    row.entry_id = entry_id
    row.assigned_at = utcnow()
    if entry_id is None:
        row.last_error = None
    session.commit()


def port_payload(row: PoolPort, now: datetime | None = None) -> dict | None:
    now = now or utcnow()
    entry = row.entry
    if entry is None or not entries_usable(entry, now):
        return None
    parsed = proxy.parse_endpoint(decrypt(entry.endpoint_enc))
    if parsed is None:
        return None
    return {
        "port": row.port,
        "group": row.group_name,
        "ip": entry.ip_address,
        "region": entry.country_region,
        "country_code": entry.country_code,
        "state": entry.region_state,
        "city": entry.city,
        "account": entry.account_name,
        "expires_at": _aware(entry.expires_at).isoformat(timespec="seconds")
        if entry.expires_at
        else None,
        "upstream": {
            "scheme": "http",
            "host": parsed["host"],
            "port": parsed["port"],
            "username": parsed["user"],
            "password": parsed["password"],
        },
    }


def entries_for_api(
    session: Session,
    machine: str,
    group: str | None = None,
) -> list[dict]:
    stmt = (
        select(PoolPort)
        .where(PoolPort.machine == machine, PoolPort.enabled == True)  # noqa: E712
        .order_by(PoolPort.port)
    )
    rows = session.scalars(stmt).all()
    now = utcnow()
    payload: list[dict] = []
    for row in rows:
        if group and (row.group_name or "") != group:
            continue
        item = port_payload(row, now)
        if item is not None:
            payload.append(item)
    return payload


def apply_report(session: Session, machine: str, ports: list[dict]) -> dict:
    cfg = load_pool_config()
    rules = cfg.get("group_rules") or DEFAULT_POOL_CONFIG["group_rules"]
    max_per_ip = int(cfg.get("max_ports_per_ip", 2))

    rows = {
        row.port: row
        for row in session.scalars(
            select(PoolPort).where(PoolPort.machine == machine)
        ).all()
    }
    now = utcnow()
    updated = 0
    failed: list[tuple[int, int]] = []
    for item in ports:
        try:
            port = int(item.get("port"))
        except (TypeError, ValueError):
            continue
        row = rows.get(port)
        if row is None:
            continue
        # Abaikan laporan dari assignment lama yang sudah diganti web.
        reported_user = str(item.get("upstream_user") or "")
        current_user = row.entry.username if row.entry is not None else ""
        if reported_user and current_user and reported_user != current_user:
            continue

        row.last_report_at = now
        if item.get("ok"):
            row.last_ok_at = now
            row.last_error = None
            if row.entry is not None:
                mark_entry_ok(row.entry, now)
        elif item.get("error"):
            row.last_error = str(item["error"])[:240]
            if row.entry_id:
                failed.append((port, row.entry_id))
        for field_name, attr in (
            ("latency_ms", "latency_ms"),
            ("active_conns", "active_conns"),
            ("rx_bytes", "rx_bytes"),
            ("tx_bytes", "tx_bytes"),
        ):
            value = item.get(field_name)
            if isinstance(value, (int, float)):
                setattr(row, attr, int(value))
        updated += 1

    rotated = 0
    for port, entry_id in failed:
        if rotate_port(session, machine, port, entry_id, rules, max_per_ip) is not None:
            rotated += 1

    session.commit()
    return {"updated": updated, "rotated": rotated}


def summary(session: Session, now: datetime | None = None) -> dict:
    now = now or utcnow()
    entries = session.scalars(select(PoolEntry)).all()
    ports = session.scalars(select(PoolPort)).all()
    active = [entry for entry in entries if entries_usable(entry, now)]
    expiring = [
        entry
        for entry in active
        if _aware(entry.expires_at)
        and (_aware(entry.expires_at) - now).total_seconds() <= 24 * 3600
    ]
    return {
        "entries": len(entries),
        "active": len(active),
        "offline": sum(1 for entry in entries if entry.status == "OFFLINE"),
        "expired": sum(
            1
            for entry in entries
            if entry.status == "EXPIRED"
            or (_aware(entry.expires_at) and _aware(entry.expires_at) <= now)
        ),
        "expiring_24h": len(expiring),
        "blacklisted": sum(1 for entry in entries if entry.blacklisted),
        "verified": sum(1 for entry in entries if entry.last_ok_at),
        "quarantined": sum(
            1
            for entry in entries
            if _aware(entry.quarantine_until) and _aware(entry.quarantine_until) > now
        ),
        "regions": len({entry.country_region for entry in entries if entry.country_region}),
        "ports": len(ports),
        "ports_assigned": sum(1 for row in ports if row.entry_id),
        "ports_healthy": sum(1 for row in ports if row.last_ok_at is not None),
    }
