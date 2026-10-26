"""Startup tasks: schema, optional platform admin, demo data (demo mode only)."""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.migrate import upgrade
from app.db.models import Tenant, User
from app.db.session import Database

log = logging.getLogger(__name__)


async def init_database(db: Database) -> None:
    s = get_settings()
    if s.app_env == "production":
        await upgrade(db.url)          # migrations are the only way the production schema is created
    else:
        await db.create_all()          # tests and the local demo use the models directly
    if s.admin_email and s.admin_password:
        email = s.admin_email.strip().lower()
        async with db.session() as sess:
            if (await sess.execute(select(User.id).where(User.email == email))).first() is None:
                sess.add(User(email=email, name="Platform admin", role="platform_admin",
                              password_hash=hash_password(s.admin_password)))
                log.info("created platform admin %s", email)
    if s.app_env == "demo":
        async with db.reader() as sess:
            has = (await sess.execute(select(Tenant.id).limit(1))).first()
        if not has:
            from app.fixtures.seed import seed   # fixtures exist only in demo/test
            await seed(db)
            log.warning("DEMO MODE: seeded fictional clients and mock models. Nothing here is real.")
