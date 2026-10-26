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

import pytest

from app.db.session import Database, set_db


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
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
