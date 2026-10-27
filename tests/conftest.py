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

import pytest

from app.db.session import Database, set_db


@pytest.fixture
async def db(tmp_path):
    """SQLite per test by default. TEST_DATABASE_URL=postgresql+asyncpg://... runs the same tests on Postgres."""
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path}/test.db"
    database = Database(url)
    if not url.startswith("sqlite"):
        await database.drop_all()
    await database.create_all()
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
