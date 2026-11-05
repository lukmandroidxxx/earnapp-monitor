"""Login helper: buka Chrome/Edge lewat proxy akun, tangkap cookie EarnApp.

Alur:
1. Dashboard memanggil start(account_id).
2. Helper menguji slot akun dan memilih proxy yang hidup.
3. Chromium tidak mendukung SOCKS5+auth, jadi proxy upstream (SOCKS5/HTTP)
   diteruskan lewat ProxyBridge HTTP lokal di 127.0.0.1.
4. Browser dibuka dengan profil terpisah lewat bridge tersebut.
5. User login Google seperti biasa.
6. Saat jendela ditutup atau tombol "Simpan sekarang" diklik, semua cookie
   earnapp.com (termasuk httpOnly) disimpan terenkripsi lewat jobs.set_cookie().

Kredensial Google tidak pernah dilihat/diambil aplikasi ini.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config, jobs, proxy
from .crypto import decrypt
from .db import SessionLocal
from .models import Account, ProxySlot, utcnow
from .proxy_bridge import ProxyBridge

log = logging.getLogger("earnapp-monitor.login")

_ACTIVE = {"starting", "running", "saving"}
_FINAL = {"saved", "failed", "cancelled"}

# Saat tombol "Simpan sekarang" diklik sebelum cookie login muncul, tunggu
# sebentar supaya redirect OAuth sempat selesai.
_FINISH_GRACE = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class LoginSession:
    account_id: int
    status: str = "starting"
    message: str = "Menyiapkan browser…"
    started_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    cookie_count: int = 0
    slot_hint: str = ""
    exit_ip: str = ""
    error: str | None = None
    _finish: threading.Event = field(default_factory=threading.Event, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _context: object | None = field(default=None, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, status: str | None = None, message: str | None = None, **extra) -> None:
        with self._lock:
            if status is not None:
                self.status = status
            if message is not None:
                self.message = message
            self.updated_at = _now()
            for key, value in extra.items():
                setattr(self, key, value)


_sessions: dict[int, LoginSession] = {}
_registry_lock = threading.Lock()


def _public(session: LoginSession) -> dict:
    with session._lock:
        return {
            "account_id": session.account_id,
            "status": session.status,
            "message": session.message,
            "active": session.status in _ACTIVE,
            "cookie_count": session.cookie_count,
            "slot_hint": session.slot_hint,
            "exit_ip": session.exit_ip,
            "error": session.error,
            "started_at": session.started_at.isoformat(timespec="seconds"),
            "updated_at": session.updated_at.isoformat(timespec="seconds"),
        }


def status(account_id: int) -> dict | None:
    session = _sessions.get(account_id)
    return _public(session) if session else None


def start(account_id: int) -> tuple[bool, str]:
    if not config.settings.login_helper:
        return False, "Login helper dimatikan (LOGIN_HELPER=0 di .env)."

    with _registry_lock:
        existing = _sessions.get(account_id)
        if existing and existing.status in _ACTIVE:
            return False, "Sesi login akun ini masih berjalan. Selesaikan atau batalkan dulu."

        session = LoginSession(account_id=account_id)
        _sessions[account_id] = session
        thread = threading.Thread(
            target=_run, args=(session,), daemon=True, name=f"earnapp-login-{account_id}"
        )
        session._thread = thread
        thread.start()

    return True, (
        "Jendela Chrome dibuka lewat proxy akun ini. Login dengan Google, lalu "
        "tutup jendelanya atau klik “Simpan sekarang” — cookie tersimpan otomatis."
    )


def finish(account_id: int) -> tuple[bool, str]:
    session = _sessions.get(account_id)
    if not session or session.status not in _ACTIVE:
        return False, "Tidak ada sesi login aktif untuk akun ini."
    session.update("saving", "Mengambil cookie dari browser…")
    session._finish.set()
    return True, "Cookie sedang diambil dari browser…"


def cancel(account_id: int) -> tuple[bool, str]:
    session = _sessions.get(account_id)
    if not session or session.status not in _ACTIVE:
        return False, "Tidak ada sesi login aktif untuk akun ini."
    session._cancel.set()
    session._finish.set()
    return True, "Sesi login dibatalkan."


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------
_PROBE_LIMIT = 5
_PROBE_TIMEOUT = 15


def _candidate_slots(account: Account) -> list[ProxySlot]:
    """Slot aktif, yang sudah OK didahulukan, lalu yang paling jarang dipakai."""
    candidates = [s for s in account.slots if s.enabled and not s.blacklisted]
    candidates.sort(key=lambda s: (0 if s.status == "OK" else 1, s.usage_count, s.id))
    return candidates


def _probe_account(
    session: LoginSession, account: Account, db
) -> tuple[ProxySlot | None, dict | None, str | None, list[str]]:  # noqa: ANN001
    """Cari satu slot yang benar-benar hidup; simpan skema & IP keluar.

    Mengembalikan (slot, hasil_check, warning, catatan_gagal).
    """
    slots = _candidate_slots(account)
    if not slots:
        return None, None, None, []

    timeout = min(_PROBE_TIMEOUT, max(5, config.settings.request_timeout))
    notes: list[str] = []
    mismatched: tuple[ProxySlot, dict, str] | None = None

    for slot in slots[:_PROBE_LIMIT]:
        session.update(
            "starting",
            f"Menguji proxy {slot.endpoint_hint}…",
            slot_hint=slot.endpoint_hint,
        )
        result = proxy.check(decrypt(slot.endpoint_enc), "auto", timeout)
        slot.last_check = utcnow()

        if not result["ok"]:
            slot.status = "UNHEALTHY"
            slot.status_reason = result["note"]
            db.commit()
            notes.append(f"{slot.endpoint_hint}: {result['note']}")
            continue

        slot.detected_ip = result["ip"]
        slot.latency_ms = result["latency_ms"]
        slot.last_ok = utcnow()
        if result["scheme"] in proxy.VALID_SCHEMES:
            slot.scheme = result["scheme"]

        declared = (slot.declared_ip_raw or "").strip()
        if declared and result["ip"] != declared:
            slot.status = "UNHEALTHY"
            slot.status_reason = f"IP keluar {result['ip']} ≠ sheet {declared}"
            if mismatched is None:
                mismatched = (
                    slot,
                    result,
                    f"IP keluar {result['ip']} ≠ sheet {declared} — cookie mungkin "
                    "terikat ke IP lain.",
                )
            db.commit()
            continue

        slot.status = "OK"
        slot.status_reason = None
        db.commit()
        return slot, result, None, notes

    if mismatched is not None:
        return mismatched[0], mismatched[1], mismatched[2], notes

    return None, None, None, notes


def _install_scripts(context) -> None:  # noqa: ANN001
    # Kurangi jejak otomasi supaya login Google tidak langsung ditolak.
    try:
        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """
        )
    except Exception:  # noqa: BLE001
        pass


def _launch(pw, profile_dir: Path, proxy_cfg: dict):  # noqa: ANN001
    profile_dir.mkdir(parents=True, exist_ok=True)
    options = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "proxy": proxy_cfg,
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
            # Cegah lalu lintas keluar memakai IP asli (QUIC/UDP tidak lewat proxy).
            "--disable-quic",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ],
        "no_viewport": True,
        "locale": "en-US",
    }

    preferred = config.settings.login_browser
    channels: list[str | None] = []
    if preferred in {"chrome", "msedge", "chromium"}:
        channels = [None if preferred == "chromium" else preferred]
    else:
        channels = ["chrome", "msedge", None]

    errors: list[str] = []
    for channel in channels:
        opts = dict(options)
        if channel:
            opts["channel"] = channel
        try:
            context = pw.chromium.launch_persistent_context(**opts)
            log.info("login helper memakai browser %s", channel or "chromium bawaan")
            return context
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{channel or 'chromium'}: {type(exc).__name__}: {exc}")
            log.warning("gagal membuka %s: %s", channel or "chromium", exc)

    raise RuntimeError(
        "Tidak bisa membuka Chrome/Edge. Pastikan Google Chrome atau Microsoft Edge "
        "terpasang, atau jalankan 'playwright install chromium'. "
        + " | ".join(errors)[:300]
    )


def _earnapp_cookies(cookies: list[dict]) -> list[dict]:
    result = []
    for cookie in cookies:
        domain = (cookie.get("domain") or "").lower()
        if domain == "earnapp.com" or domain.endswith(".earnapp.com"):
            if cookie.get("name") and cookie.get("value") is not None:
                result.append(cookie)
    return result


def _cookie_header(cookies: list[dict]) -> tuple[str, int]:
    pairs: dict[str, str] = {}
    for cookie in _earnapp_cookies(cookies):
        pairs[cookie["name"]] = cookie["value"]
    header = "; ".join(f"{name}={value}" for name, value in pairs.items())
    return header, len(pairs)


def _looks_like_session(cookies: list[dict]) -> bool:
    """True hanya kalau ada cookie yang benar-benar menandakan sesi login.

    Cookie visitor/analytics/CSRF (mis. `brd_sess_id`, `xsrf-token`, `_ga`)
    sengaja tidak dihitung, supaya cookie setengah jadi tidak tersimpan
    sebagai "login berhasil". Aturan namanya sama dengan validasi cookie
    manual di `jobs.is_auth_cookie`.
    """
    return any(
        jobs.is_auth_cookie(cookie.get("name") or "", cookie.get("value") or "")
        for cookie in _earnapp_cookies(cookies)
    )


def _browser_closed(context, page) -> bool:  # noqa: ANN001
    try:
        if page.is_closed():
            return True
        if not context.pages:
            return True
    except Exception:  # noqa: BLE001
        return True
    return False


def _save_cookie(
    session: LoginSession,
    header: str,
    count: int,
    warning: str | None = None,
    cookies: list[dict] | None = None,
) -> bool:
    db = SessionLocal()
    try:
        account = db.get(Account, session.account_id)
        if account is None:
            session.update("failed", "Akun tidak ditemukan saat menyimpan cookie.")
            return False
        jobs.set_cookie(db, account, header, cookies=cookies)
        reason = f"cookie dari login helper ({count} cookie), belum dites — klik Test login"
        message = (
            f"Cookie tersimpan ({count} cookie, termasuk httpOnly). "
            "Klik Test login untuk verifikasi."
        )
        account.auth_reason = reason
        db.commit()
        if warning:
            message = f"{warning} {message}"
        session.update("saved", message, cookie_count=count, error=None)
        return True
    except Exception as exc:  # noqa: BLE001
        log.exception("gagal menyimpan cookie hasil login helper")
        session.update("failed", f"Gagal menyimpan cookie: {exc}", error=str(exc))
        return False
    finally:
        db.close()


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
def _drive(session: LoginSession, context, account: Account, note: str = "") -> None:  # noqa: ANN001
    _install_scripts(context)
    page = context.pages[0] if context.pages else context.new_page()
    base = (
        "Silakan login dengan Google. Setelah masuk ke dashboard EarnApp, "
        "tutup jendela browser atau klik “Simpan sekarang”."
    )
    session.update("running", f"{note} {base}".strip())

    try:
        page.goto(
            config.settings.login_url,
            wait_until="domcontentloaded",
            timeout=90_000,
        )
    except Exception as exc:  # noqa: BLE001
        session.update(
            "running",
            f"Halaman lambat dibuka lewat proxy ({type(exc).__name__}). "
            "Coba refresh di jendela browser; login tetap bisa dilanjutkan.",
        )

    latest_header = ""
    latest_count = 0
    latest_cookies: list[dict] = []
    deadline = time.monotonic() + max(60, config.settings.login_timeout)
    closed = False
    timed_out = False
    logged_in = False
    finish_requested_at: float | None = None

    while time.monotonic() < deadline:
        if session._cancel.is_set():
            session.update("cancelled", "Sesi login dibatalkan.", error=None)
            return

        try:
            latest_cookies = context.cookies()
            header, count = _cookie_header(latest_cookies)
            if count:
                latest_header, latest_count = header, count
        except Exception:  # noqa: BLE001
            closed = True

        if _looks_like_session(latest_cookies):
            if not logged_in:
                logged_in = True
                session.update(
                    "running",
                    "Login terdeteksi ✓ Klik “Simpan sekarang” atau tutup jendela browser.",
                )

        if session._finish.is_set():
            if logged_in:
                break
            # Klik "Simpan" sebelum cookie auth muncul: beri waktu redirect OAuth.
            if finish_requested_at is None:
                finish_requested_at = time.monotonic()
                session.update(
                    "saving",
                    f"Login belum terdeteksi. Menunggu cookie auth/OAuth terbentuk "
                    f"(maks {_FINISH_GRACE} detik)…",
                )
            elif time.monotonic() - finish_requested_at >= _FINISH_GRACE:
                break

        if _browser_closed(context, page):
            closed = True
            break

        time.sleep(0.8)
    else:
        timed_out = True

    if session._cancel.is_set():
        session.update("cancelled", "Sesi login dibatalkan.", error=None)
        return

    # Baca sekali lagi saat browser masih terbuka supaya cookie terbaru ikut.
    if not closed:
        try:
            latest_cookies = context.cookies()
            header, count = _cookie_header(latest_cookies)
            if count:
                latest_header, latest_count = header, count
            logged_in = logged_in or _looks_like_session(latest_cookies)
        except Exception:  # noqa: BLE001
            pass

    if not latest_header:
        session.update(
            "failed",
            "Tidak ada cookie earnapp.com yang tertangkap. Pastikan login berhasil "
            "sebelum menutup browser.",
            error="no cookies",
        )
        return

    if not logged_in:
        if timed_out:
            message = (
                f"Waktu login habis ({config.settings.login_timeout // 60} menit) dan "
                "cookie auth/oauth EarnApp belum terbentuk. Cookie TIDAK disimpan. "
                "Klik “Buka browser login” lagi dan pastikan sampai masuk dashboard EarnApp."
            )
        else:
            message = (
                "Login belum terdeteksi: cookie auth/oauth-token EarnApp belum terbentuk, "
                "jadi cookie TIDAK disimpan. Pastikan sudah masuk ke dashboard EarnApp "
                "(bukan hanya halaman login Google), lalu ulangi login helper."
            )
        session.update("failed", message, error="auth cookie belum terbentuk")
        log.warning("cookie auth belum terbentuk untuk akun %s", account.name)
        return

    reason = "Jendela browser ditutup" if closed else "Diminta dari dashboard"
    session.update("saving", f"{reason} — menyimpan cookie…")
    if _save_cookie(session, latest_header, latest_count, cookies=latest_cookies):
        log.info("cookie akun %s tersimpan (%s cookie)", account.name, latest_count)


def _run(session: LoginSession) -> None:
    account_id = session.account_id
    context = None
    bridge: ProxyBridge | None = None
    db = SessionLocal()
    try:
        account = db.get(Account, account_id)
        if account is None:
            session.update("failed", "Akun tidak ditemukan.", error="not found")
            return

        slot, probe, warning, notes = _probe_account(session, account, db)
        if slot is None or probe is None:
            detail = "; ".join(notes)[:300] or "tidak ada slot proxy aktif"
            session.update(
                "failed",
                f"Tidak ada proxy akun ini yang bisa dihubungi. {detail}",
                error="proxy down",
            )
            return

        exit_ip = probe["ip"] or "-"
        session.update(
            "starting",
            f"Proxy hidup ({slot.endpoint_hint}, IP keluar {exit_ip}). Menyiapkan browser…",
            slot_hint=slot.endpoint_hint,
            exit_ip=exit_ip,
        )

        parsed = proxy.parse_endpoint(decrypt(slot.endpoint_enc))
        if parsed is None:
            session.update("failed", "Format proxy slot tidak dikenali.", error="bad proxy")
            return

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            session.update(
                "failed",
                "Playwright belum terpasang. Jalankan: .\\.venv\\Scripts\\pip install playwright",
                error="playwright missing",
            )
            return

        # Chromium tidak bisa SOCKS5 + auth, jadi pakai bridge HTTP lokal tanpa auth.
        bridge = ProxyBridge(parsed, probe["scheme"]).start()
        proxy_cfg = {"server": f"http://127.0.0.1:{bridge.listen_port}"}
        profile_dir = Path(config.settings.login_profile_dir) / f"account_{account_id}"

        note = f"Proxy {slot.endpoint_hint} · IP keluar {exit_ip}."
        if warning:
            note = f"{note} {warning}"

        with sync_playwright() as pw:
            context = _launch(pw, profile_dir, proxy_cfg)
            session._context = context
            try:
                _drive(session, context, account, note)
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
                context = None
    except Exception as exc:  # noqa: BLE001
        log.exception("login helper gagal untuk akun %s", account_id)
        session.update("failed", f"Login helper gagal: {type(exc).__name__}: {exc}", error=str(exc))
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:  # noqa: BLE001
            pass
        if bridge is not None:
            bridge.close()
        session._context = None
        db.close()
