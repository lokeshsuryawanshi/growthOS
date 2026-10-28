"""Tests, evals and the demo must never open the production database (the DATABASE_URL in .env)."""
import pytest

from app.core.config import get_settings
from app.db import session as db_session
from app.db.session import Database, ProtectedDatabaseError, guard_not_production


@pytest.fixture
def live_host(monkeypatch):
    monkeypatch.setattr(db_session, "protected_hosts", lambda: {"ep-live-pooler.neon.tech"})


def test_test_run_refuses_the_production_host(live_host):
    for url in ("postgresql://u:p@ep-live-pooler.neon.tech/neondb?sslmode=require",
                "postgres://u:p@EP-LIVE-POOLER.neon.tech/other",
                "postgresql+asyncpg://u:p@ep-live-pooler.neon.tech:5432/neondb"):
        with pytest.raises(ProtectedDatabaseError):
            guard_not_production(url)
        with pytest.raises(ProtectedDatabaseError):
            Database(url)


def test_other_hosts_and_sqlite_are_allowed(live_host):
    guard_not_production("postgresql://u:p@ep-test-branch-pooler.neon.tech/neondb")
    guard_not_production("sqlite+aiosqlite:////tmp/x.db")
    guard_not_production("")


def test_production_is_not_restricted(live_host, monkeypatch):
    monkeypatch.setattr(get_settings(), "app_env", "production")
    guard_not_production("postgresql://u:p@ep-live-pooler.neon.tech/neondb")


def test_protected_hosts_come_from_dotenv_and_test_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@from-env.example/db")
    hosts = db_session.protected_hosts()
    assert "from-env.example" in hosts            # APP_ENV=test: the process DATABASE_URL is protected too
