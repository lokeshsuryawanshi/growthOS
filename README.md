# Growth OS

A done-for-you growth portal for busy founders. Agents research the founder's market, competitors and voice from real web sources, plan an 8-week LinkedIn calendar, draft posts, find qualified leads and draft outreach emails. The founder only **approves, edits or rejects**. Nothing is posted or emailed without a human.

- Plan and architecture: [PLAN.md](PLAN.md) · Decisions and lessons: [DECISIONS.md](DECISIONS.md) · Deploying: [docs/DEPLOY.md](docs/DEPLOY.md) · Handoff notes: [HANDOFF.md](HANDOFF.md)

## Honesty rules (enforced in code and tests)

- **Production never shows or produces synthetic data.** All fictional clients, prospects, web pages and mock models live in `app/fixtures/` and cannot be imported unless `APP_ENV` is `demo` or `test`. A test boots the real app in production mode on an empty database and checks this.
- **No silent fallbacks.** If a key is missing or a provider fails, the step stops and the UI shows what failed and how to fix it (HTTP 424 from the API). Nothing is replaced by canned output.
- **Sources are verified.** A cited URL must have actually come back from a search or page fetch; otherwise the claim is dropped and counted. Numbers in posts need a verbatim source quote and must be used for what the source says.
- **New accounts start empty.** Sign up, answer the onboarding form, get a clean workspace with empty states.

## Run it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill it in (below)
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and sign up. **Use a new database file** (e.g. `DATABASE_URL=sqlite+aiosqlite:///./data/growthos.db`); do not reuse an old `growthos.db` that was seeded with demo clients. The schema is created by Alembic at startup.

Labelled synthetic demo (mock models, fictional clients, nothing real, local only):

```bash
APP_ENV=demo LLM_MODE=mock DATABASE_URL=sqlite+aiosqlite:///./demo_mock.db .venv/bin/uvicorn app.main:app --port 8010
```
Demo logins are shown on the login page (a red banner marks demo mode).

## Configuration (`.env`)

| Variable | Needed for | Notes |
|---|---|---|
| `JWT_SECRET` | **required** | 32+ random chars; the app refuses to start in production without it |
| `GROQ_API_KEY` | **required** | writing, strategy, scoring (console.groq.com) |
| `TAVILY_API_KEY` | **required for research** (or Gemini billing) | free tier at tavily.com. Gemini Google Search grounding also works if billing is enabled on `GOOGLE_API_KEY`; it reports quota errors honestly |
| `GOOGLE_API_KEY` | optional | Gemini judge for the writer/critic loop; falls back to a Groq Qwen judge |
| `HUNTER_API_KEY` | optional | find and verify lead emails. Without it leads are companies with no contact and no outreach is drafted. Free plan is ~25 searches/month |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET`, `GOOGLE_REDIRECT_URI` | optional | Gmail sending (`GOOGLE_CLIENT_ID`/`_SECRET` accepted too). Scope `gmail.send` only. Setup in [docs/DEPLOY.md](docs/DEPLOY.md) |
| `DATABASE_URL` | recommended | persistent path in production; created if missing |
| `APP_BASE_URL` | deploy | public https URL (OAuth redirect, secure cookies) |
| `SIGNUP_INVITE_CODE`, `ADMIN_EMAIL`/`ADMIN_PASSWORD` | optional | gate signup; create a platform admin for the ops console |
| `GLOBAL_DAILY_BUDGET_USD`, `SELF_SERVE_MONTHLY_BUDGET_USD` | optional | spend caps (5 and 2 by default) |
| `ENCRYPTION_KEY` | optional | Fernet key for stored OAuth tokens (derived from `JWT_SECRET` if empty) |

The Integrations page in the app shows what is connected and how to fix what is not.

## How it works for a founder

1. **Onboarding:** company, website, competitors, writing samples, brand rules, do-not-contact list, goals (posts/week, leads/week).
2. **Research** (about 10 minutes on Groq's free tier): reads your site, searches the web, builds voice profile, market, competitors, trends, strategy, ICP and an 8-week calendar, with provenance. You approve the strategy.
3. **Weekly cycle:** drafts posts (writer ↔ critic loop, guardrails), finds leads (web search + Hunter), drafts emails.
4. **You decide:** approve/edit/reject posts (copy to LinkedIn, then "I posted it"; no social integration). Approve emails, then send from your own Gmail.

Roles: founder (`client_owner`), read-only viewer, account manager and platform admin (ops console: runs, traces, costs, guardrails, audit log, kill switch).

## Code map

| Topic | Where |
|---|---|
| Agentic loops | `app/agents/runtime/loop.py`, writer↔critic in `app/agents/content/agents.py`, lead loop in `app/agents/leads/agents.py` |
| Coordination | `app/agents/orchestrator/` (DAGs, checkpoints, retries, gates, fair scheduler) |
| Real integrations | `app/integrations/` (search chain, safe fetch, Hunter, Gmail OAuth, status) |
| Cost | `app/llm/gateway.py` (tiers, buckets, run/tenant/global budgets, kill switch, ledger) |
| Guardrails | `app/guardrails/` (input, output incl. claim-level grounding, state machines) |
| Security | `app/core/security.py` (RBAC), `app/db/session.py` (tenant interceptor), `migrations/postgres_rls.sql`, rate limits in `app/core/ratelimit.py` |
| Fixtures (tests/demo only) | `app/fixtures/` |

## Tests

```bash
.venv/bin/python -m pytest -q        # 150 tests, ~1 minute
```
Covers tenancy, gateway, guardrails, grounding regressions, integrations (mock HTTP), orchestration failure/recovery, the send gate, accounts, and a production-mode boot audit.

## Status and known limitations

- The evals (`python -m evals.run_evals`) and load test (`python -m loadtest.stress --tenants 100`) run on mock models and fixtures. Their last reports (`evals/reports`, `loadtest/reports`) **predate the latest refactor and have not been re-run**; treat them as historical.
- Orchestrator and rate limiter are in-process; SQLite has one writer. Path to scale: Postgres (RLS migration included), a durable job queue, Redis rate limits. Run a single worker.
- No LinkedIn publishing, no email verification or password reset, no CSRF token beyond `SameSite=Lax`.
- Not yet deployed. Hunter and Gmail were tested against mock HTTP only; a live run with the latest code is pending (see HANDOFF.md).
