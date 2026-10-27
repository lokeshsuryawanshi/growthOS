"""Encryption for stored OAuth tokens (Fernet). The key comes from ENCRYPTION_KEY or is derived from JWT_SECRET."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


def _derive(passphrase: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(("growthos-enc:" + passphrase).encode()).digest())


def _fernet() -> Fernet:
    """ENCRYPTION_KEY may be a Fernet key or any passphrase (hashed into one); without it, JWT_SECRET is used.

    Regression: a non-Fernet ENCRYPTION_KEY used to raise ValueError the moment a founder connected Gmail."""
    s = get_settings()
    if s.encryption_key:
        try:
            return Fernet(s.encryption_key.encode())
        except (ValueError, TypeError):
            return Fernet(_derive("key:" + s.encryption_key))
    return Fernet(_derive(s.jwt_secret))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None   # key changed: caller treats the connection as lost and asks the user to reconnect
