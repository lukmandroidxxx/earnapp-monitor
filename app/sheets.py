from __future__ import annotations

import csv
import re
from dataclasses import dataclass

import httpx

from . import config


class SheetsError(RuntimeError):
    pass


class SheetsNotConfigured(SheetsError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


@dataclass
class Row:
    number: int
    values: dict[str, str]

    def get(self, field: str) -> str:
        for alias in config.READ_MAP.get(field, []):
            value = self.values.get(_norm(alias))
            if value and value.strip():
                return value.strip()
        return ""


@dataclass
class SheetData:
    headers: list[str]
    rows: list[Row]
    source: str  # "google-api" | "google-public" | "csv"


def _google_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        str(config.settings.credentials_file),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _read_google_api() -> list[list[str]]:
    service = _google_service()
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=config.settings.sheet_id,
            range=f"'{config.settings.sheet_tab}'!A1:AZ2000",
        )
        .execute()
    )
    return result.get("values", [])


def _read_google_public() -> list[list[str]]:
    """Baca sheet yang di-share 'anyone with the link' tanpa service account."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{config.settings.sheet_id}"
        f"/export?format=csv&gid={config.settings.sheet_gid}"
    )
    try:
        response = httpx.get(url, follow_redirects=True, timeout=30)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise SheetsError(f"gagal mengunduh sheet publik: {exc}") from exc

    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type or response.text.lstrip().startswith("<!DOCTYPE"):
        raise SheetsNotConfigured(
            "Sheet tidak bisa dibaca publik. Ubah sharing ke 'Anyone with the link (Viewer)' "
            "atau pakai service account."
        )
    return [row for row in csv.reader(response.text.splitlines())]


def _read_csv() -> list[list[str]]:
    path = config.settings.csv_fallback
    if not path.exists():
        raise SheetsNotConfigured(
            "Google Sheets belum dikonfigurasi. Isi GOOGLE_SHEET_ID, "
            f"atau taruh export CSV di {path}."
        )
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.reader(handle)]


def read_sheet() -> SheetData:
    if config.settings.sheet_id:
        if config.settings.credentials_file.exists():
            values = _read_google_api()
            source = "google-api"
        else:
            values = _read_google_public()
            source = "google-public"
    else:
        values = _read_csv()
        source = "csv"

    if not values:
        return SheetData(headers=[], rows=[], source=source)

    headers = [str(h).strip() for h in values[0]]
    normalized = [_norm(h) for h in headers]
    rows: list[Row] = []
    for offset, raw in enumerate(values[1:], start=2):
        cells = {
            normalized[index]: (str(raw[index]).strip() if index < len(raw) else "")
            for index in range(len(normalized))
        }
        if any(cells.values()):
            rows.append(Row(number=offset, values=cells))
    return SheetData(headers=headers, rows=rows, source=source)


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def writeback(sheet: SheetData, updates: dict[int, dict[str, str]]) -> tuple[bool, str]:
    """Tulis status monitoring kembali ke sheet (butuh service account)."""
    if not config.settings.sheet_writeback:
        return False, "writeback dimatikan (SHEET_WRITEBACK=0)"
    if sheet.source != "google-api":
        return False, "writeback butuh service account (GOOGLE_CREDENTIALS_FILE)"
    if not updates:
        return False, "tidak ada perubahan"

    index_by_header = {_norm(h): i for i, h in enumerate(sheet.headers)}
    payload: list[dict] = []
    for row_number, fields in updates.items():
        for key, value in fields.items():
            header = config.WRITE_MAP.get(key)
            if not header:
                continue
            index = index_by_header.get(_norm(header))
            if index is None:
                continue
            cell = f"'{config.settings.sheet_tab}'!{_col_letter(index)}{row_number}"
            payload.append({"range": cell, "values": [[value]]})

    if not payload:
        return False, "tidak ada kolom writeback yang cocok"

    try:
        _google_service().spreadsheets().values().batchUpdate(
            spreadsheetId=config.settings.sheet_id,
            body={"valueInputOption": "RAW", "data": payload},
        ).execute()
    except Exception as exc:  # noqa: BLE001
        return False, f"gagal menulis ke sheet: {exc}"
    return True, f"{len(payload)} sel diperbarui"
