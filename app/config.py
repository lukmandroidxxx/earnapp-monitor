from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SECRETS_DIR = ROOT / "secrets"
CONFIG_DIR = ROOT / "config"
BROWSER_DIR = SECRETS_DIR / "browser_profiles"

for _dir in (DATA_DIR, SECRETS_DIR, CONFIG_DIR, BROWSER_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _get(name, "1" if default else "0").lower()
    return raw in {"1", "true", "ya", "yes", "on", "aktif"}


@dataclass(frozen=True)
class Settings:
    db_url: str
    sheet_id: str
    sheet_gid: str
    sheet_tab: str
    credentials_file: Path
    csv_fallback: Path
    sheet_writeback: bool
    auto_sync: bool
    auto_poll: bool
    sync_interval: int
    poll_interval: int
    poll_concurrency: int
    proxy_scheme: str
    max_usage_count: int
    scraper_mode: str
    earnapp_url: str
    earnapp_balance_path: str
    earnapp_lifetime_path: str
    earnapp_nodes_path: str
    earnapp_auth_style: str
    earnapp_api_base: str
    earnapp_app_version: str
    device_sync: bool
    device_usage_days: int
    dashboard_user: str
    dashboard_password: str
    login_helper: bool
    login_url: str
    login_timeout: int
    login_browser: str
    login_profile_dir: Path
    telegram_token: str
    telegram_chat_id: str
    alert_cooldown: int
    request_timeout: int

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


def _load_sheet_map() -> dict:
    path = CONFIG_DIR / "sheet.json"
    if not path.exists():
        return {"read": {}, "write": {}}
    return json.loads(path.read_text(encoding="utf-8"))


SHEET_MAP = _load_sheet_map()
READ_MAP: dict[str, list[str]] = SHEET_MAP.get("read", {})
WRITE_MAP: dict[str, str] = SHEET_MAP.get("write", {})

settings = Settings(
    db_url=_get("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'monitor.db').as_posix()}"),
    sheet_id=_get("GOOGLE_SHEET_ID"),
    sheet_gid=_get("GOOGLE_SHEET_GID", "0"),
    sheet_tab=_get("GOOGLE_SHEET_TAB", "pool"),
    credentials_file=Path(_get("GOOGLE_CREDENTIALS_FILE", str(SECRETS_DIR / "service_account.json"))),
    csv_fallback=Path(_get("CSV_FALLBACK_FILE", str(DATA_DIR / "pool.csv"))),
    sheet_writeback=_bool("SHEET_WRITEBACK", False),
    auto_sync=_bool("AUTO_SYNC", True),
    auto_poll=_bool("AUTO_POLL", True),
    sync_interval=_int("SYNC_INTERVAL_SECONDS", 300),
    poll_interval=_int("POLL_INTERVAL_SECONDS", 900),
    poll_concurrency=_int("POLL_CONCURRENCY", 8),
    proxy_scheme=_get("PROXY_SCHEME", "auto").lower(),
    max_usage_count=_int("MAX_USAGE_COUNT", 0),
    scraper_mode=_get("SCRAPER_MODE", "off").lower(),
    earnapp_url=_get("EARN_APP_BALANCE_URL"),
    earnapp_balance_path=_get("EARN_APP_BALANCE_PATH", "balance"),
    earnapp_lifetime_path=_get("EARN_APP_LIFETIME_PATH", "lifetime"),
    earnapp_nodes_path=_get("EARN_APP_NODES_PATH", "nodes_online"),
    earnapp_auth_style=_get("EARN_APP_AUTH_STYLE", "cookie").lower(),
    earnapp_api_base=_get("EARN_APP_API_BASE", "https://earnapp.com/dashboard/api").rstrip("/"),
    earnapp_app_version=_get("EARN_APP_APP_VERSION", "1.685.135"),
    device_sync=_bool("DEVICE_SYNC", True),
    device_usage_days=_int("DEVICE_USAGE_DAYS", 14),
    dashboard_user=_get("DASHBOARD_USER", "admin"),
    dashboard_password=_get("DASHBOARD_PASSWORD"),
    login_helper=_bool("LOGIN_HELPER", True),
    login_url=_get("EARN_APP_LOGIN_URL", "https://earnapp.com/dashboard"),
    login_timeout=_int("LOGIN_HELPER_TIMEOUT", 900),
    login_browser=_get("LOGIN_HELPER_BROWSER", "auto").lower(),
    login_profile_dir=Path(_get("LOGIN_PROFILE_DIR", str(BROWSER_DIR))),
    telegram_token=_get("TELEGRAM_BOT_TOKEN"),
    telegram_chat_id=_get("TELEGRAM_CHAT_ID"),
    alert_cooldown=_int("ALERT_COOLDOWN_SECONDS", 1800),
    request_timeout=_int("REQUEST_TIMEOUT", 20),
)

LAST_SYNC_FILE = DATA_DIR / "last_sync.json"
