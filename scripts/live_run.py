"""Measure a real end-to-end run on real providers: fresh database, one workspace, research and strategy, then the
content and lead runs side by side. Prints and saves where the time and tokens went.

  python scripts/live_run.py --profile path/to/profile.json [--db data/live.db] [--groq-key-var GROQ_API_KEY2]
                             [--runs discovery,content,leads] [--out loadtest/reports/live-run]

The profile is the onboarding form as JSON (same field names as the web form, samples as sample_1..sample_5).
The strategy gate is approved by the script the moment it is reached, so human review time is not counted.
--groq-key-var picks which key from .env to use for this manual run (it is copied into GROQ_API_KEY for this process
only; the app itself reads only GROQ_API_KEY and never rotates keys). Keys are never printed.
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


def _prepare_env(args) -> None:
    from dotenv import dotenv_values
    env = dotenv_values(ROOT / ".env")
    if args.groq_key_var:
        key = env.get(args.groq_key_var) or os.environ.get(args.groq_key_var)
        if not key:
            sys.exit(f"{args.groq_key_var} is not set")
        os.environ["GROQ_API_KEY"] = key
    if args.no_hunter:
        os.environ["HUNTER_API_KEY"] = ""          # keep the monthly Hunter quota for measured runs
    os.environ["APP_ENV"] = "production"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{Path(args.db).resolve()}"
    os.environ.setdefault("LLM_MODE", "live")


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
    from app.agents.orchestrator.engine import get_orchestrator
    from app.bootstrap import init_database
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.services import accounts, runs as run_service

    s = get_settings()
    assert s.effective_llm_mode == "live", "needs a Groq key"
    await init_database(get_db())
    profile = json.loads(Path(args.profile).read_text())
    email = f"live-{int(time.time())}@example.com"
    p = await accounts.signup(profile.get("founder_name", "Founder"), email, "live-run-password-123",
                              s.signup_invite_code)
    tenant = await accounts.create_workspace(p, accounts.OnboardingForm.from_form(profile))
    from app.core.security import Principal
    me = Principal(p.user_id, p.name, p.role, frozenset([tenant.id]))
    orch = get_orchestrator()
    out: dict = {"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "company": tenant.name,
                 "routes": {t: s.route_chain(t) for t in ("big", "research", "small", "extract", "judge", "guard")},
                 "runs": {}}
    kinds = args.runs.split(",")
    t_all = time.monotonic()
    if "discovery" in kinds:
        rid = await run_service.start_run(me, tenant.id, "discovery")
        st = await wait_run(tenant.id, rid)
        print("discovery:", st, flush=True)
        if st == "waiting_human":
            # the founder approves; the script does it at once so review time is not measured
            await orch.approve_gate(tenant.id, rid, "strategy_review", p.user_id)
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
    ap.add_argument("--profile", required=True)
    ap.add_argument("--db", default=f"data/live-{int(time.time())}.db")
    ap.add_argument("--groq-key-var", default="")
    ap.add_argument("--runs", default="discovery,content,leads")
    ap.add_argument("--out", default="loadtest/reports/live-run")
    ap.add_argument("--no-hunter", action="store_true")
    a = ap.parse_args()
    _prepare_env(a)
    sys.exit(asyncio.run(main(a)))
