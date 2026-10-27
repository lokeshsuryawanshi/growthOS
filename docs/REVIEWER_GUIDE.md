# Growth OS: reviewer guide

A done-for-you growth portal for busy founders: content marketing, personal branding and lead generation. Agents research the founder's market and competitors, plan an 8-week LinkedIn calendar, write at least 5 posts a week in the founder's voice, find 10 to 20 leads a week and draft outreach emails. **A human approves everything before it is posted or sent.** Designed for 12 clients onboarding next month, with a path to 100+.

Deeper documents: [Architecture](ARCHITECTURE.md) · [Evals and guardrails](EVALS_AND_GUARDRAILS.md) · [Cost and run time](ECONOMICS.md) · [Known issues and handoff](../HANDOFF.md)

**Status in one line.** Built and tested (161 automated tests passing); research, strategy, drafting and lead discovery were run end to end on real providers (Groq, Gemini, Tavily, Hunter.io). Not yet exercised live: Gmail sending. Run time is slow today because of free-tier model limits (section 4).

## 1. Requirements and where they live

| Requirement | Where |
|---|---|
| Agentic loops | `app/agents/runtime/loop.py` (bounded tool loop); writer and critic in `app/agents/content/`; lead engine in `app/agents/leads/` |
| Coordination and long-horizon planning | `app/agents/orchestrator/` (DAGs, checkpoints, retries, human gate, fair scheduler); 8-week calendar planner validated by code |
| Founder voice, competitors, trends, strategy | `app/agents/research/`, `app/agents/strategy/` |
| Leads and templated outreach with human edit | `app/agents/leads/`, `app/services/approvals.py` |
| Content preview and final approval | portal UI, `app/web/` |
| Evals, guardrails | `evals/`, `app/guardrails/` |
| Cost strategy | `app/llm/gateway.py`, section 4 |
| RBAC and tenant isolation | `app/core/security.py`, `app/db/session.py` |
| Stress and performance testing | `loadtest/` |
| Production practice | Alembic migrations, budgets, rate limits, secrets in environment variables, honest failure handling, no demo data in production |

## 2. Architecture

```mermaid
flowchart TB
  U[Founder / agency user] --> W[FastAPI: auth, RBAC, rate limits]
  W --> DB[(SQLite WAL, tenant-isolated)]
  W --> O[Orchestrator: DAGs, checkpoints, retries, human gates]
  O --> A[Agents: researchers, planners, writer + critic, lead engine]
  A --> G[LLM gateway: routing, pacing, budgets, cache, cost ledger]
  G --> M[Groq models, Gemini judge]
  A --> T[Tools: web search, safe fetch, Hunter.io]
  A --> R[Guardrails: input, tool, output, state]
  R --> H[Human approval]
  H --> X[Founder posts to LinkedIn / Gmail sends approved email]
```

**Design choice: a fixed workflow with real agents inside each step.** Order, checkpoints, budgets and approvals are deterministic code, so cost is predictable, runs resume after a crash, and every gate is testable. Agents decide how to research and write.

| Workflow | Steps | Runs |
|---|---|---|
| Discovery | ingest, voice, market, competitors, trends, strategy, ICP, 8-week calendar, then **human approval gate** | once per client |
| Content and calendar | learn from founder edits, refresh trends, draft the week's posts, extend the calendar | weekly, own button |
| Leads | search for companies, verify, attach Hunter.io contacts, score, draft outreach emails | weekly, own button |

Content and lead runs are independent: separate buttons, no shared steps, and they may run at the same time. Each step is a database checkpoint; a succeeded step never re-runs, and a failed step blocks only its dependents.

| Agent loop | What it does | Bounds |
|---|---|---|
| Research agents | choose their own searches, open pages, return a typed result | step and tool-call caps, URL allowlist, budget check per call |
| Writer and critic | draft, score with a different model family, revise | pass score 4.0, 3 rounds, early exit |
| Guard auto-fix | rewrite a draft that broke a rule | two attempts, never for critical violations |
| Calendar planner | propose weeks, code validates, violations fed back | 3 repairs |
| Lead engine | plan queries, search (code), extract, verify against results, attach contacts, score | 3 rounds |
| Learning loop | founder edits become durable voice rules | 15 rules |

## 3. Real data only

Web search (Tavily or Google grounding), an SSRF-safe page fetcher, Hunter.io contacts and Gmail OAuth are real integrations. If a key is missing or a provider fails, the step stops and shows the fix; nothing is replaced by canned output. Mock models and fictional data exist only in tests and a labelled local demo, and production refuses to start with them.

## 4. Cost, latency and unit economics

### 4.0 Models in use

| Model | Role | Provider |
|---|---|---|
| gpt-oss-120b | strategy, post writing, company extraction (the "big" tier) | Groq |
| gpt-oss-20b | scoring, planning, extraction (the "small" tier) | Groq |
| Llama Prompt Guard 2 (86M) | prompt-injection classifier on every fetched page | Groq |
| Gemini 3.5 flash-lite | critic and judge (a different model family than the writer); Google Search grounding | Google |
| Qwen3.8-27B | judge fallback if Gemini fails | Groq |

Routing is by tier with a fallback chain, a circuit breaker and a per-model pacing bucket. The comparison with other paid models is in section 4.3.

### 4.1 Tokens per run (measured on a live run)

| Step | Model calls | Input tokens | Output tokens |
|---|---|---|---|
| Research and strategy (once per client) | 54 | 64,140 | 7,953 |
| Weekly work (content plus leads), planning figure | about 40 | about 100,000 | about 10,000 |
| Whole test client, everything including retries | 181 | 242,050 | 25,798 |

One client per month is one research run plus four weekly cycles: **about 464,000 input and 48,000 output tokens.** Input dominates (about 10 to 1) because agents read search results. About 40% of calls are tiny safety-scanner calls on fetched pages.

### 4.2 Where the time goes today (measured, Groq free tier)

| Research and strategy step | Wall time |
|---|---|
| ingest, voice | 10 s, 2 s |
| competitors (runs in parallel) | 516 s |
| market (runs in parallel) | 228 s |
| trends (after market) | 172 s |
| ICP, strategy, calendar | 27 s, 37 s, 86 s |
| **Whole run** | **about 14 min** |
| Weekly cycle (content plus leads) | 20 to 35 min |

| Measure | Value |
|---|---|
| Average model response time | gpt-oss-120b 3.1 s, gpt-oss-20b 2.8 s, Gemini flash-lite 1.5 s, Prompt Guard 0.3 s |
| Total model response time in a whole run | about 2.5 min |
| Retries that failed outright | 0 |

**Why it is slow:** the model is not. Groq's free tier allows 8,000 tokens per minute per model and 200,000 per day per model. The big writer model used about 73,000 tokens in one research run, which needs more than 9 minutes at 8,000 per minute, so our pacing code (`TokenBucket`) makes calls wait. Groq also counts the reserved output size, not the actual output. In one test the daily cap was reached and Groq answered "429 too many requests" until the window freed. This is a provider limit, not agent logic.

### 4.3 Projection on paid APIs

Prices are list prices per million tokens as I know them; **verify on each provider's pricing page before quoting.** Cost formula: `(input tokens x price_in + output tokens x price_out) / 1,000,000`.

| Model tier | $ in / out per 1M | Research run | Weekly cycle | Per client per month | 12 clients | 100 clients | Est. time: research / weekly |
|---|---|---|---|---|---|---|---|
| Groq gpt-oss-120b (current writer) | 0.15 / 0.60 | $0.014 | $0.021 | **$0.10** | $1.2 | $9.8 | 2-3 min / 3-4 min |
| Groq gpt-oss-20b (current scorer) | 0.075 / 0.30 | $0.007 | $0.011 | $0.05 | $0.6 | $4.9 | 2-3 min / 3-4 min |
| Gemini 3.5 flash-lite | 0.10 / 0.40 | $0.010 | $0.014 | $0.07 | $0.8 | $6.6 | 2-3 min / 3-4 min |
| OpenAI mini-class | 0.15 / 0.60 | $0.014 | $0.021 | $0.10 | $1.2 | $9.8 | 3-4 min / 4-5 min |
| OpenAI GPT-4o-class | 2.50 / 10.00 | $0.24 | $0.35 | **$1.64** | $19.7 | $164 | 4-6 min / 5-7 min |
| Claude Haiku-class | 1.00 / 5.00 | $0.10 | $0.15 | $0.70 | $8.5 | $70 | 3-5 min / 4-6 min |
| Claude Sonnet-class | 3.00 / 15.00 | $0.31 | $0.45 | **$2.11** | $25.3 | $211 | 5-8 min / 6-9 min |

**The time column is an estimate, not a measurement.** It is the number of sequential calls on the critical path (about 20 to 25 for research, 15 to 20 for a weekly cycle) times typical per-call latency: about 3 s measured on Groq, 5 to 12 s assumed for larger models. With no rate limits, expect roughly 4 to 5 times faster than today.

### 4.4 Unit economics (planning figures)

| Unit | Groq 120b | GPT-4o-class | Claude Sonnet-class |
|---|---|---|---|
| AI cost per ready post (about 40k in / 6k out per 5 posts) | $0.002 | $0.03 | $0.04 |
| AI cost per lead drafted (about 60k in / 4k out per 15 leads) | $0.001 | $0.012 | $0.016 |
| AI cost per client per month | $0.10 | $1.64 | $2.11 |
| Margin if a client pays an illustrative $500 a month (AI only) | 99.98% | 99.7% | 99.6% |

Splitting a weekly cycle into content and leads is an estimate from the ledger, not a separate measurement. **The AI is not the cost driver.** What costs money is Hunter.io (the free plan allows about 25 searches a month, so 10 to 20 leads a week per client needs a paid plan), Tavily searches (2 credits each, free tier 1,000 credits a month), hosting (free Render instance, about $7 a month for a persistent disk), and human support time. Check each provider's current pricing.

### 4.5 How cost is controlled

| Lever | How |
|---|---|
| Tiered routing | small model for scoring and extraction, large model only for strategy and writing |
| Do expensive work once | research once per client; trends reused for 6 days |
| Budgets | per run, per client per month (80% alert, 100% stop), service-wide per day |
| Kill switch | per client, checked by the gateway, orchestrator and send gate |
| Cache and compaction | response cache; compact summaries instead of raw documents in prompts |
| Visibility | every call in the ledger; live tokens and cost on the Activity page |

## 5. Evals

**30 hand-written cases plus a 14-metric release gate.** 22 red-team cases (12 post, 3 outreach, 7 input-injection, including benign controls), 8 hand-scored drafts for judge calibration, and a pipeline eval that scores a full discovery plus weekly run for two clients against business rules and ground-truth labels. The 161 automated tests are separate (section 7).

Principle: **the model proposes, code disposes.** What code can check is checked in code. The LLM judge covers only voice and hook quality, and is itself measured.

| Eval | What it measures | Result |
|---|---|---|
| Red-team gate | the right guardrail fires on attacks; benign controls measure false positives | block recall 1.0, false positives 0.0 |
| Input-injection recall | hidden and visible prompt injection in pages and lead data | 0.8 (the miss is an obfuscated case meant for the live classifier) |
| Pipeline eval | valid 8-week calendar, 5+ posts a week, drafts reach the founder, no suppressed or competitor leads, no injected text in outputs | all pass |
| Judge calibration (live only) | critic within one point of a human, Spearman 0.6 or better | gate defined; not re-run since the refactor |
| Online signals | approval rate, first-pass approval, edit distance between draft and approved text | computed per client |
| Stress test (mock models, injected 429/5xx) | 100 clients: 200/200 runs, 0 failed steps, 0 database lock errors, 0 cross-tenant violations, about 67 s, API p95 under 400 ms | pass; proves orchestration and isolation, not provider speed |

`python -m evals.run_evals` compares every metric with `evals/baseline.json` and exits non-zero on a breach.

## 6. Guardrails (enforced in code, not prompts)

**49 individual controls: 47 deterministic code, 2 model-based** (counted from the code; the grouping is a judgment).

| Group | Count | Deterministic or model-based |
|---|---|---|
| Input (strip hidden text, regex checks, quarantine rule) | 3 | deterministic |
| Input (Llama Prompt Guard classifier) | 1 | model-based |
| Tool guards (allowlist, schema, call cap, no tenant choice, URL/SSRF) | 5 | deterministic |
| Output rules (safety 7, facts 6, originality 3, quality 3, outreach 4) | 23 | deterministic |
| Process (2 approval state machines, 7 send-gate checks) | 9 | deterministic |
| Cost and access (3 budgets, kill switch, rate limits, breaker, RBAC, tenant isolation) | 8 | deterministic |

The second model-based control is the critic, which scores voice and hook quality; it is a quality check, not a safety rule. What each layer does:

| Layer | Controls |
|---|---|
| Input | strip hidden markup and zero-width text; heuristics plus Llama Prompt Guard; visible injection quarantined, hidden injection stripped and logged |
| Untrusted data | web content wrapped in `<untrusted_data>`; canary string detects prompt leakage |
| Tools | per-agent allowlist, schema validation, call caps, models can never choose a tenant, URL allowlist, SSRF-safe fetch |
| Output: safety | cross-tenant leakage, PII, banned topics, competitor disparagement, agency name leaking into client voice |
| Output: facts | **claim-level grounding:** every figure needs a verbatim source quote and must be used for what the source says; cited research URLs must have actually been seen or the claim is dropped |
| Output: originality | near-duplicates and copying of the founder's sample posts |
| Process | state machines (nothing is approved without a human; email goes approved, sending, sent with a lock so it cannot send twice); send gate checks role, approval, verified recipient, do-not-contact list, daily cap, kill switch |
| Cost and access | budgets and kill switch; deny-by-default RBAC; tenant interceptor; foreign tenants get 404 |

**A real bug found by a live run:** a customer count ("1,100 SMB customers") was reused as a market statistic. It is now a claim-level check with a regression test. The same runs also exposed an invented "$2,000" figure and invented anecdote, which the guardrails blocked, and the agency name leaking into an email, now a high-severity rule.

## 7. Tests: 161 passing

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
| Session validity (token vs database) | 5 | wiped database, deactivation, role change and membership removal take effect immediately |
| Separate content and lead workflows | 6 | independent runs, overlap rules, refusal before research, agency name never in client voice |
| Pipeline, API security, research integrity, tenancy, evals | 31 | end-to-end cycles, RBAC matrix, cross-tenant probes, fabricated sources dropped, ORM interceptor |

## 8. Security and tenancy

Four roles (admin, account manager, founder, read-only viewer) in one deny-by-default permission table; workspace membership is a second, independent check. A SQLAlchemy interceptor adds the tenant filter to every query and refuses unscoped ones; writes are checked before flush. Passwords use bcrypt, sessions are signed cookies, Gmail tokens are encrypted at rest, secrets live only in environment variables, and foreign workspaces return 404.

## 9. Known limitations and next steps

| Limitation | Plan |
|---|---|
| Free-tier model limits make runs slow | paid tier, or route different steps to different models (limits are per model) |
| No screen for inviting teammates or managing roles; no email verification or password reset | team page, invites, admin UI |
| Orchestrator and rate limiter are in-process on SQLite (single worker) | Postgres (row-level-security migration included), job queue, Redis |
| Gmail sending is code-complete but tested only against mock HTTP; Google test mode sends only to the owner | live test, then Google verification |
| LinkedIn publishing not integrated by design | founder copies an approved post and marks it posted |
| Stress results use mock models | re-measure on a paid tier |
| Search credits not in the cost ledger | add Tavily and Hunter usage to the ledger |

## 10. Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # fill in keys; use a NEW database file for DATABASE_URL
.venv/bin/uvicorn app.main:app --port 8000
.venv/bin/python -m pytest -q && .venv/bin/python -m evals.run_evals
```
Deployment notes: [DEPLOY.md](DEPLOY.md).
