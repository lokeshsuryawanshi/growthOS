# Evals and guardrails

Principle: **the model proposes, code disposes.** Anything that can be checked in code is checked in code, not requested in a prompt. An LLM judges only what code cannot (voice and hook quality), and that judge is itself measured against a human.

## 1. Guardrails: what stops bad output

| Layer | Control | Where |
|---|---|---|
| **Input** | Strip hidden markup, HTML comments and zero-width characters from every fetched page and lead field. Heuristics plus Llama Prompt Guard score injection risk on exactly the part of a page a model can see. Visible injection: the page is **quarantined** (agents never see it). Hidden-only injection: the cleaned text is kept and a high incident is logged. Lead signals are classified in packed batches and bisected to find a flagged one. | `app/guardrails/input_guard.py` |
| **Untrusted data** | Web content is wrapped in `<untrusted_data>` tags and the system prompt says never to follow instructions inside them. A canary string in every system prompt detects prompt leakage. | `app/agents/runtime/loop.py` |
| **Tools and search** | Per-agent allowlists, schema validation, call caps, a model can never pass a tenant id, pages can be read only if a search returned them or the client gave them, SSRF-safe fetcher (no private IPs, redirects re-checked, size and type limits), search queries reduced to plain words. | `loop.py`, `app/integrations/` |
| **Research facts** | Each market fact, competitor and trend is checked against the **text** of the page it cites: its figures must appear there and most of its key words too. Unsupported claims are not shown as facts (they are listed, flagged, under "Not verified"); figures 3+ years old are labelled outdated; competitor claims backed by the competitor's own site are marked official. | `app/agents/research/agents.py` |
| **Output: facts** | **Claim-level grounding** for posts: every figure needs a claim with a verbatim quote from a known source, the quote must contain the figure, and the figure must be used for what the source says (the words around it are compared). | `check_claims` |
| **Output: safety** | Cross-tenant leakage (other clients' names), canary leak, PII (emails, phone numbers): **critical**, never auto-fixed. Banned topics, competitor disparagement, the agency's own name in client copy: high. | `app/guardrails/output_guard.py` |
| **Output: originality and voice** | Near-duplicate of an earlier post, copying a founder sample or example hook, banned phrases (global and the client's own). | `check_post`, `check_copying` |
| **Output: quality** | The editor's score must reach 4.0 on an anchored rubric; a draft that never does after three attempts reaches the founder labelled "below the editor's bar" and does not count toward the weekly target. Length and hashtag limits; outreach: spam signals, false familiarity, personalisation, one clear ask. | `app/agents/content/agents.py`, `check_outreach` |
| **Leads** | Deterministic screening before any model sees a prospect (duplicates, do-not-contact list, competitors, no contact route); a lead is kept only if the scorer's evidence quotes the prospect's own data; fit threshold 70; contacts looked up only for qualified companies, within the Hunter.io quota. | `app/agents/leads/agents.py` |
| **Process** | State machines make illegal moves impossible (nothing reaches "approved" without a human; an email goes `approved -> sending -> sent` under a row lock, so a double click sends once). The send gate checks role, approval, a verified recipient (or one the founder added by hand and confirmed), do-not-contact list, daily cap, kill switch and a connected Gmail. | `app/guardrails/state.py`, `app/services/approvals.py` |
| **Cost** | Per-run, per-client monthly and service-wide daily budgets; per-client kill switch; bounded retries and circuit breakers; per-model pacing; a daily-quota 429 moves to another model instead of retrying for minutes. | `app/llm/gateway.py` |
| **Access** | Deny-by-default RBAC table, a grant table for who may hand out which role, the last-owner rule, membership checks (404 for other workspaces), sessions re-checked against the database on every request, CSRF origin check, rate limits. | `app/core/security.py`, `app/services/team.py`, `app/api/deps.py` |

Severity rule: critical and high violations block the draft; up to two automatic rewrites are tried for high (never for critical); a blocked slot gets fresh redraft attempts told why; medium and low are shown to the reviewer as warnings. Human edits are checked too (no claim bookkeeping is demanded of the founder, but figures must still exist in a source).

## 2. Evals: what proves it works

`python -m evals.run_evals` runs every offline section on mock models and fixtures and compares each metric with `evals/baseline.json`; any breach exits non-zero. It is part of CI (`tests/test_evals.py`).

### 2.1 Guardrail red team (22 cases)

12 post cases, 3 outreach cases and 7 input-injection cases, including **benign controls** (a post that names a competitor neutrally, text containing the word "instructions") so the gate measures false positives as well as recall.

| Metric | Threshold | Result |
|---|---|---|
| Block recall, and the right rule fired | 1.0 | **1.0** |
| Benign false-positive rate | 0.0 | **0.0** |
| Input-injection recall, offline (heuristics only) | 0.8 | 0.8: the miss is a deliberately obfuscated case that only the classifier is meant to catch |
| Input-injection recall, live (with Llama Prompt Guard) | 0.8 | **1.0** |

### 2.2 RBAC and tenant isolation (315 checks + 14 attacks)

`evals/rbac.py`: nine actors (platform admin, account manager assigned to the workspace, account manager with no clients, the founder, a read-only viewer, another workspace's founder, a deactivated user, a removed member, anonymous) against 35 endpoints of the JSON API and the UI, with **hand-written expected outcomes** (allowed, 403, 404, unauthenticated). Plus 14 escalation attempts: a founder creating an admin or account manager, promoting a viewer to an agency role, a viewer promoting themselves, an account manager creating account managers or assigning themselves a client, editing another workspace's team, the last owner demoting themselves, a role requested at signup, a token forged with a guessed secret, a validly signed token for a user who does not exist, and old tokens of deactivated and removed users.

| Metric | Threshold | Result |
|---|---|---|
| Matrix pass rate | 1.0 | **1.0** (315/315) |
| Cross-tenant access violations | 0 | **0** |
| Privilege escalations that succeeded | 0 | **0** of 14 |

**The eval has teeth.** `python -m evals.rbac_mutations` breaks access control on purpose and checks that the eval notices: viewers allowed to approve posts (caught: 2 failures), viewers allowed to edit the profile (caught: 2), workspace membership not checked (caught: 47 failures, 46 cross-tenant violations), founders allowed to grant the account-manager role (caught: 2 successful escalations; this needed two independent checks removed, which is the point of having two).

### 2.3 Deterministic quality report (no LLM)

`evals/quality.py` measures, with the same checks production uses, what a founder would see. It runs in CI over the mock pipeline and, with `--from-db`, over a real run's database.

| Area | Metric | Threshold | Mock pipeline | Live run (real Groq, Gemini, Tavily) |
|---|---|---|---|---|
| Factual accuracy | delivered drafts with a figure not backed by a verbatim source quote | 0 | 0 | 0 |
| Factual accuracy | research claims kept as verified / flagged | | 21 / 1 | 10 / 0 |
| Brand voice | drafts with a banned phrase / an avoided word / copied founder sample | 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| Output quality | near-duplicate pairs among delivered drafts | 0 | 0 | 0 |
| Output quality | share of delivered drafts at the editor's bar | | 1.0 | 1.0 |
| Safety | PII, leaks, disparagement or banned topics in delivered drafts | 0 | 0 | 0 |
| Lead screening | suppressed, competitor and contactless prospects screened out | 1.0 | 1.0, 1.0, 1.0 | n/a (needs labels) |
| Lead screening | good prospects wrongly screened out; people kept twice | 0 | 0; 0 | n/a |

Lead screening runs every labelled prospect of the fixture dataset through the production screening function. (The previous version reported this recall from search results that never surfaced the adversarial prospects, so it was always empty.) The live column is `evals/reports/quality-live.md`: the 5 posts of the measured live run, all at the editor's bar, one using a figure (checked against its quote).

### 2.4 Pipeline eval

A full research run plus weekly cycles for two fictional clients, scored against business rules and the dataset's ground-truth labels: a valid 8-week calendar, at least the weekly post target delivered, no suppressed or competitor leads kept, leads per week within the target range, no injected text in any artifact or post. All pass.

### 2.5 LLM judge calibration (live)

The critic uses a different model family than the writer. `evals/datasets/judge_calibration.json` holds 8 hand-scored drafts (1 = unusable, 5 = publish as is). Gate: the judge is within one point of the human at least 75% of the time and Spearman correlation is at least 0.6.

| Critic prompt | Within one point | Spearman | |
|---|---|---|---|
| Before: "be strict, 5 is rare" | 0.75 | 0.82 | scored live drafts it called "solid" at 3.6, below the 4.0 bar |
| **Now: anchored rubric** (4 = publishable after light edits) | **0.875** in both runs | **0.89** and 0.72 (two runs) | passes the gate; weak drafts still score 1.0-1.8 |

`python -m evals.run_evals --live` (judge on Gemini 3.5 Flash-Lite), report in `evals/reports/eval-live.md`. With 8 items the Spearman figure moves with a single tie, so the gate is set at 0.6.

### 2.6 Online signals

Founder behaviour is the real quality metric: approval rate, first-pass approval rate and the edit distance between draft and approved text, computed per client. Edits and rejection notes feed the learning loop.

### 2.7 Load and stress

`python -m loadtest.stress --tenants 100`: mock models with 100-600 ms latency and injected 429 (5%) and 5xx (2%) errors, through the real orchestrator, scheduler, gateway, guardrails and database.

| Clients | Steps | Failed steps | DB lock errors | Cross-tenant violations | Targets met (posts, calendar, leads) | Wall time |
|---|---|---|---|---|---|---|
| 12 | 192 | 0 | 0 | 0 | 12/12 | 18 s |
| 100 | 1,600 | 0 | 0 | 0 | 100/100 | 77 s |

The 100-client run made 7,622 model calls and absorbed 568 injected failures with retries (0 errors). API under 50 concurrent in-process requests on SQLite: about 116 requests/s, p95 about 615 ms. This proves orchestration, isolation and recovery, not provider speed; live timings are in [ECONOMICS.md](ECONOMICS.md).

## 3. The test suite (223 tests)

`.venv/bin/python -m pytest -q`. Also runs on Postgres: `TEST_DATABASE_URL=postgresql+asyncpg://... pytest`.

| Area | Tests | Covers |
|---|---|---|
| Integrations | 28 | Hunter, search order and fallback, empty results, safe fetch, Gmail OAuth, encryption keys (mock HTTP) |
| RBAC | 20 | team management, escalation, last owner, revocation, cross-tenant, viewer 403 on every API and UI write, admin data view, CSRF |
| Accounts and onboarding | 20 | signup validation, clean empty workspace, profile persistence, rate limits, UI flow |
| Guardrails | 20 | input (incl. batched classifier), output, state machines |
| Grounding | 14 | claim checks, copied hooks, the misattributed-figure regression |
| Production integrity | 13 | production refuses mock data and weak secrets, boots empty, Postgres URLs, ephemeral storage, migrations match the models |
| Gateway, runtime, routing | 21 | retries, breaker, budgets, cache, repair, spillover, oversized requests, daily quota, wait accounting |
| Orchestration | 11 | retry, fail-fast, blocked dependents, checkpoints, crash resume, fairness, event-driven starts |
| Send gate, manual leads | 15 | approval, verification, suppression, cap, double click, founder-confirmed addresses |
| Research integrity, strategy | 15 | fabricated and unsupported sources, outdated figures, calendar rules, suggest changes |
| Weekly output, workflows, pipeline, tenancy, evals, other | 36 | redraft after a block, below-bar label, shortfall status, lead evidence, Hunter quota, separate workflows, ORM interceptor, eval gates |
