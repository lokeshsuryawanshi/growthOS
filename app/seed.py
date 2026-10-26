"""Seed the dev database: 12 clients, agency + client users, intake, founder samples, suppression lists.

Usage: python -m app.seed [--reset]
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.adapters.data.clients import CLIENTS, DEMO_PASSWORD, USERS
from app.core.security import hash_password
from app.core.tenancy import tenant_scope
from app.db.audit import audit
from app.db.models import ClientProfile, KnowledgeDoc, Membership, SuppressionEntry, Tenant, User
from app.db.session import Database, get_db


async def seed(db: Database | None = None, reset: bool = False, clients: list[dict] | None = None) -> dict[str, str]:
    db = db or get_db()
    if reset:
        await db.drop_all()
    await db.create_all()
    clients = clients or CLIENTS
    async with db.reader() as s:
        if (await s.execute(select(Tenant.id).limit(1))).first():
            rows = (await s.execute(select(Tenant.slug, Tenant.id))).all()
            return {slug: tid for slug, tid in rows}

    slug_to_id: dict[str, str] = {}
    async with db.session() as s:
        for c in clients:
            t = Tenant(slug=c["slug"], name=c["name"], industry=c["industry"], founder_name=c["founder_name"],
                       status="active")
            s.add(t)
            await s.flush()
            slug_to_id[c["slug"]] = t.id
        pw = hash_password(DEMO_PASSWORD)
        for u in USERS:
            user = User(email=u["email"], name=u["name"], role=u["role"], password_hash=pw)
            s.add(user)
            await s.flush()
            for slug in u["tenants"]:
                if slug in slug_to_id:
                    s.add(Membership(user_id=user.id, tenant_id=slug_to_id[slug]))

    for c in clients:
        tid = slug_to_id[c["slug"]]
        with tenant_scope(tid):
            async with db.session() as s:
                intake = dict(c["intake"], founder_name=c["founder_name"], founder_title=c.get("founder_title", ""),
                              company_name=c["name"], industry=c["industry"])
                s.add(ClientProfile(intake=intake, policy=c.get("policy", {})))
                for i, sample in enumerate(c.get("samples", [])):
                    s.add(KnowledgeDoc(kind="founder_sample", source="intake", title=f"Founder post {i + 1}",
                                       content=sample, trusted=True))
                if c["intake"].get("facts"):
                    s.add(KnowledgeDoc(kind="intake_note", source="intake", title="Verified company facts",
                                       content="\n".join(c["intake"]["facts"]), trusted=True))
                for sup in c.get("suppression", []):
                    s.add(SuppressionEntry(**sup))
                audit(s, "system:seed", "tenant.onboarded", "tenant", tid)
    return slug_to_id


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    ids = asyncio.run(seed(reset=args.reset))
    print(f"seeded {len(ids)} tenants")
