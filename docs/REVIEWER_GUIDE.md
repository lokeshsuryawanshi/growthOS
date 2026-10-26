# Growth OS: reviewer guide

A done-for-you growth portal for busy founders: content marketing, personal branding and lead generation. Agents research the founder's market, plan an 8-week LinkedIn calendar, draft at least 5 posts a week in the founder's voice, find 10 to 20 leads a week and draft outreach emails. **A human approves everything before it is posted or sent.** Built for 12 clients onboarding next month, with a path to 100+.

Deeper documents: [Architecture](ARCHITECTURE.md) · [Evals and guardrails](EVALS_AND_GUARDRAILS.md) · [Cost and run time](ECONOMICS.md) · [Handoff and known issues](../HANDOFF.md)

## 1. What the reviewer asked for, and where it lives

| Requirement | Where |
|---|---|
| Agentic loops | `app/agents/runtime/loop.py` (bounded tool loop), writer and critic in `app/agents/content/`, lead discovery in `app/agents/leads/` |
| Coordination, long-horizon planning | `app/agents/orchestrator/` (DAGs, checkpoints, retries, human gate, fair scheduler); 8-week calendar planner with code validation |
| Founder voice, competitors, trends, strategy | `app/agents/research/`, `app/agents/strategy/` |
| Leads, templated outreach, human edit | `app/agents/leads/`, approvals in `app/services/approvals.py` |
| Content preview and approval | portal UI (`app/web/`) |
| Evals | `evals/` (gate, red-team set, judge calibration) |
| Cost strategy | `app/llm/gateway.py`, [ECONOMICS.md](ECONOMICS.md) |
| Guardrails | `app/guardrails/` |
| RBAC and tenant isolation | `app/core/security.py`, `app/db/session.py` |
| Stress and performance | `loadtest/stress.py`, `loadtest/reports/` |
| Production practice | Alembic migrations, rate limits, budgets, env-based secrets, honest failure handling |

## 2. Architecture in one page

```mermaid
flowchart TB
  U[Founder / agency user] --> W[FastAPI: auth, RBAC, rate limits]
  W --> DB[(SQLite WAL, tenant-isolated)]
  W --> O[Orchestrator: DAG, checkpoints, retries, human gates]
  O --> A[Agents: researchers, planners, writer + critic, lead engine]
  A --> G[LLM gateway: routing, pacing, budgets, cache, cost ledger]
  G --> M[Groq models, Gemini judge]
  A --> T[Tools: web search, safe fetch, Hunter.io]
  A --> R[Guardrails: input, tool, output, state]
  R --> H[Human approval]
  H --> X[Founder posts to LinkedIn / Gmail sends approved email]
```

**Design choice:** a fixed workflow with real agents inside each step. Order, checkpoints, budgets and approvals are deterministic code, so cost is predictable, runs resume after a crash and every gate is testable. Agents decide how to research and write.

- **Two workflows.** Discovery (once per client): ingest, voice, market, competitors, trends, strategy, ICP, calendar, then a human gate. Weekly: learn from edits, refresh trends, draft posts, extend the calendar, find leads, draft outreach.
- **Loops.** Researchers choose their own searches within step caps. Writer and critic revise until a 4.0 score (max 3 rounds). Guard failures trigger rewrites. The calendar planner is repaired against rules. Founder edits become learned voice rules.
- **Real data only.** Web search (Tavily or Google grounding), SSRF-safe page fetch, Hunter.io contacts, Gmail OAuth for sending. If a provider is missing or failing, the step stops with the fix shown. Mock models and demo data exist only for tests.

## 3. Cost and timing

Measured on a live run (real Groq, Gemini, Tavily and Hunter calls): **research and strategy 64k input / 8k output tokens; a weekly cycle about 100k / 10k.** One client per month is one research run plus four weekly cycles: about **464k input and 48k output tokens**. Input dominates because agents read search results.

Prices are list prices per million tokens as I know them. **Verify before quoting.** Cost formula: `(input tokens x price_in + output tokens x price_out) / 1,000,000`.

| Model tier | $ in / out per 1M | Research run | Weekly cycle | Per client / month | 12 clients | 100 clients | Est. time: research / weekly |
|---|---|---|---|---|---|---|---|
| Groq gpt-oss-120b (current writer) | 0.15 / 0.60 | $0.014 | $0.021 | **$0.10** | $1.2 | $9.8 | 2-3 min / 3-4 min |
| Groq gpt-oss-20b (current scorer) | 0.075 / 0.30 | $0.007 | $0.011 | $0.05 | $0.6 | $4.9 | 2-3 min / 3-4 min |
| Gemini 3.5 flash-lite (judge) | 0.10 / 0.40 | $0.010 | $0.014 | $0.07 | $0.8 | $6.6 | 2-3 min / 3-4 min |
| OpenAI mini-class | 0.15 / 0.60 | $0.014 | $0.021 | $0.10 | $1.2 | $9.8 | 3-4 min / 4-5 min |
| OpenAI GPT-4o-class | 2.50 / 10.00 | $0.24 | $0.35 | **$1.64** | $19.7 | $164 | 4-6 min / 5-7 min |
| Claude Haiku-class | 1.00 / 5.00 | $0.10 | $0.15 | $0.70 | $8.5 | $70 | 3-5 min / 4-6 min |
| Claude Sonnet-class | 3.00 / 15.00 | $0.31 | $0.45 | **$2.11** | $25.3 | $211 | 5-8 min / 6-9 min |

**Timing, honestly.** Today, on Groq's free tier, research takes about 14 minutes and a weekly cycle 20 to 35. The cost ledger shows only about 2.5 minutes of real model response time; the rest is our own pacing and 429 waits against Groq's free limits (8,000 tokens per minute and 200,000 per day per model). The "est. time" column is **an estimate** from the number of sequential calls on the critical path (about 20 to 25 for research, 15 to 20 for a weekly cycle) multiplied by typical per-call latency (about 3 s measured on Groq, 5 to 12 s assumed for larger models). It is not measured on those providers.

Other costs: Tavily searches (2 credits each; free tier 1,000 credits a month), Hunter.io (free plan about 25 searches a month, so 10 to 20 leads a week needs a paid plan), hosting (free Render instance, or about $7 a month for a persistent disk). Check each provider's current pricing page.

**Cost controls.** Tiered routing (small model for scoring and extraction, large only for strategy and writing), research done once and trends reused for 6 days, per-run, per-client monthly and service-wide daily budgets, a per-client kill switch, response cache, and a ledger row for every call. The Activity page shows live tokens and cost.

## 4. Evals

Principle: **the model proposes, code disposes.** What code can check is checked in code; the LLM judge covers only voice and hook quality, and is itself measured against hand-scored drafts.

| Eval | What it measures | Result |
|---|---|---|
| Red-team gate (`evals/datasets/redteam.json`) | Does the right guardrail fire on attacks; benign controls measure false positives | block recall 1.0, false-positive rate 0.0 |
| Input-injection recall | Hidden and visible prompt injection in pages and lead data | 0.8 (the miss is an obfuscated case meant for the live classifier) |
| Pipeline eval | 8-week calendar valid, 5+ posts a week, drafts reaching the founder, lead rules (no suppressed or competitor leads), no injected text in any output | all pass |
| Judge calibration (live only) | Critic within one point of a human, Spearman 0.6 or better | gate defined; not re-run since the refactor |
| Online signals | Approval rate, first-pass approval, edit distance between draft and approved text | computed per client |
| Stress test | 100 clients, mock models with injected 429/5xx: 200/200 runs succeeded, 0 failed steps, 0 database lock errors, 0 cross-tenant violations, about 67 s, API p95 under 400 ms | pass (mock models, so it proves orchestration and isolation, not provider speed) |

`python -m evals.run_evals` compares every metric with `evals/baseline.json` and exits non-zero on a breach.

## 5. Guardrails (enforced in code, not prompts)

| Layer | Controls |
|---|---|
| Input | Strip hidden markup and zero-width text; heuristics plus Llama Prompt Guard; visible injection quarantined, hidden injection stripped and logged |
| Untrusted data | Web content wrapped in `<untrusted_data>`; canary string detects prompt leakage |
| Tools | Per-agent allowlist, schema validation, call caps, models can never choose a tenant, URL allowlist, SSRF-safe fetch |
| Output: safety | Cross-tenant leakage, PII, banned topics, competitor disparagement |
| Output: facts | Claim-level grounding: every figure needs a verbatim source quote and must be used for what the source says; cited research URLs must have actually been seen or the claim is dropped |
| Output: originality | Near-duplicates and copying of the founder's own sample posts |
| Process | State machines (nothing is approved without a human; email is approved, sending, sent with a lock so it cannot send twice); send gate checks role, approval, verified recipient, do-not-contact list, daily cap, kill switch |
| Cost and access | Budgets and kill switch; deny-by-default RBAC; tenant interceptor; foreign tenants get 404 |

A real bug found by a live run: a customer count ("1,100 SMB customers") was reused as a market statistic. That is now a claim-level check with a regression test.

## 6. Tests: 150 passing

| Area | Tests | Covers |
|---|---|---|
| Accounts and onboarding | 20 | signup validation, clean empty workspace, profile persistence, isolation, rate limits, UI flow |
| Integrations | 24 | Hunter, search fallback, safe fetch, Gmail OAuth (mock HTTP): quota, bad key, outage, retries |
| Guardrails | 19 | input, output, state machines |
| Grounding | 14 | claim checks, copied hooks, the misattribution regression |
| Gateway and agent runtime | 12 | retries, breaker, budgets, cache, repair loop, tool guard |
| Orchestration | 10 | retry, fail-fast, blocked dependents, checkpoint reuse, crash resume, fairness, budget stop |
| Send gate | 10 | unapproved, unverified, suppressed, wrong role, double click sends once, daily cap |
| Production integrity | 10 | production refuses mock and weak secrets; boots the real app on an empty database with no demo data |
| Pipeline, API security, research integrity, tenancy, evals | 31 | end-to-end cycles, RBAC matrix, cross-tenant probes, fabricated sources dropped, ORM interceptor |

Run: `.venv/bin/python -m pytest -q` (about a minute).

## 7. Security and tenancy

Four roles (admin, account manager, founder, read-only viewer) in one deny-by-default permission table; workspace membership is a second, independent check. A SQLAlchemy interceptor adds the tenant filter to every query and refuses unscoped ones; writes are checked before flush. Passwords use bcrypt, sessions are signed cookies, Gmail tokens are encrypted at rest, and secrets live only in environment variables.

## 8. Known limitations

- Free-tier model limits make runs slow today; a paid tier or routing across several models is the fix.
- Orchestrator and rate limiter run in-process on SQLite (single worker). Path to scale: Postgres (row-level-security migration included), a job queue, Redis rate limits.
- No team-invite or role-management screen yet, no email verification or password reset.
- Gmail sending is code-complete and tested against mock HTTP; Google test mode only allows sending to the owner. LinkedIn publishing is intentionally not integrated: the founder copies an approved post and marks it posted.
- Stress results use mock models; live provider speed is as described in section 3.

## 9. Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in keys; use a NEW database file for DATABASE_URL
.venv/bin/uvicorn app.main:app --port 8000
.venv/bin/python -m pytest -q && .venv/bin/python -m evals.run_evals
```
Deployment notes are in [DEPLOY.md](DEPLOY.md).
