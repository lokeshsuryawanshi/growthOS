# Growth OS: handoff

State as of 2026-10-30. v2 was built on the branch `feature/v2-single-user`, merged to **`main`** (fast-forward) and
deployed: the live site runs v2 on Neon Postgres and `scripts/check_deploy.py` passes. Untracked on
purpose, never to be committed: `AGENT_PROMPT.md`, `AGENT_PLAN_V2.md`, `coding-round-links.md`, `data/`.

## 1. What this is

A done-for-you growth portal run by one operator for the founders they work for: research, an 8-week LinkedIn
calendar, five or more posts a week in each founder's voice, leads with a real way to reach a person, and short
outreach emails. The operator approves everything; nothing is posted or sent by the system. Start reviewers at
`docs/REVIEWER_GUIDE.md`.

**Measured results** are in `docs/REVIEWER_GUIDE.md` (section 2) and `docs/EVALS_AND_GUARDRAILS.md`. In one line:
tests and the eval gate pass; isolation holds with its mutation test; posts avoid the generic template but are not yet
in each founder's own voice and rarely reach the editor's bar on Groq's free model; leads are traceable and never
invented, but only one of six profiles reached 10 a week. No human has rated anything yet.

## 2. Run it

```bash
DATABASE_URL="sqlite+aiosqlite:///./local-check.db" .venv/bin/uvicorn app.main:app --port 8000   # never the prod DB
.venv/bin/python -m pytest -q                                   # about 45 s on SQLite
.venv/bin/python -m evals.run_evals                             # offline gate
.venv/bin/python scripts/live_run.py --profile evals/datasets/voice_samples/<person>.json --db <file.db> \
    --groq-key-var GROQ_API_KEY2 --runs discovery,content,leads   # a measured live run with your dev keys
.venv/bin/python scripts/check_deploy.py https://growthos-b0t9.onrender.com
```

## 3. What you need to do

1. **Render environment**: set on 2026-10-30. Keep the production `ENCRYPTION_KEY` unchanged from now on (if it
   changes, saved API keys must be re-entered). `APP_BASE_URL` must be exactly `https://growthos-b0t9.onrender.com`,
   with no path. If they are still there, delete the v1 variables:
   `GROQ_API_KEY`, `TAVILY_API_KEY`, `GOOGLE_API_KEY`, `HUNTER_API_KEY`, `SIGNUP_INVITE_CODE`, `ADMIN_EMAIL`,
   `ADMIN_PASSWORD`, `GOOGLE_REDIRECT_URI`, `GLOBAL_DAILY_BUDGET_USD`, `SELF_SERVE_MONTHLY_BUDGET_USD`.
2. **Google Cloud console**: add the redirect URIs `https://growthos-b0t9.onrender.com/auth/google/callback` and
   `http://localhost:8000/auth/google/callback`; consent screen "In production" with scopes `openid email profile`,
   terms `https://growthos-b0t9.onrender.com/termsofservice`, privacy `https://growthos-b0t9.onrender.com/login/policy`.
3. Merged and deployed on 2026-10-30. Run `scripts/check_deploy.py` after every deploy.
4. **Sign in once** on the live site with Google, so a full click-through can be done there (I never type
   passwords).
5. **Rate drafts** (30-45 minutes): `scripts/export_blind_review.py` builds a shuffled, unlabelled sheet; fill
   `ratings.csv`; `scripts/import_ratings.py` reports the result. Until then the honest headline is "no human ratings
   yet".
6. Local `.env`: lines 32 and 33 are not `NAME=value` lines (python-dotenv cannot read them); please fix or remove
   them. `ADMIN_PASSWORD`, `SIGNUP_INVITE_CODE`, `GLOBAL_DAILY_BUDGET_USD`, `SELF_SERVE_MONTHLY_BUDGET_USD`,
   `GROQ_MODEL` and `GOOGLE_REDIRECT_URI` are no longer read and can go.

## 4. Environment variables

Required in production: `JWT_SECRET`, `ENCRYPTION_KEY` (or `ENCRYPTION_KEYS` to rotate), `DATABASE_URL`,
`APP_BASE_URL`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`. Optional: `CONTACT_EMAIL`, `ALLOWED_EMAILS`,
`MODEL_ROUTES_JSON`, `MODEL_LIMITS_JSON`. The server reads **no** provider keys; `GROQ_API_KEY*`, `TAVILY_API_KEY`
and `HUNTER_API_KEY` in `.env` are for development scripts, which save them into a test account through the same
service the UI uses. `TEST_DATABASE_URL` is a separate Neon branch for tests; nothing but production may open the
database in `.env`'s `DATABASE_URL`. Never print or commit `.env`.
