"""Gmail OAuth (authorization-code flow) and sending. Optional until the Google OAuth client is configured.

Scope is gmail.send only: Growth OS can send mail the founder approved, and cannot read the mailbox.
The refresh token is stored encrypted per client. Sending happens only from approvals.send_outreach.
"""
from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import select

from app.core.config import get_settings
from app.core.tenancy import tenant_scope
from app.db.models import IntegrationCredential
from app.db.session import get_db
from app.integrations import http
from app.integrations.crypto import decrypt, encrypt
from app.integrations.errors import (AUTH_FAILED, BAD_RESPONSE, NOT_CONFIGURED, QUOTA, RATE_LIMITED, TIMEOUT, UNAVAILABLE,
                                     IntegrationError)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SCOPES = "https://www.googleapis.com/auth/gmail.send openid email"


def redirect_uri() -> str:
    return get_settings().app_base_url.rstrip("/") + "/oauth/google/callback"


def configured() -> bool:
    s = get_settings()
    return bool(s.google_oauth_client_id and s.google_oauth_client_secret)


def _require() -> None:
    if not configured():
        raise IntegrationError("gmail", NOT_CONFIGURED, "Google OAuth client is not configured on this server",
                               "Create an OAuth client (Web application) in Google Cloud Console, enable the Gmail API, add "
                               f"{redirect_uri()} as a redirect URI, then set GOOGLE_OAUTH_CLIENT_ID and "
                               "GOOGLE_OAUTH_CLIENT_SECRET.")


def make_state(tenant_id: str, user_id: str) -> str:
    s = get_settings()
    return jwt.encode({"tid": tenant_id, "uid": user_id, "n": secrets.token_urlsafe(8), "purpose": "gmail_oauth",
                       "exp": datetime.now(timezone.utc) + timedelta(minutes=10)}, s.jwt_secret, algorithm="HS256")


def read_state(state: str) -> dict:
    try:
        p = jwt.decode(state, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise IntegrationError("gmail", AUTH_FAILED, "the sign-in link expired or is invalid", "Start the connection again.") from e
    if p.get("purpose") != "gmail_oauth":
        raise IntegrationError("gmail", AUTH_FAILED, "invalid state")
    return p


def authorize_url(tenant_id: str, user_id: str) -> str:
    _require()
    q = {"client_id": get_settings().google_oauth_client_id, "redirect_uri": redirect_uri(), "response_type": "code",
         "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": make_state(tenant_id, user_id)}
    return AUTH_URL + "?" + urlencode(q)


async def _token_request(data: dict) -> dict:
    s = get_settings()
    try:
        async with http.client(timeout=20) as c:
            r = await c.post(TOKEN_URL, data={**data, "client_id": s.google_oauth_client_id,
                                              "client_secret": s.google_oauth_client_secret})
    except httpx.TimeoutException as e:
        raise IntegrationError("gmail", TIMEOUT, "Google timed out") from e
    except httpx.HTTPError as e:
        raise IntegrationError("gmail", UNAVAILABLE, f"Google unreachable ({type(e).__name__})") from e
    if r.status_code != 200:
        err = ""
        try:
            err = r.json().get("error", "")
        except ValueError:
            pass
        if err in ("invalid_grant", "invalid_client", "unauthorized_client"):
            raise IntegrationError("gmail", AUTH_FAILED, f"Google rejected the credentials ({err})",
                                   "Reconnect Gmail from the Integrations page.")
        raise IntegrationError("gmail", UNAVAILABLE, f"Google token endpoint HTTP {r.status_code}")
    return r.json()


def _email_from_id_token(id_token: str) -> str:
    try:
        return jwt.decode(id_token, options={"verify_signature": False}).get("email", "")  # received directly from Google over TLS
    except jwt.PyJWTError:
        return ""


async def complete_connection(tenant_id: str, user_id: str, code: str) -> str:
    _require()
    tok = await _token_request({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
    if not tok.get("refresh_token"):
        raise IntegrationError("gmail", BAD_RESPONSE, "Google did not return a refresh token",
                               "Remove Growth OS at myaccount.google.com/permissions and connect again.")
    if "gmail.send" not in tok.get("scope", ""):
        raise IntegrationError("gmail", AUTH_FAILED, "the Gmail send permission was not granted",
                               "Connect again and tick the permission to send email.")
    email = _email_from_id_token(tok.get("id_token", ""))
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            cred = (await s.execute(select(IntegrationCredential).where(IntegrationCredential.provider == "gmail"))).scalars().first()
            if cred is None:
                cred = IntegrationCredential(provider="gmail")
                s.add(cred)
            cred.account_email, cred.scopes = email, tok.get("scope", "")
            cred.secret_enc = encrypt(json.dumps({"refresh_token": tok["refresh_token"]}))
            cred.status, cred.last_error, cred.connected_by = "connected", "", user_id
    return email


async def connection(tenant_id: str) -> IntegrationCredential | None:
    with tenant_scope(tenant_id):
        async with get_db().reader() as s:
            return (await s.execute(select(IntegrationCredential).where(IntegrationCredential.provider == "gmail"))).scalars().first()


async def disconnect(tenant_id: str) -> None:
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            cred = (await s.execute(select(IntegrationCredential).where(IntegrationCredential.provider == "gmail"))).scalars().first()
            if cred:
                await s.delete(cred)


async def _mark(tenant_id: str, status: str, err: str) -> None:
    with tenant_scope(tenant_id):
        async with get_db().session() as s:
            cred = (await s.execute(select(IntegrationCredential).where(IntegrationCredential.provider == "gmail"))).scalars().first()
            if cred:
                cred.status, cred.last_error = status, err[:300]


def build_message(sender: str, to: str, subject: str, body: str) -> str:
    m = EmailMessage()
    m["To"], m["Subject"] = to, subject
    if sender:
        m["From"] = sender
    m.set_content(body)
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


async def send_email(tenant_id: str, to: str, subject: str, body: str) -> str:
    """Send one approved email from the founder's own Gmail. Returns the Gmail message id."""
    _require()
    cred = await connection(tenant_id)
    if cred is None or cred.status != "connected":
        raise IntegrationError("gmail", NOT_CONFIGURED, "Gmail is not connected for this workspace",
                               "Connect Gmail from the Integrations page.")
    secret = decrypt(cred.secret_enc)
    if not secret:
        await _mark(tenant_id, "error", "stored token cannot be decrypted")
        raise IntegrationError("gmail", AUTH_FAILED, "the stored Gmail connection can no longer be read",
                               "Reconnect Gmail from the Integrations page.")
    try:
        tok = await _token_request({"refresh_token": json.loads(secret)["refresh_token"], "grant_type": "refresh_token"})
    except IntegrationError as e:
        if e.code == AUTH_FAILED:
            await _mark(tenant_id, "revoked", e.message)
        raise
    try:
        async with http.client(timeout=25) as c:
            r = await c.post(SEND_URL, headers={"Authorization": f"Bearer {tok['access_token']}"},
                             json={"raw": build_message(cred.account_email, to, subject, body)})
    except httpx.TimeoutException as e:
        raise IntegrationError("gmail", TIMEOUT, "Gmail timed out; check your Sent folder before retrying") from e
    except httpx.HTTPError as e:
        raise IntegrationError("gmail", UNAVAILABLE, f"Gmail unreachable ({type(e).__name__})") from e
    if r.status_code == 200:
        return r.json().get("id", "")
    if r.status_code == 401:
        await _mark(tenant_id, "revoked", "Gmail rejected the access token")
        raise IntegrationError("gmail", AUTH_FAILED, "Gmail rejected the connection", "Reconnect Gmail from the Integrations page.")
    if r.status_code == 429 or (r.status_code == 403 and "rate" in r.text.lower()):
        raise IntegrationError("gmail", RATE_LIMITED, "Gmail sending limit reached", "Try again later.")
    if r.status_code == 403:
        raise IntegrationError("gmail", QUOTA, "Gmail refused to send (permission or daily limit)", "Reconnect Gmail and grant send access.")
    raise IntegrationError("gmail", UNAVAILABLE, f"Gmail HTTP {r.status_code}")
