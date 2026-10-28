"""Data-integrity audit. The deployed configuration must never contain or produce synthetic data.

Part 1 checks the settings rules. Part 2 boots the real application as a separate process in production mode,
with a fresh database and no provider keys, and walks it over HTTP.
"""
import ast
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from app.core.config import ConfigError, Settings

ROOT = Path(__file__).resolve().parents[1]
STRONG = "s" * 48
FERNET = "q0x6pUm8wuh9cUQBF1uqnrLKF0UQiS2Mfeg2rKeVc0s="       # a valid Fernet key used only by these tests


# ---------------------------------------------------------------- settings rules
def test_production_forbids_mock_models_and_fixtures():
    s = Settings(_env_file=None, app_env="production", jwt_secret=STRONG, encryption_key=FERNET, llm_mode="auto")
    assert s.fixtures_allowed is False and s.uses_fixture_sources is False
    assert s.effective_llm_mode == "live"                              # each account's own key decides; never a mock
    with pytest.raises(ConfigError):
        Settings(_env_file=None, app_env="production", jwt_secret=STRONG, llm_mode="mock").effective_llm_mode
    with pytest.raises(ConfigError):
        Settings(_env_file=None, app_env="production", jwt_secret=STRONG, encryption_key=FERNET, llm_mode="mock").validate_runtime()
    assert Settings(_env_file=None, app_env="demo", llm_mode="auto").effective_llm_mode == "mock"   # labelled demo only


@pytest.mark.parametrize("secret", ["", "short", "dev-only-change-me-0123456789abcdef"])
def test_production_refuses_weak_or_default_jwt_secret(secret):
    kw = {"jwt_secret": secret} if secret else {}
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        Settings(_env_file=None, app_env="production", llm_mode="auto", encryption_key=FERNET, **kw).validate_runtime()
    Settings(_env_file=None, app_env="production", llm_mode="auto", jwt_secret=STRONG, encryption_key=FERNET).validate_runtime()


@pytest.mark.parametrize("key", ["", "any passphrase at all", "x" * 44, f"{'y' * 44},{'z' * 44}"])
def test_production_refuses_a_missing_or_invalid_encryption_key(key):
    with pytest.raises(ConfigError, match="ENCRYPTION_KEY"):
        Settings(_env_file=None, app_env="production", llm_mode="auto", jwt_secret=STRONG, encryption_key=key).validate_runtime()
    Settings(_env_file=None, app_env="production", llm_mode="auto", jwt_secret=STRONG, encryption_keys=f"{FERNET},{FERNET}").validate_runtime()


PROVIDER_ENV = ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TAVILY_API_KEY", "HUNTER_API_KEY", "GOOGLE_API_KEY")


def test_the_app_never_reads_provider_keys_from_the_environment(monkeypatch):
    """BYOK: the server holds no provider key. Settings have no such fields, and no app module reads one from os.environ."""
    for name in PROVIDER_ENV:
        monkeypatch.setenv(name, "from-the-environment-0123456789")
    s = Settings(_env_file=None, app_env="production", llm_mode="auto", jwt_secret=STRONG, encryption_key=FERNET)
    dumped = str(s.model_dump())
    assert "from-the-environment" not in dumped
    offenders = [str(p) for p in (ROOT / "app").rglob("*.py")
                 if any(name in p.read_text() for name in PROVIDER_ENV)]
    assert offenders == []


def test_importing_fixtures_in_production_fails():
    r = subprocess.run([sys.executable, "-c", "import app.fixtures"], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "APP_ENV": "production", "JWT_SECRET": STRONG})
    assert r.returncode != 0 and "FixturesDisabled" in r.stderr


def test_no_production_module_imports_fixtures_unconditionally():
    """Static audit: outside app/fixtures, any import of app.fixtures must sit inside a function or an `if`."""
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        if "fixtures" in path.relative_to(ROOT / "app").parts:
            continue
        for node in ast.parse(path.read_text()).body:                  # module-level statements only
            mods = [node.module] if isinstance(node, ast.ImportFrom) and node.module else \
                [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if any(m and m.startswith("app.fixtures") for m in mods):
                offenders.append(str(path))
    assert offenders == []


def test_no_mock_adapters_are_reachable_from_the_approval_paths():
    text = (ROOT / "app/services/approvals.py").read_text()
    assert "Mock" not in text and "fixtures" not in text


# ---------------------------------------------------------------- the real app, production mode
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def prod_server(tmp_path_factory):
    port, db = free_port(), tmp_path_factory.mktemp("prod") / "prod.db"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GROQ", "GOOGLE", "TAVILY", "HUNTER", "LLM_MODE", "APP_ENV"))}
    env.update(APP_ENV="production", JWT_SECRET=STRONG, ENCRYPTION_KEY=FERNET, DATABASE_URL=f"sqlite+aiosqlite:///{db}",
               APP_BASE_URL="http://127.0.0.1")
    env["PYTHONPATH"] = str(ROOT)
    # cwd is a scratch directory so the project's .env (real keys) can never leak into this hermetic server
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
                            cwd=db.parent, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("production server did not start:\n" + (proc.stdout.read() if proc.stdout else ""))
    yield base, db
    proc.terminate()
    proc.wait(timeout=10)


def test_production_boots_empty_with_no_demo_accounts_and_no_test_login(prod_server):
    base, db = prod_server
    h = httpx.get(base + "/healthz").json()
    assert h == {"ok": True, "env": "production", "llm_mode": "live", "fixtures": False, "database": "sqlite",
                 "ephemeral_storage": False}
    # the sign-in shortcut used by tests and the demo does not exist here, and nor do passwords or signup
    for path in ("/auth/test-login", "/login", "/signup", "/api/auth/login", "/api/auth/signup"):
        r = httpx.post(base + path, data={"email": "operator@growthos.example", "password": "growthos-demo"})
        assert r.status_code in (404, 405), (path, r.status_code)
    import sqlite3
    con = sqlite3.connect(db)
    assert con.execute("select count(*) from tenants").fetchone()[0] == 0
    assert con.execute("select count(*) from users").fetchone()[0] == 0
    assert con.execute("select version_num from alembic_version").fetchone()[0]          # schema came from migrations
    page = httpx.get(base + "/login").text
    assert "DEMO" not in page and "Test sign-in" not in page and "Continue with Google" in page or "not set up" in page
    for path in ("/termsofservice", "/login/policy"):
        assert httpx.get(base + path).status_code == 200


def _session_for_new_account(db_path) -> dict:
    """Production has no test login. Insert an account the way Google sign-in would and sign its session cookie with
    the server's own secret, which is exactly what a real sign-in produces."""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    import jwt
    con = sqlite3.connect(db_path)
    con.execute("insert into users (id, google_sub, email, name, created_at) values ('u-prod', 'g-prod', "
                "'dana@prod.example', 'Dana Cho', '2026-10-29 10:00:00')")
    con.commit()
    now = datetime.now(timezone.utc)
    return {"growthos_token": jwt.encode({"sub": "u-prod", "iat": now, "exp": now + timedelta(hours=1)}, STRONG,
                                         algorithm="HS256")}


def test_production_user_journey_produces_no_synthetic_output(prod_server):
    from tests.helpers import FORM
    base, db = prod_server
    with httpx.Client(base_url=base, cookies=_session_for_new_account(db)) as c:
        r = c.post("/clients/new", data=FORM)
        assert r.status_code == 303, r.text[:300]
        tid = r.headers["location"].split("/portal/")[1].split("?")[0]
        for tab in ("overview", "content", "calendar", "leads", "research", "activity", "settings"):
            page = c.get(f"/portal/{tid}?tab={tab}")
            assert page.status_code == 200 and "synthetic" not in page.text.lower() and "DEMO" not in page.text, tab
        # No key saved for this account: starting research is refused with the fix, never answered with mock output.
        r = c.post(f"/ui/{tid}/runs", data={"kind": "discovery"})
        assert r.status_code == 303
        assert "No AI provider" in c.get(r.headers["location"]).text
        assert "Nothing has run yet" in c.get(f"/portal/{tid}?tab=activity").text


def test_hosted_postgres_urls_are_accepted_as_given():
    from app.core.config import Settings
    s = Settings(app_env="test", database_url="postgres://u:p@ep-1.neon.tech/db?sslmode=require&channel_binding=require")
    assert s.database_url == "postgresql+asyncpg://u:p@ep-1.neon.tech/db?ssl=require"
    assert Settings(app_env="test", database_url="sqlite+aiosqlite:////tmp/g.db").database_url == "sqlite+aiosqlite:////tmp/g.db"


def test_tmp_sqlite_is_flagged_as_ephemeral_storage():
    from app.core.config import Settings
    assert Settings(app_env="test", database_url="sqlite+aiosqlite:////tmp/growthos.db").ephemeral_storage
    assert not Settings(app_env="test", database_url="sqlite+aiosqlite:////data/growthos.db").ephemeral_storage
    assert not Settings(app_env="test", database_url="postgres://u@h/db").ephemeral_storage
    assert Settings(app_env="test", database_url="sqlite+aiosqlite:////data/g.db", EPHEMERAL_STORAGE="true").ephemeral_storage


def test_migrations_match_the_models(tmp_path):
    """Production builds its schema only from Alembic migrations; they must produce exactly the models' schema."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    from app.db.migrate import upgrade_sync
    from app.db.models import Base
    url = f"sqlite+aiosqlite:///{tmp_path}/drift.db"
    upgrade_sync(url)
    engine = create_engine(f"sqlite:///{tmp_path}/drift.db")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    engine.dispose()
    assert diff == [], diff


def test_an_empty_optional_setting_in_env_does_not_stop_startup():
    from app.core.config import Settings
    assert Settings(_env_file=None, app_env="test", EPHEMERAL_STORAGE="", database_url="sqlite+aiosqlite:////tmp/x.db").ephemeral_storage
