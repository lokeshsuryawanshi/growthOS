# Growth OS

A done-for-you growth portal, run by an **operator** for the founders they work for. For each founder (a *client*) it
researches the market, competitors and the founder's own voice from real sources, plans an 8-week LinkedIn calendar,
drafts at least five posts a week in that founder's voice, finds companies that fit and a real way to reach someone
there, and drafts short outreach emails. **The operator approves, edits or rejects everything.** Nothing is posted or
sent by the system: approved posts are copied to LinkedIn, approved emails are opened in Gmail or copied.

**Start here: [docs/REVIEWER_GUIDE.md](docs/REVIEWER_GUIDE.md)** (what changed after the review, measured results,
limitations).

Docs: [Architecture](docs/ARCHITECTURE.md) · [Evals and guardrails](docs/EVALS_AND_GUARDRAILS.md) ·
[Cost and run time](docs/ECONOMICS.md) · [Deploying](docs/DEPLOY.md) · [Decisions and lessons](DECISIONS.md) ·
[Handoff](HANDOFF.md)

Live: https://growthos-b0t9.onrender.com

## How it works

1. **Sign in with Google.** One account per person; no passwords, no roles.
2. **Save your own keys** on *API keys & spend*: an AI key (Groq, OpenAI or Anthropic), a Tavily search key, and
   optionally Hunter.io. Each is checked with one free call, stored encrypted and never shown again. You pay your
   providers directly; an optional monthly cap stops runs when reached.
3. **Add a client:** company, audience, competitors, a few of the founder's own posts, goals.
4. **Research and strategy** (a few minutes), then you approve it or suggest changes.
5. **Posts:** each week's posts, written from a style card measured from the founder's own writing and checked by a
   second model and by code (sources for every figure, no invented stories, no copied sentences, no generic template).
   Click any calendar card to open its post.
6. **Leads:** companies found on the open web, each with the page it came from and a way to reach a person (named on
   their own site, an email they publish, a LinkedIn profile from search, or Hunter). Every run shows where candidates
   went; a short week offers *Broaden and retry*.
7. **Outreach:** approve or edit, then *Open in Gmail* or *Copy email*, send it yourself, and mark it sent.

## Honesty rules (enforced in code and tests)

- Production never shows or produces synthetic data: fixtures and mock models load only when `APP_ENV` is `demo` or
  `test`, and a test boots the real app in production mode to check.
- No silent fallbacks: a missing or refused key stops the step with the fix (HTTP 424 style); nothing is replaced by
  canned output.
- Sources are verified: a cited URL must have come back from a search or a page read, and a figure needs a verbatim
  quote used in the source's sense.
- Targets are reported honestly: a run that misses them ends "succeeded with shortfall" and says why.
- Accounts never see each other's data or keys: `evals/isolation.py` checks every client route automatically.

## Run it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                                   # JWT_SECRET, ENCRYPTION_KEY, DATABASE_URL, Google sign-in
.venv/bin/uvicorn app.main:app --port 8000
```

Labelled synthetic demo (mock models, fictional clients, sign-in without Google):

```bash
APP_ENV=demo DATABASE_URL=sqlite+aiosqlite:///./demo.db .venv/bin/uvicorn app.main:app --port 8010
```

## Tests and evals

```bash
.venv/bin/python -m pytest -q                         # SQLite; TEST_DATABASE_URL=postgres://... runs them on Postgres
.venv/bin/python -m pytest -m e2e tests/e2e           # a real browser (needs: pip install playwright; playwright install chromium)
.venv/bin/python -m evals.run_evals                   # offline gate: guardrails, pipeline, quality, account isolation
.venv/bin/python -m evals.isolation --mutations       # breaks isolation on purpose; the eval must notice every break
.venv/bin/python -m evals.voice --db <live run db>    # voice distance and template similarity on real output
.venv/bin/python scripts/check_deploy.py <url>        # after every deploy
```

## Code map

| Topic | Where |
|---|---|
| Accounts, clients, sign-in | `app/core/google_oidc.py`, `app/api/deps.py`, `app/services/accounts.py` |
| Your own keys | `app/services/keys.py` (the only reader of stored keys), `app/integrations/crypto.py`, `app/core/scrub.py` |
| Model calls | `app/llm/gateway.py`, `app/llm/providers.py` (Groq, OpenAI, Anthropic), model table in `app/core/config.py` |
| Workflows | `app/agents/orchestrator/` (event-driven DAGs, checkpoints, retries, human gate, fair scheduler) |
| Research, strategy, posts | `app/agents/research/`, `app/agents/strategy/`, `app/agents/content/` (`style.py`: style card, template detector) |
| Leads | `app/agents/leads/live.py` (discovery, contact routes, funnel), `app/agents/leads/agents.py` |
| Guardrails | `app/guardrails/` (input, claims and output, state machines) |
| Evals | `evals/` (`isolation.py`, `voice.py`, `run_evals.py`, `quality.py`), `evals/datasets/voice_samples/` |
| UI | `app/web/routes.py`, `app/web/templates/` |
