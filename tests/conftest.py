import os

os.environ["APP_ENV"] = "test"
os.environ["LLM_MODE"] = "mock"
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("APP_BASE_URL", "http://testserver")
os.environ["GROQ_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""
os.environ["TAVILY_API_KEY"] = ""
os.environ["HUNTER_API_KEY"] = ""
os.environ["GOOGLE_REDIRECT_URI"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["GOOGLE_CLIENT_SECRET"] = ""
# Anything that falls through to get_settings().database_url gets a throwaway file, never the .env (live) database.
import tempfile
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mkdtemp(prefix='growthos-test-')}/fallback.db"

from app.core.config import Settings
Settings.model_config["env_file"] = None      # tests are hermetic: nothing from the developer's .env leaks in

from app.db.session import guard_not_production
guard_not_production(os.environ.get("TEST_DATABASE_URL", ""))   # refuse to start against the live database

import pytest

from app.db.session import Database, set_db


_pg_schema_ready = False


@pytest.fixture
async def db(tmp_path):
    """SQLite per test by default. TEST_DATABASE_URL=postgres://... runs the same tests on Postgres: the schema is
    rebuilt once per session, and every table is emptied before each test (one TRUNCATE, fast over the network)."""
    global _pg_schema_ready
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path}/test.db"
    database = Database(url)
    if url.startswith("sqlite"):
        await database.create_all()
    else:
        if not _pg_schema_ready:
            await database.drop_all()
            await database.create_all()
            _pg_schema_ready = True
        from sqlalchemy import text
        from app.db.models import Base
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        async with database.write_engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    set_db(database)
    yield database
    await database.dispose()


@pytest.fixture(autouse=True)
def _clean_globals():
    from app.core.ratelimit import limiter
    from tests.helpers import reset_http
    limiter.reset()
    reset_http()
    yield
    reset_http()
