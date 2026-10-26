"""Run Alembic migrations programmatically (used at startup so a fresh deploy creates its own schema)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]


def _config(url: str | None = None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    if url:
        cfg.attributes["url"] = url
    return cfg


def upgrade_sync(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(_config(url), revision)


async def upgrade(url: str | None = None) -> None:
    await asyncio.to_thread(upgrade_sync, url)   # env.py runs its own event loop, so it needs its own thread
