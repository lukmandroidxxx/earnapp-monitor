from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from .config import SECRETS_DIR

_KEY_FILE = SECRETS_DIR / "secret.key"


def _load_key() -> bytes:
    # Di server/container, kunci bisa diberikan lewat env supaya tetap sama
    # walaupun folder secrets tidak ikut terbawa. Kalau kosong, pakai file.
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        return env_key.encode("ascii")
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    return key


_fernet = Fernet(_load_key())


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet.decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""
