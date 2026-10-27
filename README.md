# Growth OS

A done-for-you growth portal for busy founders. Agents research the founder's market, competitors and voice from real web sources, plan an 8-week LinkedIn calendar, draft posts, find qualified leads and draft outreach emails. The founder only **approves, edits or asks for changes**. Nothing is posted or emailed without a human.

**Start here: [docs/REVIEWER_GUIDE.md](docs/REVIEWER_GUIDE.md)** (two-page overview with measured results).

Docs: [Architecture](docs/ARCHITECTURE.md) · [Evals and guardrails](docs/EVALS_AND_GUARDRAILS.md) · [Cost and run time](docs/ECONOMICS.md) · [Deploying](docs/DEPLOY.md) · [Decisions and lessons](DECISIONS.md) · [Original plan](PLAN.md) · [Handoff notes](HANDOFF.md) · [Live run report](loadtest/reports/live-run.md)

Live: https://growthos-b0t9.onrender.com (free Render plan; see DEPLOY.md for how data is kept).

## Honesty rules (enforced in code and tests)

- **Production never shows or produces synthetic data.** Fictional clients, prospects, web pages and mock models live in `app/fixtures/` and cannot be imported unless `APP_ENV` is `demo` or `test`. A test boots the real app in production mode on an empty database and checks this.
- **No silent fallbacks.** If a key is missing or a provider fails, the step stops and the UI shows what failed and how to fix it (HTTP 424 from the API). Nothing is replaced by canned output.
- **Sources are verified.** A cited URL must have come back from a search or a page read, and the claim must be supported by that page's text; otherwise it is flagged, not shown as a fact. Figures in posts need a verbatim source quote and must be used for what the source says.
- **Targets are reported honestly.** A run that misses the client's weekly targets ends "succeeded with shortfall" and says why.

## Run it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill it in (below)
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and sign up. **Use a new database** (e.g. `DATABASE_URL=sqlite+aiosqlite:///./data/growthos.db`, or any `postgres://` URL); never reuse an old `growthos.db` seeded with demo clients. The schema is created by Alembic at startup.

Labelled synthetic demo (mock models, fictional clients, local only):

```bash
APP_ENV=demo LLM_MODE=mock DATABASE_URL=sqlite+aiosqlite:///./demo_mock.db .venv/bin/uvicorn app.main:app --port 8010
```

## Configuration (`.env`)

| Variable | Needed for | Notes |
|---|---|---|
| `JWT_SECRET` | **required** | 32+ random characters; production refuses to start without it |
| `GROQ_API_KEY` | **required** | writing, strategy, scoring (console.groq.com) |
| `TAVILY_API_KEY` | **required for research** | free tier at tavily.com; Gemini grounding is a fallback if billing is enabled on the Google key |
| `GOOGLE_API_KEY` | recommended | Gemini 3.5 Flash-Lite: the critic and the large-input extraction model (free tier at aistudio.google.com) |
| `HUNTER_API_KEY` | optional | find and verify lead emails; without it leads are companies with no contact. The Integrations page shows the quota left |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET`, `GOOGLE_REDIRECT_URI` | optional | Gmail sending, `gmail.send` scope only. Setup in [docs/DEPLOY.md](docs/DEPLOY.md) |
| `DATABASE_URL` | recommended | SQLite path or any Postgres URL (`postgres://...?sslmode=require` works as given) |
| `APP_BASE_URL` | deploy | public https URL (OAuth redirect, secure cookies) |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | optional | creates a platform admin at startup (data view, account managers) |
| `SIGNUP_INVITE_CODE` | optional | gate signup on a public link |
| `ROUTE_BIG` ... `ROUTE_GUARD`, `MODEL_LIMITS_JSON` | optional | model routing per tier and per-model limits (see `.env.example`) |
| `GLOBAL_DAILY_BUDGET_USD`, `SELF_SERVE_MONTHLY_BUDGET_USD` | optional | spend caps (5 and 2 by default) |
| `ENCRYPTION_KEY` | optional | any long random value; encrypts stored Gmail tokens (derived from `JWT_SECRET` if empty) |

## How it works for a founder

1. **Onboarding:** company, website, competitors, writing samples, brand rules, goals (posts per week, leads per week).
2. **Research and strategy** (about 1.5 minutes on free tiers): reads the site, searches the web, builds the voice profile, market, competitors, trends, strategy, ideal customer profile and an 8-week calendar, each claim tied to a checked source. The founder approves it or **suggests changes**.
3. **Posts** (about 2.5 minutes): the week's posts drafted, critiqued, checked by guardrails and redrafted until they clear the bar.
4. **Leads** (under a minute): companies that fit, with quoted evidence, verified contacts and drafted emails.
5. **The founder decides:** approve, edit or reject posts (copy to LinkedIn, then "I posted it"); approve emails, then send from their own Gmail. Teammates can be added as read-only viewers or co-owners on the Team page.

Roles: founder (`client_owner`), read-only viewer, account manager and platform admin (ops console, data view).

## Code map

| Topic | Where |
|---|---|
| Agentic loops | `app/agents/research/` (decide-act research loop), `app/agents/content/` (writer, critic, redrafts), `app/agents/leads/`, `app/agents/runtime/loop.py` |
| Coordination | `app/agents/orchestrator/` (event-driven DAGs, checkpoints, retries, gates, fair scheduler) |
| Model routing and cost | `app/llm/gateway.py`, `app/llm/providers.py` (tiers, spillover, per-model pacing, budgets, ledger), `app/db/usage.py` |
| Guardrails | `app/guardrails/` (input, output incl. claim-level grounding, state machines) |
| Security | `app/core/security.py` (RBAC), `app/services/team.py`, `app/services/admin.py`, `app/db/session.py` (tenant interceptor), `migrations/postgres_rls.sql` |
| Evals | `evals/run_evals.py`, `evals/rbac.py`, `evals/rbac_mutations.py`, `evals/quality.py` |
| Load and live measurement | `loadtest/stress.py`, `scripts/live_run.py` |
| Fixtures (tests and demo only) | `app/fixtures/` |

## Tests and evals

```bash
.venv/bin/python -m pytest -q                 # 224 tests, ~2.5 min (TEST_DATABASE_URL=postgresql+asyncpg://... runs them on Postgres)
.venv/bin/python -m evals.run_evals           # offline gate: guardrails, pipeline, quality, RBAC (315 checks)
.venv/bin/python -m evals.run_evals --live    # red team with the live classifier, judge calibration
.venv/bin/python -m evals.rbac_mutations      # breaks access control on purpose; the eval must notice
.venv/bin/python -m loadtest.stress --tenants 100
```

## Known limitations

- Gmail sending is tested against mock HTTP; a live send needs the founder's own Google consent (use "Add a lead manually" with your own address).
- On the free Render plan SQLite is wiped on restart or idle spin-down (a banner says so); set `DATABASE_URL` to a hosted Postgres to keep data.
- Orchestrator and rate limiter are in-process: run one worker. Path to scale: a durable queue, Redis rate limits, Postgres with RLS.
- No LinkedIn publishing (by design), no email verification or password reset for accounts.
