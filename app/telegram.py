from __future__ import annotations

import html

import httpx

from . import config


def send(text: str) -> bool:
    if not config.settings.telegram_enabled:
        return False
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{config.settings.telegram_token}/sendMessage",
            json={
                "chat_id": config.settings.telegram_chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        return response.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def alert_account(account_name: str, status: str, reason: str) -> bool:
    icon = "⛔" if status == "BLOCKED" else "⚠️"
    return send(
        f"{icon} <b>{html.escape(account_name)}</b> → <code>{html.escape(status)}</code>\n"
        f"{html.escape(reason or '-')}"
    )
