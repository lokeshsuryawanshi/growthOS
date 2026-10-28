"""Operators' own API keys: the only module that reads or writes the encrypted value.

Rules (each one tested in tests/test_keys.py and evals/isolation.py):
* Write-only from the browser. After saving, only provider, last four characters and status are ever shown.
* A key is checked with one cheap call before it is stored; a key that fails is never stored.
* Every value is encrypted together with its owner and kind ({"u", "k", "p", "v"}); after decrypting, all three must
  match the row and the caller, so a ciphertext copied to another row or account is refused.
* Every read takes the account's user id. Gateway, search and contacts resolve the key of the account that owns the
  client a run belongs to, never anyone else's.
* Deleting a key deletes the row. Audit entries name the provider, never the value.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, select

from app.core.config import PROVIDER_LABELS
from app.core.scrub import scrub
from app.core.security import Principal
from app.db.models import AccountEvent, AccountSecret, Tenant
from app.db.session import get_db
from app.integrations import crypto, http

log = logging.getLogger(__name__)

PROVIDERS = {"llm": ("groq", "openai", "anthropic"), "search": ("tavily",), "contacts": ("hunter",)}
LABELS = {**PROVIDER_LABELS, "tavily": "Tavily", "hunter": "Hunter.io"}
FORMATS = {
    "groq": re.compile(r"gsk_[A-Za-z0-9]{20,100}"),
    "openai": re.compile(r"sk-(?!ant-)[A-Za-z0-9_\-]{20,200}"),
    "anthropic": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,200}"),
    "tavily": re.compile(r"tvly-[A-Za-z0-9_\-]{10,100}"),
    "hunter": re.compile(r"[A-Za-z0-9]{20,64}"),
}


class KeyRefused(ValueError):
    """Shown to the operator: why the key was not saved."""


class SecretValue:
    """A decrypted key. It never prints itself: repr, str and f-strings show only the provider and last four."""
    __slots__ = ("provider", "_value", "fingerprint")

    def __init__(self, provider: str, value: str):
        import hashlib
        self.provider, self._value = provider, value
        self.fingerprint = hashlib.sha256(value.encode()).hexdigest()[:16]

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"<{self.provider} key ***{self._value[-4:]}>"

    __str__ = __repr__


@dataclass
class KeyView:
    kind: str
    provider: str
    label: str
    last4: str
    status: str
    status_detail: str
    validated_at: datetime | None


def _event(s, user_id: str, action: str, **detail) -> None:
    s.add(AccountEvent(user_id=user_id, action=action, detail=detail))


async def list_keys(user_id: str) -> dict[str, KeyView]:
    async with get_db().reader() as s:          # the ciphertext column is deliberately not selected
        rows = (await s.execute(select(AccountSecret.kind, AccountSecret.provider, AccountSecret.last4, AccountSecret.status,
                                       AccountSecret.status_detail, AccountSecret.validated_at)
                                .where(AccountSecret.user_id == user_id))).all()
    return {k: KeyView(k, p, LABELS.get(p, p), l4, st, det, at) for k, p, l4, st, det, at in rows}


async def events(user_id: str, limit: int = 20) -> list[AccountEvent]:
    async with get_db().reader() as s:
        return list((await s.execute(select(AccountEvent).where(AccountEvent.user_id == user_id)
                                     .order_by(AccountEvent.created_at.desc()).limit(limit))).scalars().all())


# ---------------------------------------------------------------- the one cheap check before storing
async def check_key(provider: str, value: str) -> tuple[bool, str]:
    """(valid, reason). Network trouble raises KeyRefused: the key is neither stored nor declared invalid."""
    if provider == "anthropic":
        return await _check_anthropic(value)
    urls = {"groq": "https://api.groq.com/openai/v1/models", "openai": "https://api.openai.com/v1/models",
            "tavily": "https://api.tavily.com/usage", "hunter": "https://api.hunter.io/v2/account"}
    headers = {"X-API-KEY": value} if provider == "hunter" else {"Authorization": f"Bearer {value}"}  # never in a URL
    try:
        async with http.client(timeout=15) as c:
            r = await c.get(urls[provider], headers=headers)
    except httpx.HTTPError as e:
        raise KeyRefused(f"Could not reach {LABELS[provider]} to check the key ({type(e).__name__}). Nothing was saved; "
                         "try again in a moment.")
    if r.status_code == 200:
        return True, "accepted"
    if r.status_code in (401, 403):
        return False, f"{LABELS[provider]} rejected this key"
    if r.status_code == 402:
        return False, f"{LABELS[provider]} says the account has no credit"
    if r.status_code == 429:
        raise KeyRefused(f"{LABELS[provider]} is rate limiting checks right now. Nothing was saved; try again shortly.")
    raise KeyRefused(f"{LABELS[provider]} answered HTTP {r.status_code} to the check. Nothing was saved; try again later.")


async def _check_anthropic(value: str) -> tuple[bool, str]:
    """Through the official SDK (models.list is free)."""
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=value, max_retries=0, timeout=15, http_client=http.sdk_client("anthropic"))
    try:
        await client.models.list(limit=1)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        return False, "Anthropic rejected this key"
    except anthropic.APIStatusError as e:
        if e.status_code == 402:
            return False, "Anthropic says the account has no credit"
        raise KeyRefused(f"Anthropic answered HTTP {e.status_code} to the check. Nothing was saved; try again later.")
    except anthropic.APIConnectionError:
        raise KeyRefused("Could not reach Anthropic to check the key. Nothing was saved; try again in a moment.")
    finally:
        await client.close()
    return True, "accepted"


async def save_key(p: Principal, kind: str, provider: str, raw: str) -> KeyView:
    value = (raw or "").strip()
    if kind not in PROVIDERS or provider not in PROVIDERS[kind]:
        raise KeyRefused("Unknown service.")
    if not FORMATS[provider].fullmatch(value):
        raise KeyRefused(f"That does not look like a {LABELS[provider]} key. Paste the whole key, with nothing around it.")
    ok, reason = await check_key(provider, value)
    if not ok:
        raise KeyRefused(f"Not saved: {reason}.")
    blob = crypto.encrypt(json.dumps({"u": p.user_id, "k": kind, "p": provider, "v": value}))
    now = datetime.now(timezone.utc)
    async with get_db().session() as s:
        row = (await s.execute(select(AccountSecret).where(AccountSecret.user_id == p.user_id,
                                                           AccountSecret.kind == kind))).scalars().first()
        action = "key.replaced" if row else "key.added"
        if row is None:
            row = AccountSecret(user_id=p.user_id, kind=kind, provider=provider, ciphertext=blob, last4=value[-4:])
            s.add(row)
        row.provider, row.ciphertext, row.last4 = provider, blob, value[-4:]
        row.status, row.status_detail, row.validated_at = "valid", reason, now
        _event(s, p.user_id, action, kind=kind, provider=provider)
    log.info("account %s: %s for %s", p.user_id, action, provider)
    return (await list_keys(p.user_id))[kind]


async def delete_key(p: Principal, kind: str) -> bool:
    async with get_db().session() as s:
        row = (await s.execute(select(AccountSecret.provider).where(AccountSecret.user_id == p.user_id,
                                                                    AccountSecret.kind == kind))).first()
        if row is None:
            return False
        await s.execute(delete(AccountSecret).where(AccountSecret.user_id == p.user_id, AccountSecret.kind == kind))
        _event(s, p.user_id, "key.deleted", kind=kind, provider=row[0])
    return True


async def delete_all(user_id: str) -> None:
    async with get_db().session() as s:
        await s.execute(delete(AccountSecret).where(AccountSecret.user_id == user_id))
        await s.execute(delete(AccountEvent).where(AccountEvent.user_id == user_id))


async def mark_invalid(user_id: str, kind: str, reason: str) -> None:
    """The provider refused a stored key during a run (revoked, no credit): show it, stop using it."""
    async with get_db().session() as s:
        row = (await s.execute(select(AccountSecret).where(AccountSecret.user_id == user_id,
                                                           AccountSecret.kind == kind))).scalars().first()
        if row is not None and row.status != "invalid":
            row.status, row.status_detail = "invalid", scrub(reason)[:300]
            _event(s, user_id, "key.rejected_by_provider", kind=kind, provider=row.provider)


# ---------------------------------------------------------------- reading a key (always for a named account)
async def secret(user_id: str, kind: str) -> SecretValue | None:
    """The account's own valid key of this kind, decrypted in memory for the caller, or None."""
    async with get_db().reader() as s:
        row = (await s.execute(select(AccountSecret).where(AccountSecret.user_id == user_id,
                                                           AccountSecret.kind == kind))).scalars().first()
    if row is None or row.status != "valid":
        return None
    plain = crypto.decrypt(row.ciphertext)
    if plain is None:
        log.warning("account %s: stored %s key could not be decrypted (encryption key changed?)", user_id, kind)
        return None
    try:
        d = json.loads(plain)
    except json.JSONDecodeError:
        return None
    if d.get("u") != user_id or d.get("k") != kind or d.get("p") != row.provider:
        log.warning("account %s: stored %s key is bound to another owner or kind; refused", user_id, kind)
        return None
    return SecretValue(row.provider, d["v"])


async def owner_of(tenant_id: str) -> str | None:
    async with get_db().reader() as s:
        t = await s.get(Tenant, tenant_id)
    return t.owner_user_id if t else None


async def for_client(tenant_id: str, kind: str) -> tuple[str | None, SecretValue | None]:
    """(owner's user id, owner's key) for work done on this client. The client decides the account; nothing else."""
    uid = await owner_of(tenant_id)
    return uid, (await secret(uid, kind) if uid else None)
