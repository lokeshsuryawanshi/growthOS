"""Startup tasks: schema, demo data (demo mode only)."""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.db.migrate import upgrade
from app.db.models import Tenant
from app.db.session import Database

log = logging.getLogger(__name__)


def ensure_sqlite_dir(url: str) -> None:
    """A fresh deploy points at e.g. /data/growthos.db; SQLite will not create the directory itself."""
    if url.startswith("sqlite") and ":memory:" not in url:
        path = url.split("///", 1)[-1]
        if path:
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


async def init_database(db: Database) -> None:
    s = get_settings()
    ensure_sqlite_dir(db.url)
    if s.app_env == "production":
        await upgrade(db.url)          # migrations are the only way the production schema is created
    else:
        await db.create_all()          # tests and the local demo use the models directly
    if s.app_env == "demo":
        async with db.reader() as sess:
            has = (await sess.execute(select(Tenant.id).limit(1))).first()
        if not has:
            from app.fixtures.seed import seed   # fixtures exist only in demo/test
            await seed(db)
            log.warning("DEMO MODE: seeded fictional clients and mock models. Nothing here is real.")
