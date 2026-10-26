"""Test doubles for third-party HTTP APIs. They exist only in tests: production code never imports them."""
from __future__ import annotations

import json
import time

import httpx
from sqlalchemy import update

from app.core.tenancy import tenant_scope
from app.db.models import IntegrationCredential, Lead
from app.integrations import gmail, http
from app.integrations.crypto import encrypt


class FakeGoogle:
    """Answers Google's token endpoint and the Gmail send endpoint. Records every sent message."""

    def __init__(self, send_status: int = 200, token_error: str = ""):
        self.sent: list[dict] = []
        self.token_calls = 0
        self.send_status = send_status
        self.token_error = token_error

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            self.token_calls += 1
            if self.token_error:
                return httpx.Response(400, json={"error": self.token_error})
            body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            out = {"access_token": "at-123", "expires_in": 3600, "scope": "https://www.googleapis.com/auth/gmail.send openid email"}
            if body.get("grant_type") == "authorization_code":
                import jwt
                out["refresh_token"] = "rt-456"
                out["id_token"] = jwt.encode({"email": "founder@example.com"}, "x", algorithm="HS256")
            return httpx.Response(200, json=out)
        if request.url.host == "gmail.googleapis.com":
            if request.headers.get("authorization") != "Bearer at-123":
                return httpx.Response(401, json={})
            if self.send_status != 200:
                return httpx.Response(self.send_status, json={"error": "x"}, text="rate limit" if self.send_status == 403 else "")
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"gmail-{len(self.sent)}"})
        return httpx.Response(404)

    def install(self) -> "FakeGoogle":
        http.set_transport(httpx.MockTransport(self.handler))
        return self


async def connect_fake_gmail(db, tid: str, email: str = "founder@example.com") -> None:
    with tenant_scope(tid):
        async with db.session() as s:
            s.add(IntegrationCredential(provider="gmail", account_email=email, status="connected",
                                        secret_enc=encrypt(json.dumps({"refresh_token": "rt-456"})), scopes="gmail.send"))


async def make_sendable(db, tid: str) -> None:
    """Pretend the fixture leads are real, verified contacts. Only done inside tests that exercise the send gate."""
    with tenant_scope(tid):
        async with db.session() as s:
            await s.execute(update(Lead).values(is_synthetic=False, email_status="valid"))


def reset_http() -> None:
    http.set_transport(None)
