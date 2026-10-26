import os

os.environ["LLM_MODE"] = "mock"

import pytest

from app.db.session import Database, set_db


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await database.create_all()
    set_db(database)
    yield database
    await database.dispose()
