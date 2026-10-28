"""Measure a real end-to-end run on real providers through the same services the UI uses.

  python scripts/live_run.py --profile evals/datasets/voice_samples/seth_godin.json --db <path.db> \
         [--groq-key-var GROQ_API_KEY2] [--runs discovery,content,leads] [--hunter] [--out <report path>]

Each profile gets its own account (so its own keys): the account is created the way Google sign-in creates one, the
client from the profile's onboarding form, and the keys from .env are saved through app.services.keys (with the real
validation call), exactly as an operator would paste them. Keys are never printed. The strategy gate is approved by
the script at once so human review time is not counted. --reuse runs more steps for a client already in --db.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
KEYS: dict[str, str] = {}


def _prepare_env(args) -> None:
    """Server settings for this process; provider keys stay in this module and go into the account, not the env."""
    from dotenv import dotenv_values
    env = dotenv_values(ROOT / ".env")
    for name in ("JWT_SECRET", "ENCRYPTION_KEY"):
        if env.get(name):
            os.environ[name] = env[name]
    KEYS["groq"] = env.get(args.groq_key_var or "GROQ_API_KEY") or ""
    KEYS["tavily"] = env.get("TAVILY_API_KEY") or ""
    KEYS["hunter"] = (env.get("HUNTER_API_KEY") or "") if args.hunter else ""
    if not KEYS["groq"] or not KEYS["tavily"]:
        sys.exit("GROQ_API_KEY (or --groq-key-var) and TAVILY_API_KEY must be set in .env")
    os.environ["APP_ENV"] = "production"
    os.environ["LLM_MODE"] = "live"
    if args.database_url_var:                    # e.g. TEST_DATABASE_URL: a Neon test branch, never the live one
        url = env.get(args.database_url_var) or ""
        if not url or url.split("@")[-1].split("/")[0] == (env.get("DATABASE_URL") or "").split("@")[-1].split("/")[0]:
            sys.exit(f"{args.database_url_var} is empty or names the production database host")
        os.environ["DATABASE_URL"] = url
    else:
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{Path(args.db).resolve()}"


async def wait_run(tid: str, rid: str, poll: float = 1.0, also_wait: tuple[str, ...] = ()) -> str:
    from sqlalchemy import select
    from app.core.tenancy import tenant_scope
    from app.db.models import Run
    from app.db.session import get_db
    while True:
        with tenant_scope(tid):
            async with get_db().reader() as s:
                st = (await s.execute(select(Run.status).where(Run.id == rid))).scalar_one()
        if st not in ("pending", "running", *also_wait):
            return st
        await asyncio.sleep(poll)


async def report_for(tid: str, rid: str) -> dict:
    from sqlalchemy import select
    from app.core.tenancy import tenant_scope
    from app.db.models import LLMCall, Run, RunNode
    from app.db.session import get_db
    with tenant_scope(tid):
        async with get_db().reader() as s:
            run = (await s.execute(select(Run).where(Run.id == rid))).scalars().one()
            nodes = (await s.execute(select(RunNode).where(RunNode.run_id == rid))).scalars().all()
            calls = (await s.execute(select(LLMCall).where(LLMCall.run_id == rid))).scalars().all()
    models: dict = defaultdict(lambda: defaultdict(int))
    for c in calls:
        m = models[c.model]
        m["calls"] += 1
        m["tokens_in"] += c.prompt_tokens
        m["tokens_out"] += c.completion_tokens
        m["model_ms"] += c.latency_ms
        m["pace_wait_ms"] += c.pace_wait_ms
        m["retry_wait_ms"] += c.retry_wait_ms
        m["errors"] += c.status == "error"
        m["cost_usd"] = round(m.get("cost_usd", 0) + c.cost_usd, 6)

    def secs(a, b):
        if not a or not b:
            return None
        return round((b - a).total_seconds(), 1)
    return {
        "kind": run.kind, "status": run.status, "error": run.error, "cost_usd": round(run.cost_usd, 5),
        "wall_s": secs(run.created_at, run.finished_at),
        "nodes": sorted([{"key": n.key, "status": n.status, "s": secs(n.started_at, n.finished_at),
                          "output": {k: v for k, v in (n.output or {}).items() if k != "integration"}}
                         for n in nodes], key=lambda x: (x["s"] is None, -(x["s"] or 0))),
        "calls": len(calls), "tokens_in": sum(c.prompt_tokens for c in calls),
        "tokens_out": sum(c.completion_tokens for c in calls),
        "model_s": round(sum(c.latency_ms for c in calls) / 1000, 1),
        "pace_wait_s": round(sum(c.pace_wait_ms for c in calls) / 1000, 1),
        "retry_wait_s": round(sum(c.retry_wait_ms for c in calls) / 1000, 1),
        "by_model": {k: dict(v) for k, v in models.items()},
    }


async def main(args) -> int:
    from sqlalchemy import select
    from app.agents.orchestrator.engine import get_orchestrator
    from app.api.deps import load_principal
    from app.bootstrap import init_database
    from app.core.config import get_settings
    from app.db.models import Tenant
    from app.db.session import get_db
    from app.services import accounts, keys, runs as run_service

    s = get_settings()
    await init_database(get_db())
    doc = json.loads(Path(args.profile).read_text())
    profile = doc.get("client_profile", doc)
    slug = Path(args.profile).stem
    email = f"live-{slug}@example.com"
    p = await accounts.google_user(f"live:{slug}", email, profile.get("founder_name", slug))
    async with get_db().reader() as sess:
        tenant = (await sess.execute(select(Tenant).where(Tenant.owner_user_id == p.user_id,
                                                          Tenant.name == profile["company_name"]))).scalars().first()
    if tenant is None:
        if args.reuse:
            sys.exit(f"no client {profile['company_name']} in this database")
        tenant = await accounts.create_client(p, accounts.OnboardingForm.from_form(profile))
    saved = await keys.list_keys(p.user_id)
    for provider, kind in (("groq", "llm"), ("tavily", "search"), ("hunter", "contacts")):
        if KEYS.get(provider) and (kind not in saved or saved[kind].status != "valid"):
            await keys.save_key(p, kind, provider, KEYS[provider])        # the real check call, as in the UI
    me = await load_principal(p.user_id)
    orch = get_orchestrator()
    out: dict = {"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "company": tenant.name,
                 "provider": "groq", "routes": {t: s.route_chain("groq", t) for t in
                                                ("big", "research", "small", "extract", "judge", "guard")},
                 "runs": {}}
    kinds = args.runs.split(",")
    t_all = time.monotonic()
    if "discovery" in kinds:
        rid = await run_service.start_run(me, tenant.id, "discovery")
        st = await wait_run(tenant.id, rid)
        print("discovery:", st, flush=True)
        if st == "waiting_human":
            # the founder approves; the script does it at once so review time is not measured
            await orch.approve_gate(tenant.id, rid, "strategy_review", me.user_id)
            st = await wait_run(tenant.id, rid, also_wait=("waiting_human",))
        out["runs"]["discovery"] = await report_for(tenant.id, rid)
        print(json.dumps({k: v for k, v in out["runs"]["discovery"].items() if k != "nodes"}, indent=1), flush=True)
    weekly = [k for k in kinds if k in ("content", "leads")]
    if weekly:
        t_week = time.monotonic()
        rids = {k: await run_service.start_run(me, tenant.id, k) for k in weekly}
        for k, rid in rids.items():
            print(k, await wait_run(tenant.id, rid), flush=True)
            out["runs"][k] = await report_for(tenant.id, rid)
        out["weekly_wall_s"] = round(time.monotonic() - t_week, 1)
    out["total_wall_s"] = round(time.monotonic() - t_all, 1)
    from app.services.kpis import tenant_kpis
    out["kpis"] = await tenant_kpis(tenant.id)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.with_suffix(".json").write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps({k: out[k] for k in ("total_wall_s", "weekly_wall_s") if k in out}), flush=True)
    for k, r in out["runs"].items():
        print(f"\n== {k}: {r['status']} in {r['wall_s']}s, {r['calls']} calls, {r['tokens_in']} in / {r['tokens_out']} out, "
              f"model {r['model_s']}s, pacing {r['pace_wait_s']}s, 429 waits {r['retry_wait_s']}s")
        for n in r["nodes"]:
            print(f"   {n['key']:<16} {n['status']:<10} {n['s']}s  {json.dumps(n['output'])[:160]}")
    await get_db().dispose()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, help="a voice_samples JSON (its client_profile) or an onboarding form JSON")
    ap.add_argument("--reuse", action="store_true", help="the client must already exist in --db")
    ap.add_argument("--db", default="", help="SQLite file for this run (or use --database-url-var)")
    ap.add_argument("--database-url-var", default="", help="name of a .env variable holding a Postgres URL (tests only)")
    ap.add_argument("--groq-key-var", default="")
    ap.add_argument("--runs", default="discovery,content,leads")
    ap.add_argument("--out", default="loadtest/reports/live-run")
    ap.add_argument("--hunter", action="store_true", help="also save HUNTER_API_KEY (optional enrichment)")
    a = ap.parse_args()
    _prepare_env(a)
    sys.exit(asyncio.run(main(a)))
