"""Buat repo GitHub dari token device flow, lalu push commit lokal.

Prasyarat: sudah menjalankan tools/github_login.py (secrets/github_token ada).
Jalankan: python tools/github_setup.py [--repo earnapp-monitor] [--private]
"""

from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / "secrets" / "github_token"


def api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "earnapp-monitor-setup",
    }


def git(*args: str) -> None:
    exe = shutil.which("git")
    if not exe:
        raise SystemExit("git tidak ditemukan di PATH")
    print("$ git " + " ".join(args))
    subprocess.run([exe, *args], cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="earnapp-monitor")
    parser.add_argument("--private", action="store_true")
    args = parser.parse_args()

    if not TOKEN_FILE.exists():
        print("Token belum ada. Jalankan tools/github_login.py dulu.", file=sys.stderr)
        return 1
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    with httpx.Client(timeout=60, headers=api_headers(token)) as client:
        me = client.get("https://api.github.com/user")
        if me.status_code != 200:
            print(f"Token tidak valid ({me.status_code}): {me.text[:200]}", file=sys.stderr)
            return 2
        login = me.json()["login"]
        print(f"Login sebagai: {login}")

        payload = {
            "name": args.repo,
            "description": "EarnApp multi-account monitor dashboard",
            "private": bool(args.private),
            "has_issues": True,
            "has_wiki": False,
            "auto_init": False,
        }
        created = client.post("https://api.github.com/user/repos", json=payload)
        if created.status_code == 201:
            print(f"Repo dibuat: {created.json()['html_url']}")
        elif created.status_code == 422:
            print("Repo sudah ada, lanjut push.")
        else:
            print(f"Gagal buat repo ({created.status_code}): {created.text[:300]}", file=sys.stderr)
            return 3

        default_branch = client.get(
            f"https://api.github.com/repos/{login}/{args.repo}"
        ).json().get("default_branch", "main")
        if default_branch != "main":
            print(f"Catatan: default branch repo = {default_branch}")

    remote = f"https://github.com/{login}/{args.repo}.git"
    existing = subprocess.run(
        [shutil.which("git") or "git", "remote"], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()
    if "origin" in existing:
        git("remote", "remove", "origin")
    git("remote", "add", "origin", remote)

    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    git(
        "-c",
        f"http.extraHeader=AUTHORIZATION: basic {auth}",
        "push",
        "-u",
        "origin",
        "main",
        "--force",
    )
    print(f"Selesai: {remote}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
