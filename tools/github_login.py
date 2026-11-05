"""Login GitHub lewat OAuth device flow, simpan token ke secrets/github_token.

Jalankan:
    python tools/github_login.py

Token dipakai untuk membuat repo dan push. File token tidak ikut ke git
(secrets/ sudah di-.gitignore).
"""

from __future__ import annotations

import json
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / "secrets" / "github_token"
# Client ID publik GitHub CLI (device flow, tidak butuh secret).
CLIENT_ID = "178c6fc778ccc68e1d6a"
SCOPE = "repo read:user"


def main() -> int:
    headers = {"Accept": "application/json"}
    with httpx.Client(timeout=30, headers=headers) as client:
        resp = client.post(
            "https://github.com/login/device/code",
            data={"client_id": CLIENT_ID, "scope": SCOPE},
        )
        resp.raise_for_status()
        data = resp.json()
        if "device_code" not in data:
            print("Gagal memulai device flow:", data, file=sys.stderr)
            return 1

        device_code = data["device_code"]
        user_code = data["user_code"]
        verify_url = data.get("verification_uri", "https://github.com/login/device")
        interval = int(data.get("interval", 5))
        expires = int(data.get("expires_in", 900))

        url = f"{verify_url}?user_code={user_code}"
        print(f"KODE: {user_code}")
        print(f"URL: {url}")
        print("Menunggu otorisasi di browser...", flush=True)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

        deadline = time.time() + expires
        while time.time() < deadline:
            time.sleep(interval)
            poll = client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            payload = poll.json()
            token = payload.get("access_token")
            if token:
                TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                TOKEN_FILE.write_text(token.strip(), encoding="utf-8")
                me = client.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {token}"},
                ).json()
                print(f"LOGIN OK: {me.get('login')}", flush=True)
                return 0
            error = payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            print(f"Gagal: {payload}", file=sys.stderr)
            return 2

        print("Kedaluwarsa, coba lagi.", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
