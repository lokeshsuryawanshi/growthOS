"""Does data survive a restart on Postgres? Run against the TEST branch (TEST_DATABASE_URL), in production mode, one
phase per process:

  python scripts/check_persistence.py reset   # empty the test branch's schema (refuses the .env DATABASE_URL host)
  python scripts/check_persistence.py write   # migrations, then an account, a client and a Tavily key (real check call)
  # restart anything you like here, e.g. boot the app against the same URL
  python scripts/check_persistence.py read    # the same rows, read by a new process

Prints names, statuses and the migration version only, never a URL or a key."""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values  # noqa: E402

env = dotenv_values(ROOT / ".env")
url, prod = env.get("TEST_DATABASE_URL") or "", env.get("DATABASE_URL") or ""
host = lambda u: u.split("@")[-1].split("/")[0].split("?")[0]  # noqa: E731
if not url or host(url) == host(prod):
    sys.exit("TEST_DATABASE_URL missing or names the production host")
os.environ.update(APP_ENV="production", LLM_MODE="live", DATABASE_URL=url,
                  JWT_SECRET=env["JWT_SECRET"], ENCRYPTION_KEY=env["ENCRYPTION_KEY"])
EMAIL = "persistence-check@example.invalid"


async def main(phase: str) -> None:
    from sqlalchemy import select, text
    from app.db.session import get_db
    db = get_db()
    if phase == "reset":
        async with db.write_engine.begin() as c:
            await c.execute(text("DROP SCHEMA public CASCADE"))
            await c.execute(text("CREATE SCHEMA public"))
        print("test branch schema reset")
        return
    from app.db.models import AccountSecret, Tenant, User
    if phase == "write":
        import json
        from app.bootstrap import init_database
        from app.services import accounts, keys
        from app.api.deps import load_principal
        await init_database(db)                      # production: Alembic migrations create the schema
        p = await accounts.google_user("persist-check-sub", EMAIL, "Persistence Check")
        profile = json.loads((ROOT / "evals/datasets/voice_samples/arvid_kahl.json").read_text())["client_profile"]
        t = await accounts.create_client(p, accounts.OnboardingForm.from_form(profile))
        p = await load_principal(p.user_id)
        await keys.save_key(p, "search", "tavily", env["TAVILY_API_KEY"])
        print("wrote account, client", t.name, "and a search key")
        return
    async with db.reader() as s:
        users = (await s.execute(select(User.email))).scalars().all()
        tenants = (await s.execute(select(Tenant.name))).scalars().all()
        secrets = (await s.execute(select(AccountSecret.kind, AccountSecret.status, AccountSecret.last4))).all()
        ver = (await s.execute(text("select version_num from alembic_version"))).scalars().all()
    print("users", users, "clients", tenants, "keys", [(k, st, "len4" if len(l4) == 4 else l4) for k, st, l4 in secrets],
          "migration", ver)


asyncio.run(main(sys.argv[1]))
