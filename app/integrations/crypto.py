"""Encryption for stored OAuth tokens (Fernet). The key comes from ENCRYPTION_KEY or is derived from JWT_SECRET."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


def _fernet() -> Fernet:
    s = get_settings()
    if s.encryption_key:
        return Fernet(s.encryption_key.encode())
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("growthos-enc:" + s.jwt_secret).encode()).digest()))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None   # key changed: caller treats the connection as lost and asks the user to reconnect
