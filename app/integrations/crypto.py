"""Encryption for operators' stored API keys (Fernet, with rotation through MultiFernet).

Production requires a valid ENCRYPTION_KEY (or ENCRYPTION_KEYS, newest first) and refuses to start without one
(Settings.validate_runtime). Tests and the local demo may run without one: the key is then derived from JWT_SECRET.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings


def _fernet() -> MultiFernet:
    s = get_settings()
    keys = s.fernet_keys
    if not keys:
        if s.app_env == "production":
            raise RuntimeError("ENCRYPTION_KEY is required in production")
        keys = [base64.urlsafe_b64encode(hashlib.sha256(("growthos-dev:" + s.jwt_secret).encode()).digest()).decode()]
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None   # wrong or retired key, or a tampered value: the caller treats the key as missing


def rotate(token: str) -> str:
    """Re-encrypt under the newest key (after ENCRYPTION_KEYS gained a new first entry)."""
    return _fernet().rotate(token.encode()).decode()
