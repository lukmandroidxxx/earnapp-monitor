from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from . import config, jobs, login_helper, pool_store
from .crypto import decrypt
from .db import SessionLocal, get_session, init_db
from .models import (
    Account,
    CookieMeta,
    DeviceUsage,
    EarnDevice,
    PoolEntry,
    PoolImport,
    PoolPort,
    ProxySlot,
    SlotCheck,
    Snapshot,
    utcnow,
)

log = logging.getLogger("earnapp-monitor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

templates = Jinja2Templates(directory=str(config.ROOT / "app" / "templates"))

_STATUS_STYLES = {
    "OK": "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
    "DEGRADED": "bg-amber-500/15 text-amber-300 ring-amber-500/30",
    "ERROR": "bg-rose-500/15 text-rose-300 ring-rose-500/30",
    "BLOCKED": "bg-rose-500/15 text-rose-300 ring-rose-500/30",
    "PAUSED": "bg-slate-500/15 text-slate-300 ring-slate-500/30",
    "UNCHECKED": "bg-sky-500/15 text-sky-300 ring-sky-500/30",
    "UNHEALTHY": "bg-amber-500/15 text-amber-300 ring-amber-500/30",
    "BLACKLIST": "bg-slate-500/15 text-slate-400 ring-slate-500/30",
}
_STATUS_LABELS = {
    "OK": "SEHAT",
    "DEGRADED": "SEBAGIAN",
    "ERROR": "BERMASALAH",
    "BLOCKED": "BENTROK IP",
    "PAUSED": "NONAKTIF",
    "UNCHECKED": "BELUM DICEK",
    "UNHEALTHY": "PERLU DIGANTI",
    "BLACKLIST": "BLACKLIST",
}
_STATUS_COLORS = {
    "OK": "#34d399",
    "DEGRADED": "#fbbf24",
    "ERROR": "#fb7185",
    "BLOCKED": "#f43f5e",
    "UNHEALTHY": "#fb923c",
    "PAUSED": "#94a3b8",
    "UNCHECKED": "#38bdf8",
    "BLACKLIST": "#475569",
}
_AUTH_STYLES = {
    "OK": "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
    "EXPIRED": "bg-rose-500/15 text-rose-300 ring-rose-500/30",
    "ERROR": "bg-amber-500/15 text-amber-300 ring-amber-500/30",
    "NOT_SET": "bg-slate-500/15 text-slate-400 ring-slate-500/30",
}
_AUTH_LABELS = {
    "OK": "LOGIN OK",
    "EXPIRED": "COOKIE EXPIRED",
    "ERROR": "LOGIN ERROR",
    "NOT_SET": "BELUM ADA COOKIE",
}


def _badge(value: str, styles: dict[str, str], labels: dict[str, str]) -> Markup:
    css = styles.get(value, "bg-slate-500/15 text-slate-300 ring-slate-500/30")
    text = labels.get(value, (value or "-").replace("_", " "))
    return Markup(f'<span class="px-2 py-0.5 rounded-md text-xs font-medium ring-1 {css}">{escape(text)}</span>')


def _auth_badge(account) -> Markup:  # noqa: ANN001
    """Badge login; bedakan 'belum ada cookie' dengan 'cookie ada tapi belum dites'."""
    if isinstance(account, str):
        return _badge(account or "NOT_SET", _AUTH_STYLES, _AUTH_LABELS)
    status = getattr(account, "auth_status", None) or "NOT_SET"
    if status == "NOT_SET" and getattr(account, "cookie_enc", None):
        return Markup(
            '<span class="px-2 py-0.5 rounded-md text-xs font-medium ring-1 '
            'bg-sky-500/15 text-sky-300 ring-sky-500/30">COOKIE TERSIMPAN · BELUM DITES</span>'
        )
    return _badge(status, _AUTH_STYLES, _AUTH_LABELS)


templates.env.globals["status_badge"] = lambda value: _badge(value or "-", _STATUS_STYLES, _STATUS_LABELS)
templates.env.globals["auth_badge"] = _auth_badge
templates.env.globals["STATUS_LABELS"] = _STATUS_LABELS


def usage_class(value: int | None) -> str:
    count = value or 0
    if count <= 5:
        return "text-emerald-300"
    if count <= 30:
        return "text-slate-200"
    if count <= 60:
        return "text-amber-300"
    return "text-rose-300"


def uptime_class(value: int | None) -> str:
    if value is None:
        return "text-slate-500"
    if value >= 95:
        return "text-emerald-300"
    if value >= 60:
        return "text-amber-300"
    return "text-rose-300"


templates.env.globals["usage_class"] = usage_class
templates.env.globals["uptime_class"] = uptime_class


def _fmt_dt(value):
    if not value:
        return "-"
    return value.strftime("%d/%m %H:%M")


def _fmt_money(value):
    if value is None:
        return "-"
    return f"${value:,.4f}"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["money"] = _fmt_money
templates.env.filters["duration"] = jobs.format_duration

_last_run = {"sync": 0.0, "poll": 0.0}


def _with_session(func, *args):
    session = SessionLocal()
    try:
        return func(session, *args)
    finally:
        session.close()


def _run_sync() -> dict:
    return _with_session(jobs.sync_from_sheet)


def _run_poll() -> dict:
    return _with_session(jobs.poll_slots)


def _tick() -> None:
    now = time.monotonic()

    if config.settings.auto_sync and now - _last_run["sync"] >= config.settings.sync_interval:
        _last_run["sync"] = now
        try:
            log.info("sync sheet selesai: %s", _run_sync())
        except Exception:  # noqa: BLE001
            log.exception("sync gagal")

    if config.settings.auto_poll and now - _last_run["poll"] >= config.settings.poll_interval:
        _last_run["poll"] = now
        try:
            log.info("poll slot selesai: %s", _run_poll())
        except Exception:  # noqa: BLE001
            log.exception("poll gagal")


async def _background_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_tick)
        except Exception:  # noqa: BLE001
            log.exception("loop error")
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    task = asyncio.create_task(_background_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="EarnApp Multi-Account Monitor", lifespan=lifespan)

# Route yang tetap bisa diakses tanpa Basic Auth: dipakai health check dan
# keepalive (UptimeRobot/ping) supaya service gratis tidak tidur.
# /api/pool* tetap butuh token agent sendiri (bukan Basic Auth browser).
PUBLIC_PATHS = {"/ping", "/healthz", "/api/pool", "/api/pool/report"}


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):  # noqa: ANN001, ANN201
    """Kunci dashboard dengan HTTP Basic auth kalau DASHBOARD_PASSWORD diisi.

    Wajib dipakai sebelum dashboard diekspos ke internet lewat tunnel.
    """
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    password = config.settings.dashboard_password
    if not password:
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        except Exception:  # noqa: BLE001
            raw = ""
        user, sep, supplied = raw.partition(":")
        if (
            sep
            and hmac.compare_digest(user, config.settings.dashboard_user)
            and hmac.compare_digest(supplied, password)
        ):
            return await call_next(request)

    return Response(
        "Login required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="EarnApp Monitor", charset="UTF-8"'},
    )


@app.middleware("http")
async def no_store_html(request: Request, call_next):  # noqa: ANN001, ANN201
    """Jangan cache halaman HTML supaya status cookie tidak terlihat basi."""
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def db_session() -> Session:
    session = get_session()
    try:
        yield session
    finally:
        session.close()


def _redirect(path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    params = []
    if msg:
        params.append(f"msg={quote(msg)}")
    if err:
        params.append(f"err={quote(err)}")
    if params:
        path += ("&" if "?" in path else "?") + "&".join(params)
    return RedirectResponse(url=path, status_code=303)


def _account_stats(account: Account) -> dict:
    slots = [slot for slot in account.slots if slot.enabled and not slot.blacklisted]
    counts = [slot.usage_count for slot in slots]
    return {
        "total": len(account.slots),
        "active": len(slots),
        "ok": sum(1 for slot in slots if slot.status == "OK"),
        "problems": sum(1 for slot in slots if slot.status in {"UNHEALTHY", "BLOCKED"}),
        "blacklist": sum(1 for slot in account.slots if slot.blacklisted),
        "avg_usage": int(sum(counts) / len(counts)) if counts else 0,
        "max_usage": max(counts, default=0),
        "regions": len({slot.region for slot in slots if slot.region}),
        "last_check": max((slot.last_check for slot in slots if slot.last_check), default=None),
    }


def _problem_rows(session: Session, limit: int | None = None):
    stmt = (
        select(ProxySlot, Account.name)
        .join(Account)
        .where(
            ProxySlot.enabled == True,  # noqa: E712
            ProxySlot.status.in_(["UNHEALTHY", "BLOCKED"]),
        )
        .order_by(ProxySlot.status, ProxySlot.usage_count.desc())
    )
    if limit:
        stmt = stmt.limit(limit)
    return session.execute(stmt).all()


def _uptime_map(session: Session, slot_ids: list[int], hours: int = 24) -> dict[int, int | None]:
    if not slot_ids:
        return {}
    since = utcnow() - timedelta(hours=hours)
    rows = session.execute(
        select(
            SlotCheck.slot_id,
            func.count(SlotCheck.id),
            func.sum(case((SlotCheck.ok == True, 1), else_=0)),  # noqa: E712
        )
        .where(SlotCheck.slot_id.in_(slot_ids), SlotCheck.created_at >= since)
        .group_by(SlotCheck.slot_id)
    ).all()
    result: dict[int, int | None] = {}
    for slot_id, total, ok_count in rows:
        result[slot_id] = int((ok_count or 0) * 100 / total) if total else None
    return result


def _hours(ms: int | float | None) -> float:
    return round((ms or 0) / 3_600_000, 2)


def _day_label(day: str) -> str:
    try:
        value = datetime.strptime(day, "%Y-%m-%d")
        return value.strftime("%d/%m")
    except ValueError:
        return day


def _earnapp_overview(session: Session, days: int = 7) -> dict:
    """Agregasi data EarnApp (device, usage, banned) untuk grafik dashboard."""
    accounts = session.scalars(select(Account).order_by(Account.name)).all()
    devices = session.scalars(select(EarnDevice).order_by(EarnDevice.title)).all()

    today = datetime.now(timezone.utc).date()
    day_keys = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    today_key = day_keys[-1]
    cutoff = day_keys[0]

    usage_rows = session.execute(
        select(DeviceUsage).where(DeviceUsage.day >= cutoff)
    ).scalars().all()

    usage_by_acc_day: dict[int, dict[str, int]] = {account.id: {} for account in accounts}
    usage_by_uuid: dict[str, dict[str, int]] = {}
    for row in usage_rows:
        usage_by_acc_day.setdefault(row.account_id, {})
        usage_by_acc_day[row.account_id][row.day] = (
            usage_by_acc_day[row.account_id].get(row.day, 0) + row.ms
        )
        usage_by_uuid.setdefault(row.device_uuid, {})[row.day] = row.ms

    devices_by_acc: dict[int, list[EarnDevice]] = {account.id: [] for account in accounts}
    for device in devices:
        devices_by_acc.setdefault(device.account_id, []).append(device)

    total = len(devices)
    online = sum(1 for device in devices if device.online)
    banned = sum(1 for device in devices if device.banned_reason)
    offline = total - online

    per_account = []
    for account in accounts:
        account_devices = devices_by_acc.get(account.id, [])
        acc_usage = usage_by_acc_day.get(account.id, {})
        per_account.append(
            {
                "id": account.id,
                "name": account.name,
                "auth_status": account.auth_status,
                "has_cookie": bool(account.cookie_enc),
                "devices": len(account_devices),
                "online": sum(1 for device in account_devices if device.online),
                "banned": sum(1 for device in account_devices if device.banned_reason),
                "today": _hours(acc_usage.get(today_key, 0)),
                "week": _hours(sum(acc_usage.values())),
                "balance": account.balance,
                "lifetime": account.lifetime,
                "daily": [acc_usage.get(day, 0) for day in day_keys],
            }
        )

    daily_total = []
    for day in day_keys:
        daily_total.append(sum(account_usage.get(day, 0) for account_usage in usage_by_acc_day.values()))

    slot_ips = {
        account.id: {slot.detected_ip for slot in account.slots if slot.detected_ip}
        for account in accounts
    }
    banned_devices = []
    for device in sorted(
        (item for item in devices if item.banned_reason),
        key=lambda item: (item.account.name if item.account else "", item.title),
    ):
        account = device.account
        banned_devices.append(
            {
                "id": device.id,
                "uuid": device.uuid,
                "title": device.title,
                "account_id": account.id if account else 0,
                "account_name": account.name if account else "-",
                "reason": device.banned_reason,
                "ip": device.banned_ip,
                "ip_in_pool": bool(
                    device.banned_ip and device.banned_ip in slot_ips.get(account.id, set())
                )
                if account
                else False,
                "last_seen": device.last_seen,
                "online": device.online,
                "earned_total": device.earned_total,
                "usage_today": _hours(usage_by_uuid.get(device.uuid, {}).get(today_key)),
            }
        )

    synced_at = max((device.updated_at for device in devices), default=None)
    device_cards = []
    for device in sorted(devices, key=lambda item: (item.account_id, item.title)):
        usage = usage_by_uuid.get(device.uuid, {})
        device_cards.append(
            {
                "id": device.id,
                "uuid": device.uuid,
                "account_id": device.account_id,
                "title": device.title,
                "online": device.online,
                "online_known": device.online_known,
                "banned_reason": device.banned_reason,
                "banned_ip": device.banned_ip,
                "country": device.country,
                "rate": device.rate,
                "earned": device.earned,
                "earned_total": device.earned_total,
                "last_seen": device.last_seen,
                "usage_today": _hours(usage.get(today_key)),
                "usage_week": _hours(sum(usage.get(day, 0) for day in day_keys)),
            }
        )

    return {
        "accounts": per_account,
        "devices": device_cards,
        "total": total,
        "online": online,
        "offline": offline,
        "banned": banned,
        "balance": sum(account.balance or 0 for account in accounts),
        "lifetime": sum(account.lifetime or 0 for account in accounts),
        "today_hours": _hours(sum(daily_total[-1:])),
        "week_hours": _hours(sum(daily_total)),
        "day_keys": day_keys,
        "day_labels": [_day_label(day) for day in day_keys],
        "daily_total_hours": [_hours(value) for value in daily_total],
        "synced_at": synced_at,
        "banned_devices": banned_devices,
        "usage_by_uuid": usage_by_uuid,
        "today_key": today_key,
    }


def _device_rows(account: Account, overview: dict) -> list[dict]:
    """Baris device untuk halaman detail, termasuk pemakaian & bentrok IP banned."""
    usage_by_uuid = overview["usage_by_uuid"]
    today_key = overview["today_key"]
    slot_ips = {slot.detected_ip for slot in account.slots if slot.detected_ip}
    rows = []
    for device in sorted(
        account.devices,
        key=lambda item: (item.banned_reason is None, item.title),
    ):
        usage = usage_by_uuid.get(device.uuid, {})
        week_ms = sum(usage.get(day, 0) for day in overview["day_keys"])
        rows.append(
            {
                "device": device,
                "usage_today": _hours(usage.get(today_key)),
                "usage_week": _hours(week_ms),
                "ip_in_pool": bool(device.banned_ip and device.banned_ip in slot_ips),
            }
        )
    return rows


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, view: str = "tabel", session: Session = Depends(db_session)):
    accounts = session.scalars(select(Account).order_by(Account.name)).all()
    stats = {account.id: _account_stats(account) for account in accounts}

    total_slots = sum(item["total"] for item in stats.values())
    healthy_slots = sum(item["ok"] for item in stats.values())
    authed = sum(1 for account in accounts if account.auth_status == "OK")
    problems = _problem_rows(session)
    problem_total = len(problems)

    active_slots = [
        slot
        for account in accounts
        for slot in account.slots
        if slot.enabled and not slot.blacklisted
    ]
    status_counts = {key: 0 for key in _STATUS_LABELS}
    for slot in active_slots:
        status_counts[slot.status] = status_counts.get(slot.status, 0) + 1

    status_chart = [
        {
            "key": key,
            "label": _STATUS_LABELS.get(key, key),
            "count": count,
            "color": _STATUS_COLORS.get(key, "#64748b"),
        }
        for key, count in status_counts.items()
        if count > 0
    ]

    usage_by_account = [
        {
            "name": account.name,
            "avg": stats[account.id]["avg_usage"],
            "max": stats[account.id]["max_usage"],
            "ok": stats[account.id]["ok"],
            "total": stats[account.id]["active"],
        }
        for account in accounts
    ]

    heatmap = [
        {
            "id": account.id,
            "name": account.name,
            "slots": sorted(
                (slot for slot in account.slots if slot.enabled and not slot.blacklisted),
                key=lambda slot: (slot.status != "OK", slot.usage_count),
            ),
        }
        for account in accounts
    ]

    health_pct = round(healthy_slots * 100 / total_slots) if total_slots else 0
    health_known = any(slot.status != "UNCHECKED" for slot in active_slots)

    since = utcnow() - timedelta(days=7)
    rows = session.execute(
        select(SlotCheck.slot_id, SlotCheck.ok, SlotCheck.created_at)
        .where(SlotCheck.created_at >= since)
        .order_by(SlotCheck.created_at)
    ).all()
    per_day: dict[str, dict[int, bool]] = {}
    for slot_id, ok, created_at in rows:
        per_day.setdefault(created_at.strftime("%Y-%m-%d"), {})[slot_id] = bool(ok)
    labels = sorted(per_day)
    values = [sum(1 for ok in per_day[day].values() if ok) for day in labels]

    earn = _earnapp_overview(session)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "accounts": accounts,
            "stats": stats,
            "total_slots": total_slots,
            "healthy_slots": healthy_slots,
            "authed": authed,
            "problems": problems[:20],
            "problem_total": problem_total,
            "chart_labels": labels,
            "chart_values": values,
            "status_counts": status_counts,
            "status_chart": status_chart,
            "usage_by_account": usage_by_account,
            "heatmap": heatmap,
            "health_pct": health_pct,
            "health_known": health_known,
            "earn": earn,
            "view": "grafik" if view == "grafik" else "tabel",
            "last_sync": jobs.load_last_sync(),
            "settings": config.settings,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
        },
    )


@app.get("/slots", response_class=HTMLResponse)
def slots_page(
    request: Request,
    account: int | None = None,
    status: str = "",
    q: str = "",
    min_usage: int = 0,
    session: Session = Depends(db_session),
):
    stmt = select(ProxySlot, Account.name).join(Account)
    if account:
        stmt = stmt.where(ProxySlot.account_id == account)
    if status:
        stmt = stmt.where(ProxySlot.status == status)
    if min_usage:
        stmt = stmt.where(ProxySlot.usage_count >= min_usage)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                ProxySlot.detected_ip.like(like),
                ProxySlot.declared_ip_raw.like(like),
                ProxySlot.region.like(like),
                ProxySlot.endpoint_hint.like(like),
                Account.name.like(like),
            )
        )
    stmt = stmt.order_by(ProxySlot.usage_count.desc(), Account.name)
    rows = session.execute(stmt).all()

    slots_for_uptime = [slot.id for slot, _name in rows]
    uptime = _uptime_map(session, slots_for_uptime)

    accounts = session.scalars(select(Account).order_by(Account.name)).all()
    return templates.TemplateResponse(
        request,
        "slots.html",
        {
            "rows": rows,
            "accounts": accounts,
            "uptime": uptime,
            "filters": {"account": account, "status": status, "q": q, "min_usage": min_usage},
            "settings": config.settings,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
        },
    )


@app.get("/devices", response_class=HTMLResponse)
def devices_page(
    request: Request,
    account: int | None = None,
    status: str = "",
    session: Session = Depends(db_session),
):
    earn = _earnapp_overview(session)
    accounts = session.scalars(select(Account).order_by(Account.name)).all()
    devices = session.scalars(
        select(EarnDevice).order_by(EarnDevice.account_id, EarnDevice.title)
    ).all()
    if account:
        devices = [device for device in devices if device.account_id == account]
    if status == "banned":
        devices = [device for device in devices if device.banned_reason]
    elif status == "online":
        devices = [device for device in devices if device.online]
    elif status == "offline":
        devices = [device for device in devices if not device.online]

    rows = []
    for device in devices:
        usage = earn["usage_by_uuid"].get(device.uuid, {})
        rows.append(
            {
                "device": device,
                "account": device.account,
                "usage_today": _hours(usage.get(earn["today_key"])),
                "usage_week": _hours(sum(usage.get(day, 0) for day in earn["day_keys"])),
            }
        )

    return templates.TemplateResponse(
        request,
        "devices.html",
        {
            "rows": rows,
            "accounts": accounts,
            "earn": earn,
            "filters": {"account": account, "status": status},
            "settings": config.settings,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
        },
    )


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(account_id: int, request: Request, session: Session = Depends(db_session)):
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan")

    slots = sorted(
        account.slots,
        key=lambda slot: (slot.status == "OK", not slot.enabled, slot.usage_count),
    )
    uptime = _uptime_map(session, [slot.id for slot in slots])
    checks = session.execute(
        select(SlotCheck, ProxySlot.endpoint_hint)
        .join(ProxySlot, SlotCheck.slot_id == ProxySlot.id)
        .where(ProxySlot.account_id == account_id)
        .order_by(SlotCheck.created_at.desc())
        .limit(40)
    ).all()
    snapshots = session.scalars(
        select(Snapshot)
        .where(Snapshot.account_id == account_id)
        .order_by(Snapshot.created_at.desc())
        .limit(20)
    ).all()

    earn = _earnapp_overview(session)
    device_rows = _device_rows(account, earn)
    earn_account = next(
        (item for item in earn["accounts"] if item["id"] == account_id), None
    )

    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "account": account,
            "stats": _account_stats(account),
            "slots": slots,
            "uptime": uptime,
            "checks": checks,
            "snapshots": snapshots,
            "device_rows": device_rows,
            "earn_account": earn_account,
            "earn": earn,
            "login": login_helper.status(account_id),
            "login_enabled": config.settings.login_helper,
            "settings": config.settings,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
        },
    )


@app.post("/sync")
def trigger_sync():
    report = _run_sync()
    if report.get("errors"):
        return _redirect("/", err="; ".join(report["errors"])[:400])
    return _redirect(
        "/",
        msg=(
            f"Sync selesai: {report.get('rows', 0)} baris, "
            f"{report.get('slots_created', 0)} slot baru, "
            f"{report.get('slots_updated', 0)} diperbarui"
        ),
    )


@app.post("/check")
def trigger_check(background: BackgroundTasks):
    background.add_task(_run_poll)
    return _redirect("/", msg="Pengecekan semua slot dimulai — muat ulang halaman beberapa saat lagi.")


@app.post("/earnapp/refresh")
def trigger_earnapp_refresh(background: BackgroundTasks):
    background.add_task(_with_session, jobs.sync_earnapp_all, True)
    return _redirect(
        "/?view=grafik",
        msg="Sinkronisasi data EarnApp dimulai untuk semua akun yang punya cookie.",
    )


@app.post("/accounts/{account_id}/devices/refresh")
def refresh_account_devices(account_id: int, session: Session = Depends(db_session)):
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan")
    result = jobs.sync_earnapp(session, account, alert=False)
    if not result.get("ok"):
        reason = "; ".join(result.get("errors") or []) or "gagal sinkron"
        return _redirect(f"/accounts/{account_id}", err=reason[:400])
    warning = "; ".join(result.get("errors") or [])
    return _redirect(
        f"/accounts/{account_id}",
        msg=(
            f"Data EarnApp: {result.get('devices', 0)} device, "
            f"{result.get('online', 0)} online, {result.get('banned', 0)} banned, "
            f"saldo ${(result.get('balance') or 0):.4f}"
        ),
        err=warning[:300] if warning else None,
    )


@app.post("/accounts/{account_id}/toggle")
def toggle_account(account_id: int, session: Session = Depends(db_session)):
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan")
    account.enabled = not account.enabled
    jobs._recompute_account(account, alert=False)
    session.commit()
    return _redirect("/", msg=f"{account.name}: {'diaktifkan' if account.enabled else 'dipause'}")


@app.post("/accounts/{account_id}/cookie")
def save_cookie(
    account_id: int,
    cookie: str = Form(""),
    test: str = Form(""),
    session: Session = Depends(db_session),
):
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan")
    value = cookie.strip()
    if not value:
        return _redirect(
            f"/accounts/{account_id}",
            err=(
                "Kotak cookie kosong — cookie lama TIDAK diubah. Tempel cookie dulu, "
                "atau pakai tombol “Hapus cookie” kalau memang ingin menghapusnya."
            ),
        )
    cleaned, cookie_meta = jobs.parse_cookie_input(value)
    if not cleaned:
        return _redirect(
            f"/accounts/{account_id}",
            err=(
                "Cookie tidak dikenali atau tidak berisi cookie earnapp.com. "
                "Tempel header `Cookie` atau JSON hasil Cookie-Editor (export untuk "
                "situs earnapp.com). Cookie lama TIDAK diubah."
            ),
        )
    jobs.set_cookie(session, account, cleaned, cookies=cookie_meta)
    if test:
        ok, message = jobs.test_auth(session, account)
        return _redirect(f"/accounts/{account_id}", msg=message if ok else None, err=None if ok else message)
    count = len(jobs.cookie_names(cleaned))
    if jobs.has_auth_cookie(cleaned):
        message = (
            f"Cookie disimpan ({count} cookie, termasuk auth/oauth). "
            "Klik Test login untuk verifikasi."
        )
    else:
        message = (
            f"Cookie disimpan ({count} cookie), tapi belum ada auth/oauth-token EarnApp. "
            "Kalau Test login gagal, export ulang cookie saat sudah masuk dashboard EarnApp."
        )
    return _redirect(f"/accounts/{account_id}", msg=message)


@app.post("/accounts/{account_id}/test")
def test_account(account_id: int, session: Session = Depends(db_session)):
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan")
    ok, message = jobs.test_auth(session, account)
    return _redirect(f"/accounts/{account_id}", msg=message if ok else None, err=None if ok else message)


@app.post("/accounts/{account_id}/cookie/clear")
def clear_cookie(account_id: int, session: Session = Depends(db_session)):
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan")
    jobs.set_cookie(session, account, "")
    return _redirect(f"/accounts/{account_id}", msg="Cookie dihapus.")


@app.get("/accounts/{account_id}/cookie.json")
def export_cookie_json(account_id: int, session: Session = Depends(db_session)):
    """Unduh cookie tersimpan sebagai JSON kompatibel Cookie-Editor."""
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan")
    header = decrypt(account.cookie_enc) if account.cookie_enc else ""
    if not header:
        return _redirect(
            f"/accounts/{account_id}",
            err="Belum ada cookie untuk di-export. Jalankan login helper atau tempel cookie dulu.",
        )
    meta_row = session.get(CookieMeta, account_id)
    meta: list[dict] | None = None
    if meta_row is not None:
        try:
            meta = json.loads(decrypt(meta_row.payload_enc))
        except (ValueError, TypeError):
            meta = None
    payload_items = jobs.cookie_export_json(header, meta)
    if not payload_items:
        return _redirect(
            f"/accounts/{account_id}",
            err=(
                "Cookie tersimpan sudah kedaluwarsa semuanya. Login ulang lewat "
                "helper atau tempel cookie baru."
            ),
        )
    payload = json.dumps(payload_items, indent=2, ensure_ascii=False)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", account.name) or f"account_{account_id}"
    return Response(
        content=payload,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="earnapp-{safe_name}-cookies.json"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/accounts/{account_id}/login")
def start_login(account_id: int, session: Session = Depends(db_session)):
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan")
    ok, message = login_helper.start(account_id)
    return _redirect(
        f"/accounts/{account_id}", msg=message if ok else None, err=None if ok else message
    )


@app.post("/accounts/{account_id}/login/finish")
def finish_login(account_id: int, background: BackgroundTasks):
    ok, message = login_helper.finish(account_id)
    if ok and config.settings.device_sync:
        background.add_task(_with_session, jobs.sync_earnapp_all, False)
    return _redirect(
        f"/accounts/{account_id}", msg=message if ok else None, err=None if ok else message
    )


@app.post("/accounts/{account_id}/login/cancel")
def cancel_login(account_id: int, session: Session = Depends(db_session)):
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan")
    ok, message = login_helper.cancel(account_id)
    return _redirect(
        f"/accounts/{account_id}", msg=message if ok else None, err=None if ok else message
    )


@app.get("/accounts/{account_id}/login/status")
def login_status(account_id: int):
    payload = login_helper.status(account_id) or {
        "status": "idle",
        "active": False,
        "message": "Tidak ada sesi login.",
        "cookie_count": 0,
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/slots/{slot_id}/check")
def check_slot(slot_id: int, session: Session = Depends(db_session)):
    slot = session.get(ProxySlot, slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="Slot tidak ditemukan")
    ok, message = jobs.check_single(session, slot)
    target = f"/accounts/{slot.account_id}"
    return _redirect(target, msg=message if ok else None, err=None if ok else message)


# --------------------------------------------------------------------------
# IP pool: import xlsx, assignment port, dan API untuk agent di RDP
# --------------------------------------------------------------------------
def _require_agent_token(request: Request) -> None:
    expected = config.settings.pool_agent_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="POOL_AGENT_TOKEN belum diset di .env server",
        )
    supplied = request.headers.get("x-pool-token", "")
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        supplied = header.split(" ", 1)[1].strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="token agent salah")


def _pool_filters(entries: list[PoolEntry], now) -> list[dict]:
    buckets: dict[str, dict] = {}
    for entry in entries:
        key = entry.country_region or "(tanpa region)"
        bucket = buckets.setdefault(
            key,
            {
                "region": key,
                "total": 0,
                "active": 0,
                "expired": 0,
                "groups": set(),
                "accounts": set(),
            },
        )
        bucket["total"] += 1
        if pool_store.entries_usable(entry, now):
            bucket["active"] += 1
        elif pool_store.is_expired(entry, now):
            bucket["expired"] += 1
        if entry.group_name:
            bucket["groups"].add(entry.group_name)
        if entry.account_name:
            bucket["accounts"].add(entry.account_name)
    rows = []
    for bucket in buckets.values():
        bucket["groups"] = ", ".join(sorted(bucket["groups"])) or "-"
        bucket["accounts"] = ", ".join(sorted(bucket["accounts"])) or "-"
        rows.append(bucket)
    rows.sort(key=lambda item: (-item["total"], item["region"]))
    return rows


@app.get("/pool", response_class=HTMLResponse)
def pool_page(
    request: Request,
    q: str = "",
    region: str = "",
    group: str = "",
    status: str = "",
    session: Session = Depends(db_session),
):
    now = utcnow()
    cfg = pool_store.load_pool_config()

    stmt = select(PoolEntry)
    if region:
        stmt = stmt.where(PoolEntry.country_region == region)
    if group:
        stmt = stmt.where(PoolEntry.group_name == group)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                PoolEntry.ip_address.like(like),
                PoolEntry.username.like(like),
                PoolEntry.country_region.like(like),
                PoolEntry.account_name.like(like),
                PoolEntry.group_name.like(like),
            )
        )
    stmt = stmt.order_by(
        PoolEntry.country_region.is_(None),
        PoolEntry.country_region,
        PoolEntry.group_name.is_(None),
        PoolEntry.group_name,
        PoolEntry.account_name.is_(None),
        PoolEntry.account_name,
        PoolEntry.ip_address,
    )
    entries = session.scalars(stmt).all()

    if status == "active":
        entries = [entry for entry in entries if pool_store.entries_usable(entry, now)]
    elif status == "expired":
        entries = [entry for entry in entries if pool_store.is_expired(entry, now)]
    elif status == "offline":
        entries = [entry for entry in entries if entry.status == "OFFLINE"]
    elif status == "blacklist":
        entries = [entry for entry in entries if entry.blacklisted]
    elif status == "unassigned":
        pass

    ports = session.scalars(
        select(PoolPort).order_by(PoolPort.machine, PoolPort.port)
    ).all()
    ports_by_entry: dict[int, list[PoolPort]] = {}
    for row in ports:
        if row.entry_id:
            ports_by_entry.setdefault(row.entry_id, []).append(row)
    imports = session.scalars(
        select(PoolImport).order_by(PoolImport.imported_at.desc()).limit(8)
    ).all()
    all_entries = session.scalars(select(PoolEntry)).all()

    region_options = sorted(
        {entry.country_region for entry in all_entries if entry.country_region}
    )
    group_options = sorted({entry.group_name for entry in all_entries if entry.group_name})
    entry_rows = [
        {
            "entry": entry,
            "usable": pool_store.entries_usable(entry, now),
            "expired": pool_store.is_expired(entry, now),
        }
        for entry in entries[:800]
    ]

    return templates.TemplateResponse(
        request,
        "pool.html",
        {
            "entry_rows": entry_rows,
            "entry_total": len(entries),
            "groups": _pool_filters(all_entries, now),
            "ports": ports,
            "ports_by_entry": ports_by_entry,
            "imports": imports,
            "summary": pool_store.summary(session, now),
            "region_options": region_options,
            "group_options": group_options,
            "filters": {"q": q, "region": region, "group": group, "status": status},
            "cfg": cfg,
            "now": now,
            "settings": config.settings,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
        },
    )


@app.post("/pool/import")
async def pool_import(
    files: list[UploadFile] = File(default=[]),
    account: str = Form(default=""),
    group: str = Form(default=""),
    session: Session = Depends(db_session),
):
    files = [item for item in files if item and item.filename]
    if not files:
        return _redirect("/pool", err="Pilih minimal satu file xlsx dulu.")

    created = updated = skipped = 0
    warnings: list[str] = []
    imported = 0
    for upload in files:
        data = await upload.read()
        if not data:
            warnings.append(f"{upload.filename}: file kosong")
            continue
        try:
            parsed = pool_store.parse_xlsx(data, upload.filename or "upload.xlsx")
        except ValueError as exc:
            warnings.append(f"{upload.filename}: {exc}")
            continue
        if not parsed.entries:
            warnings.append(
                f"{upload.filename}: tidak ada sesi proxy yang bisa dibaca"
            )
            continue
        report = pool_store.import_parsed(
            session,
            parsed,
            default_account=account,
            default_group=group,
        )
        created += report["created"]
        updated += report["updated"]
        skipped += report["skipped"]
        imported += report["seen"]
        warnings.extend(f"{upload.filename}: {text}" for text in report["warnings"])

    if imported == 0:
        return _redirect("/pool", err="; ".join(warnings)[:400] or "Tidak ada data yang diimpor.")

    cfg = pool_store.load_pool_config()
    assign = pool_store.auto_assign(
        session,
        machine=str(cfg["machine"]),
        start_port=int(cfg["start_port"]),
        num_ports=int(cfg["num_ports"]),
        max_ports_per_ip=int(cfg.get("max_ports_per_ip", 2)),
        rules=cfg.get("group_rules"),
    )
    message = (
        f"Import selesai: {imported} sesi ({created} baru, {updated} diperbarui"
        f"{f', {skipped} baris dilewati' if skipped else ''}). "
        f"Assignment {assign['machine']}: {assign['assigned']}/{assign['ports']} port terisi, "
        f"{assign['changed']} berubah."
    )
    return _redirect("/pool", msg=message, err="; ".join(warnings)[:400] or None)


@app.post("/pool/assign")
def pool_assign(
    session: Session = Depends(db_session),
    machine: str = Form(""),
    start_port: int = Form(0),
    num_ports: int = Form(0),
    max_ports_per_ip: int = Form(0),
):
    cfg = pool_store.load_pool_config()
    machine = (machine or str(cfg["machine"])).strip()
    start_port = start_port or int(cfg["start_port"])
    num_ports = num_ports or int(cfg["num_ports"])
    max_ports_per_ip = max_ports_per_ip or int(cfg.get("max_ports_per_ip", 2))
    result = pool_store.auto_assign(
        session,
        machine=machine,
        start_port=start_port,
        num_ports=num_ports,
        max_ports_per_ip=max_ports_per_ip,
        rules=cfg.get("group_rules"),
    )
    return _redirect(
        "/pool",
        msg=(
            f"Assignment {machine}: {result['assigned']}/{result['ports']} port terisi "
            f"dari {result['usable_entries']} sesi aktif, {result['changed']} berubah."
        ),
    )


@app.post("/pool/assign/one")
def pool_assign_one(
    session: Session = Depends(db_session),
    machine: str = Form(""),
    port: int = Form(...),
    entry_id: str = Form(""),
):
    cfg = pool_store.load_pool_config()
    machine = (machine or str(cfg["machine"])).strip()
    value = None
    if entry_id.strip():
        try:
            value = int(entry_id)
        except ValueError:
            return _redirect("/pool", err="ID entry tidak valid.")
        if session.get(PoolEntry, value) is None:
            return _redirect("/pool", err=f"Entry #{value} tidak ditemukan.")
    pool_store.assign_port(session, machine, port, value)
    return _redirect("/pool", msg=f"Port {port} ({machine}) diperbarui.")


@app.post("/pool/entry/{entry_id}/blacklist")
def pool_toggle_blacklist(entry_id: int, session: Session = Depends(db_session)):
    entry = session.get(PoolEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry tidak ditemukan")
    entry.blacklisted = not entry.blacklisted
    session.commit()
    return _redirect(
        "/pool",
        msg=f"{entry.ip_address or entry.endpoint_hint}: "
        f"{'masuk blacklist' if entry.blacklisted else 'keluar dari blacklist'}",
    )


@app.get("/api/pool")
def api_pool(
    request: Request,
    machine: str = "",
    group: str = "",
    session: Session = Depends(db_session),
):
    _require_agent_token(request)
    cfg = pool_store.load_pool_config()
    machine = (machine or str(cfg["machine"])).strip()
    # Self-heal: agent memanggil endpoint ini tiap refresh, jadi port yang
    # kosong (entry habis/karantina) langsung diisi lagi tanpa menunggu import.
    pool_store.auto_assign(
        session,
        machine=machine,
        start_port=int(cfg["start_port"]),
        num_ports=int(cfg["num_ports"]),
        max_ports_per_ip=int(cfg.get("max_ports_per_ip", 2)),
        rules=cfg.get("group_rules"),
        keep_existing=True,
    )
    ports = pool_store.entries_for_api(session, machine, group or None)
    return JSONResponse(
        {
            "machine": machine,
            "generated_at": utcnow().isoformat(timespec="seconds"),
            "bind_ip": cfg.get("bind_ip"),
            "start_port": cfg.get("start_port"),
            "num_ports": cfg.get("num_ports"),
            "group_rules": cfg.get("group_rules", []),
            "ports": ports,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/pool/report")
async def api_pool_report(request: Request, session: Session = Depends(db_session)):
    _require_agent_token(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="body harus JSON")
    machine = str(payload.get("machine") or "").strip()
    if not machine:
        raise HTTPException(status_code=400, detail="machine wajib diisi")
    ports = payload.get("ports")
    if not isinstance(ports, list):
        raise HTTPException(status_code=400, detail="ports harus list")
    result = pool_store.apply_report(session, machine, ports)
    return JSONResponse(
        {
            "ok": True,
            "machine": machine,
            "updated": result["updated"],
            "rotated": result["rotated"],
            "at": utcnow().isoformat(timespec="seconds"),
        }
    )


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "time": utcnow().isoformat(timespec="seconds")})


@app.get("/ping")
def ping():
    """Endpoint publik super ringan untuk keepalive UptimeRobot."""
    return PlainTextResponse("pong", headers={"Cache-Control": "no-store"})

