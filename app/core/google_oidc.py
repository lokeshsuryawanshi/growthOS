"""Sign in with Google (OpenID Connect, authorization-code flow with PKCE). Scopes: openid email profile, nothing else.

start():    a random state, nonce and PKCE verifier go into a signed, 10-minute, HttpOnly cookie; the browser is sent to
            Google with the state, the nonce and the verifier's S256 challenge.
finish():   the state in the callback must equal the cookie's; the code is exchanged (with the verifier) for an ID token,
            whose signature is checked against Google's published keys, and whose audience, issuer, expiry and nonce must
            match. The email must be verified by Google. The account is identified by Google's `sub`, not by email.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt

from app.core.config import get_settings
from app.integrations import http

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
FLOW_COOKIE = "gx_oauth"
FLOW_TTL_S = 600


class LoginFailed(Exception):
    """Shown to the person on the sign-in page; never contains a token or a secret."""


@dataclass
class GoogleIdentity:
    sub: str
    email: str
    name: str


def configured() -> bool:
    s = get_settings()
    return bool(s.google_oauth_client_id and s.google_oauth_client_secret)


def redirect_uri() -> str:
    return get_settings().app_base_url.rstrip("/") + "/auth/google/callback"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def start() -> tuple[str, str]:
    """(URL to send the browser to, signed flow cookie value)."""
    if not configured():
        raise LoginFailed("Google sign-in is not set up on this server yet.")
    s = get_settings()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    cookie = jwt.encode({"st": state, "n": nonce, "v": verifier,
                         "exp": datetime.now(timezone.utc) + timedelta(seconds=FLOW_TTL_S)},
                        s.jwt_secret, algorithm="HS256")
    url = AUTH_URL + "?" + urlencode({
        "client_id": s.google_oauth_client_id, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": "openid email profile", "state": state, "nonce": nonce, "code_challenge": challenge,
        "code_challenge_method": "S256", "prompt": "select_account"})
    return url, cookie


def _flow(cookie: str | None) -> dict:
    if not cookie:
        raise LoginFailed("Your sign-in took too long or was started in another browser. Please try again.")
    try:
        return jwt.decode(cookie, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise LoginFailed("Your sign-in took too long or was started in another browser. Please try again.")


_jwks: tuple[float, dict] | None = None


async def _google_keys(refresh: bool = False) -> dict:
    """Google's signing keys, cached for an hour (they rotate rarely; an unknown key id forces one refresh)."""
    global _jwks
    if _jwks and not refresh and time.monotonic() - _jwks[0] < 3600:
        return _jwks[1]
    async with http.client(timeout=10) as c:
        r = await c.get(CERTS_URL)
    if r.status_code != 200:
        raise LoginFailed("Could not reach Google to check your sign-in. Please try again.")
    _jwks = (time.monotonic(), r.json())
    return _jwks[1]


def reset_key_cache() -> None:
    global _jwks
    _jwks = None


async def verify_id_token(id_token: str, nonce: str) -> GoogleIdentity:
    s = get_settings()
    try:
        kid = jwt.get_unverified_header(id_token).get("kid")
    except jwt.PyJWTError:
        raise LoginFailed("Google returned an unreadable sign-in token.")
    keys = await _google_keys()
    jwk = next((k for k in keys.get("keys", []) if k.get("kid") == kid), None)
    if jwk is None:
        jwk = next((k for k in (await _google_keys(refresh=True)).get("keys", []) if k.get("kid") == kid), None)
    if jwk is None:
        raise LoginFailed("Google's sign-in token was signed with an unknown key.")
    try:
        claims = jwt.decode(id_token, jwt.PyJWK(jwk).key, algorithms=["RS256"], audience=s.google_oauth_client_id,
                            options={"require": ["iss", "sub", "aud", "exp", "iat"]}, leeway=60)
    except jwt.PyJWTError as e:
        raise LoginFailed(f"Google's sign-in token did not check out ({type(e).__name__}).")
    if claims.get("iss") not in ISSUERS:
        raise LoginFailed("The sign-in token was not issued by Google.")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise LoginFailed("The sign-in token does not belong to this sign-in attempt.")
    if claims.get("email_verified") is not True or not claims.get("email"):
        raise LoginFailed("Your Google account's email address is not verified.")
    email = str(claims["email"]).strip().lower()
    allowed = {e.strip().lower() for e in s.allowed_emails.split(",") if e.strip()}
    if allowed and email not in allowed:
        raise LoginFailed("This account is not on the list of people allowed to use this site.")
    return GoogleIdentity(sub=str(claims["sub"]), email=email, name=str(claims.get("name") or email.split("@")[0])[:200])


async def finish(code: str, state: str, cookie: str | None) -> GoogleIdentity:
    flow = _flow(cookie)
    if not state or not secrets.compare_digest(state, flow.get("st", "")):
        raise LoginFailed("The sign-in response did not match the request. Please try again.")
    if not code:
        raise LoginFailed("Google did not return a sign-in code. Please try again.")
    s = get_settings()
    try:
        async with http.client(timeout=15) as c:
            r = await c.post(TOKEN_URL, data={
                "code": code, "client_id": s.google_oauth_client_id, "client_secret": s.google_oauth_client_secret,
                "redirect_uri": redirect_uri(), "grant_type": "authorization_code", "code_verifier": flow["v"]})
    except httpx.HTTPError:
        raise LoginFailed("Could not reach Google to finish signing in. Please try again.")
    try:
        body = r.json()
    except json.JSONDecodeError:
        body = {}
    if r.status_code != 200 or not body.get("id_token"):
        raise LoginFailed("Google did not accept the sign-in (" + str(body.get("error") or f"HTTP {r.status_code}") + ").")
    return await verify_id_token(body["id_token"], flow["n"])
