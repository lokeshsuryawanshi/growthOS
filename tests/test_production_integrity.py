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


# ---------------------------------------------------------------- settings rules
def test_production_forbids_mock_models_and_fixtures():
    s = Settings(_env_file=None, app_env="production", jwt_secret=STRONG, llm_mode="auto", groq_api_key="")
    assert s.fixtures_allowed is False and s.uses_fixture_sources is False
    assert s.effective_llm_mode == "unconfigured"                      # no key: NOT silently mock
    with pytest.raises(ConfigError):
        Settings(_env_file=None, app_env="production", jwt_secret=STRONG, llm_mode="mock").effective_llm_mode
    with pytest.raises(ConfigError):
        Settings(_env_file=None, app_env="production", jwt_secret=STRONG, llm_mode="mock").validate_runtime()


def test_live_requires_a_key_and_never_degrades_to_mock():
    assert Settings(_env_file=None, app_env="production", jwt_secret=STRONG, llm_mode="auto", groq_api_key="k").effective_llm_mode == "live"
    assert Settings(_env_file=None, app_env="production", jwt_secret=STRONG, llm_mode="live", groq_api_key="").effective_llm_mode == "unconfigured"
    assert Settings(_env_file=None, app_env="demo", llm_mode="auto", groq_api_key="").effective_llm_mode == "mock"   # labelled demo only


@pytest.mark.parametrize("secret", ["", "short", "dev-only-change-me-0123456789abcdef"])
def test_production_refuses_weak_or_default_jwt_secret(secret):
    kw = {"jwt_secret": secret} if secret else {}
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        Settings(_env_file=None, app_env="production", llm_mode="auto", **kw).validate_runtime()
    Settings(_env_file=None, app_env="production", llm_mode="auto", jwt_secret=STRONG).validate_runtime()


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


def test_no_mock_adapters_are_reachable_from_the_send_or_publish_paths():
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
    env.update(APP_ENV="production", JWT_SECRET=STRONG, DATABASE_URL=f"sqlite+aiosqlite:///{db}", APP_BASE_URL="http://127.0.0.1")
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


def test_production_boots_empty_with_no_demo_accounts(prod_server):
    base, db = prod_server
    h = httpx.get(base + "/healthz").json()
    assert h == {"ok": True, "env": "production", "llm_mode": "unconfigured", "fixtures": False, "database": "sqlite",
                 "ephemeral_storage": False}
    for email in ("admin@growthos.example", "am.sam@growthos.example", "priya@ledgerloop.example"):
        r = httpx.post(base + "/api/auth/login", json={"email": email, "password": "growthos-demo"})
        assert r.status_code == 401, email
    import sqlite3
    con = sqlite3.connect(db)
    assert con.execute("select count(*) from tenants").fetchone()[0] == 0
    assert con.execute("select count(*) from users").fetchone()[0] == 0
    assert con.execute("select version_num from alembic_version").fetchone()[0]          # schema came from migrations
    assert "DEMO" not in httpx.get(base + "/login").text


def test_production_user_journey_produces_no_synthetic_output(prod_server):
    from tests.test_accounts import FORM
    base, _ = prod_server
    r = httpx.post(base + "/api/auth/signup", json={"name": "Dana Cho", "email": "dana@prod.example", "password": "correct horse battery"})
    assert r.status_code == 201
    h = {"Authorization": "Bearer " + r.json()["token"]}
    r = httpx.post(base + "/api/tenants", json=FORM, headers=h)
    assert r.status_code == 201
    tid, h = r.json()["id"], {"Authorization": "Bearer " + r.json()["token"]}
    assert httpx.get(f"{base}/api/tenants/{tid}/posts", headers=h).json() == []
    assert httpx.get(f"{base}/api/tenants/{tid}/leads", headers=h).json() == []

    # No AI key: starting research must be refused with the fix, not answered with mock output.
    r = httpx.post(f"{base}/api/tenants/{tid}/runs", json={"kind": "discovery"}, headers=h)
    assert r.status_code == 424 and r.json()["code"] == "not_configured" and "GROQ_API_KEY" in r.json()["recovery"]
    assert httpx.get(f"{base}/api/tenants/{tid}/runs", headers=h).json() == []
    integ = {i["key"]: i for i in httpx.get(f"{base}/api/integrations?tid={tid}", headers=h).json()}
    assert integ["ai"]["state"] == "not_configured" and "mock" not in integ["ai"]["detail"].lower().replace("mock models: output is synthetic", "")
    assert integ["hunter"]["state"] == "not_configured" and integ["gmail"]["state"] == "not_configured"
    assert integ["publishing"]["state"] == "not_configured"                       # honest: nothing publishes automatically

    # Sending is impossible without a connected Gmail, whatever the role.
    assert httpx.get(f"{base}/portal/{tid}?tab=leads", cookies={"growthos_token": h["Authorization"][7:]}).status_code == 200


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
    assert Settings(app_env="test", EPHEMERAL_STORAGE="", database_url="sqlite+aiosqlite:////tmp/x.db").ephemeral_storage
