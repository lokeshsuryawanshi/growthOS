# Growth OS: handoff

State as of 2026-10-27 (evening). All work is on the branch **`feature/improvements`**, committed in small steps, **not pushed and not merged**. `main` is untouched, so the live site (which auto-deploys from `main`) still runs the old code. Untracked on purpose: `AGENT_PROMPT.md`, `coding-round-links.md` (the user's own files).

## 1. What this is

Take-home for an AI analyst role: a done-for-you growth portal for founders (LinkedIn content, personal brand, lead generation). Agents research, plan an 8-week calendar, draft at least 5 posts a week, find leads and draft outreach emails; a human approves everything. The reviewer reads the repo; start them at `docs/REVIEWER_GUIDE.md`.

## 2. Run it

```bash
.venv/bin/uvicorn app.main:app --port 8000          # production semantics (the default APP_ENV)
.venv/bin/python -m pytest -q                        # 224 tests, ~2.5 min
.venv/bin/python -m evals.run_evals                  # offline gate
.venv/bin/python scripts/live_run.py --profile <form.json> --db data/live.db --groq-key-var GROQ_API_KEY2   # live measurement
```

## 3. Done on this branch (see `git log main..feature/improvements`)

| Area | What changed |
|---|---|
| Speed (P0-A) | Per-model pacing synced to Groq's headers, tier routing with spillover (`ROUTE_*`), oversized-request and daily-quota handling, wait times in the ledger, event-driven DAG, decide-act research loop with one Gemini extraction, smaller `max_tokens`, cheaper Prompt Guard use, concurrent calendar chunks, ETA and progress in the UI, a "where the time went" view for the agency. Live: research 90 s, posts 147 s, leads 18 s (was 25-35 min overall). |
| RBAC (P0-B) | Team page, platform admin data view, account managers created and assigned by the admin, 403 on every viewer write (API and UI) with a page naming the permission, last-owner rule, grant table, CSRF origin check. RBAC eval (315 checks, 14 escalations) and a mutation test. |
| Weekly output (P0-C) | Bounded redraft loop to the posts-per-week target, anchored critic rubric (calibrated), `succeeded_with_shortfall` with reasons. |
| Leads (P1) | Fit threshold 70 with quoted evidence, contacts only for qualified companies within the Hunter quota (shown on Integrations), plain-word search queries, empty results handled, "Add a lead manually" with Hunter verification or explicit founder confirmation. |
| Strategy | Every research claim checked against its source page; unsupported claims flagged, outdated figures labelled, official competitor sites preferred; "Suggest changes" before approving. |
| Deploy (P1) | `render.free.yaml`, ephemeral-storage banner and `/healthz` field, Postgres support (tested locally), `docs/DEPLOY.md`. |
| Polish (P2) | Search and contact credits in a cost ledger, deterministic quality report, ops pages restyled, DECISIONS.md and PLAN.md corrected, reports refreshed, prices checked on provider pages. |
| Bugs found by live runs and Postgres | un-awaited async call; Unicode hyphen failing every calendar slot; strict critic; empty search disabling the provider; lead scoring penalising missing fields; double email send on Postgres; cache unique-key race; re-planning clashing with drafted weeks; invalid `ENCRYPTION_KEY` crashing Gmail connect. |

## 4. Verified, and not verified

Verified: all tests on SQLite and on PostgreSQL 15; offline and live eval gates; stress at 12 and 100 clients; a full live run on Groq, Gemini, Tavily and Hunter on a fresh database; every page rendered for every role with data (demo-mode crawl); a production-mode boot from a clean Python 3.13 environment filtered like the Docker build.

Not verified: **a live Gmail send** (needs the user's own OAuth consent in a browser); **the Docker build itself** (no Docker on this machine; Render builds it on deploy); **a hosted Postgres provider** (Neon/Supabase) as opposed to local Postgres; the new UI in a real browser (pages were fetched and checked as HTML, not looked at).

## 5. Next steps for the user

1. Review and merge: `git push -u origin feature/improvements`, open a PR, merge when happy. Merging redeploys the live site; on the free plan that also wipes its SQLite data.
2. On Render (Environment tab): set `GOOGLE_API_KEY` if not set (Gemini is now the critic and extraction model), keep `TAVILY_API_KEY`, `GROQ_API_KEY`, `HUNTER_API_KEY`. For data that survives sleeping, create a free hosted Postgres and set `DATABASE_URL` to its URL. Optionally set `ADMIN_EMAIL`/`ADMIN_PASSWORD` to get the admin data view.
3. Try a live Gmail send to your own address: Integrations → Connect Gmail, Leads → Add a lead manually (your address, tick "I know this address is correct"), approve the drafted email, send.
4. Local `.env`: `ENCRYPTION_KEY` was not a valid Fernet key; it now works as a passphrase, but keep it stable or connected Gmail accounts must reconnect. `DATABASE_URL` still points at the old `growthos.db` (demo data); use a new file for production-mode runs.
5. Hunter's free plan here is 50 searches and 100 verifications a month (17 and 35 left on 2026-10-27 after testing); 12 clients need the Starter plan.

## 6. Environment variables

Required in production: `JWT_SECRET`, `GROQ_API_KEY`, `TAVILY_API_KEY`, `DATABASE_URL`, `APP_BASE_URL`. Recommended: `GOOGLE_API_KEY`. Optional: `HUNTER_API_KEY`, Google OAuth (`GOOGLE_OAUTH_CLIENT_ID`/`_SECRET`, `GOOGLE_REDIRECT_URI`), `ENCRYPTION_KEY`, `ADMIN_EMAIL`/`ADMIN_PASSWORD`, `SIGNUP_INVITE_CODE`, `ROUTE_*`, `MODEL_LIMITS_JSON`, `SEARCH_PROVIDERS`, `CONTENT_TIME_BUDGET_S`, budgets. `GROQ_API_KEY2..4` are test-only keys from separate accounts; the app never reads them (pass one as `GROQ_API_KEY`, or use `scripts/live_run.py --groq-key-var`). Never print or commit `.env`.
