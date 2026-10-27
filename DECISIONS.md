# Design decisions and lessons learned

The original plan is in [PLAN.md](PLAN.md); the current design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). This file records where the build followed the plan, where it changed, and why. Most of the "why" comes from running the system against real models, real web search and the stress test.

## 1. Workflow first, agents inside nodes

The pipeline is a fixed DAG. Autonomy lives inside nodes: research agents choose what to search and which pages to read, the lead scout plans its own queries, and the writer revises against a critic. For a long-horizon process (8 weeks of content, a weekly lead quota) this gives predictable cost and ordering, a natural checkpoint per node, and a place to put human gates. A free-roaming planner agent would be harder to budget, test and resume.

One change from the plan: `trends` depends on `market` rather than running fully in parallel, because the trend scout uses the market summary to judge relevance. Nodes start the moment their own dependencies succeed (event-driven, not in waves), so `trends` no longer waits for the slower `competitors` step.

Content and lead generation are two independent workflows with their own buttons (`content`, `leads`); the all-in-one `weekly` run is kept for the agency console.

## 2. The model proposes, code disposes

Every rule that can be checked in code is checked in code:

| Concern | Enforced by |
|---|---|
| ≥ 5 posts/week on distinct days, pillar mix, unique topics | `validate_calendar`, with violations fed back for repair |
| Who may approve, send, run, see costs | RBAC policy table, deny by default |
| Draft → published only via approval | State machines (`guardrails/state.py`) |
| Lead dedupe, suppression, competitor staff, missing contact | Deterministic filters run *before* any model sees a prospect |
| Statistics must be sourced | Claim-level grounding: every figure needs a verbatim quote from a known source, used in the sense the source uses it (`check_claims`) |
| Research facts must be true to their source | Each fact, competitor and trend is checked against the text of the page it cites; unsupported ones are flagged, not shown as facts; 3+ year old figures are labelled outdated |
| The weekly post target | `draft_week` redrafts (bounded) until enough drafts pass guardrails and the editor's bar, or reports exactly why not |
| Leads need evidence | a lead is kept only if the scorer's evidence quotes the prospect's own data |

Prompts ask for the same things, but correctness never depends on the prompt.

## 3. Tenant isolation: three walls, and the gaps found

1. **Service layer:** every query runs inside `tenant_scope(tenant_id)`, which the server sets. Tool arguments may never include a tenant id (the tool guard rejects them).
2. **ORM interceptor** (`db/session.py`): `do_orm_execute` adds `tenant_id = :scope` to every SELECT/UPDATE/DELETE touching a tenant table, and refuses any query with no scope. `before_flush` refuses cross-tenant writes and changes to `tenant_id`.
3. **Postgres RLS** (`migrations/postgres_rls.sql`), generated from the model list, for production.

Testing turned up two gaps, both now covered:
- `select(func.count()).select_from(Lead)` has no ORM entity, so `with_loader_criteria` silently skipped it and returned all tenants' rows. The interceptor now walks the statement's tables and refuses tenant tables it cannot filter. The entity form `select(func.count(Lead.id))` works.
- `do_orm_execute` never sees INSERTs from `session.add()`, which is why `before_flush` exists.

Known bypass, by design: raw `text()` SQL. The app doesn't use it on tenant tables, and RLS covers it in production.

It was also verified (not assumed) that the `contextvars` scope reaches SQLAlchemy's greenlet bridge and survives `asyncio.gather` fan-out (`test_scope_propagates_into_concurrent_tasks`).

## 4. SQLite under concurrency

WAL, `synchronous=NORMAL` and `busy_timeout=30000` on every connection, as suggested. On its own, though, `busy_timeout` does not stop "database is locked": a deferred transaction that reads and then tries to write can fail immediately, without waiting. So:
- the write engine issues `BEGIN IMMEDIATE` (it takes the write lock up front and waits up to `busy_timeout`); reads use a separate deferred engine;
- no transaction is ever held open across an LLM call.

The same property makes check-then-insert safe inside one write transaction. A real race showed up when two research agents fetched the same page at the same moment and stored it twice; it was fixed by re-checking inside the write transaction.

Result: 0 lock errors in the 100-tenant stress test (about 12k model calls, each with its own ledger write).

## 5. What the first live run taught

The first real run against Groq failed in ways the mock never would have:

| Problem | Fix |
|---|---|
| The on-demand tier allows **8,000 tokens/minute per model**, and Groq counts the *`max_tokens` reservation*, not actual output. Parallel research agents hit 429s immediately. | A token bucket per model, sized from the `x-ratelimit-limit-tokens` header, paces requests before they're sent. `max_tokens` is right-sized per agent. |
| Retries gave up before the window reset (backoff of about 7.5 s against a needed 10–17 s). | The wait is parsed from the 429 message ("try again in 10.14s"). 429s get their own, more patient retry budget, separate from real errors. |
| `gpt-oss` reasoning tokens count against the budget. | `reasoning_effort=low` by default, `medium` only for strategy. |
| Groq checks tool-call arguments against the schema on its side and **returns a 400 with no content** when a field is missing, so the repair loop never sees the output. | The provider gets a *loose* schema (no `required`/length constraints). Pydantic does the real check and sends precise errors back in a repair turn. Research schemas also got sensible defaults. |
| Gemini's Google Search grounding is over quota on this key (plain Gemini works). | Search uses Tavily first and Gemini grounding as a fallback. There is no canned fallback anywhere: if no provider answers, the step stops with the fix. |

## 5b. Making it fast on free tiers (2026-10-27)

Research and strategy took about 20 minutes and a weekly cycle 20 to 35, with only about 2.5 minutes of actual model time. The ledger now records, per call, provider time, time waiting in our own pacing and time backing off after 429s, so this is measured rather than guessed:

| Finding | Change | Effect |
|---|---|---|
| Every step ran on one model with 8k tokens/minute, and each call reserved `max_tokens` of 2,500-4,000 against ~200 used | Each tier leads with a different model (limits are per model) and spills to the next model in its chain when the primary would make a call wait; `max_tokens` sized from measured use; Groq's `x-ratelimit-remaining-tokens` header keeps our pacing honest | most waits gone |
| The research tool loop re-sent every page it had read on every step (26k input tokens for competitors) | A decide-act loop: the model chooses searches and pages from compact observations, code runs them in parallel, one structured extraction reads the evidence (on Gemini, whose limits are far higher) | research steps 4-10 min → 10-30 s |
| Prompt Guard scored up to 8,000 characters per page in 1,800-character chunks, and hit its requests-per-minute limit | It scores only the 1,800 characters a model can see; client-typed text gets heuristics only; lead signals are packed and bisected | 43 → ~12 classifier calls per research run, no 429s |
| A request bigger than a model's per-minute limit is rejected by Groq; a per-day quota 429 was retried for minutes | Oversized requests skip that model; a daily-quota 429 moves to the next model and is remembered | no hopeless retries |

Result on real providers: research and strategy in 65-80 s, a content run in about 3 minutes, a lead run in under a minute.

## 5c. The critic and the weekly target

A strict critic prompt ("be strict, 5 is rare") scored drafts it itself called "solid" at 3.6, below the 4.0 bar, and redrafting could not move them: a live content run took 11 minutes and delivered 1 of 5 posts. The writer is forbidden to invent specifics, so "add more concrete detail" cannot be fixed by trying again. The rubric is now anchored to the same scale as the hand-scored calibration set (4 = publishable after light edits). Measured on that set, agreement within one point rose from 75% to 87.5% (Spearman 0.82 → 0.72, still above the 0.6 gate). Redrafts are bounded three ways (attempts, no improvement, a time budget), and a run that misses a KPI ends as `succeeded_with_shortfall` with the reasons instead of `succeeded`.

## 5d. What Postgres found that SQLite hid

The test suite now also runs on PostgreSQL (`TEST_DATABASE_URL`). Two real bugs appeared that SQLite's serialised writers had hidden: a double click sent the same email four times (the send claim now takes a row lock), and two identical model calls caching at once failed a step on a unique key (now treated as success).

## 5e. RBAC has to be provable

Roles were correct in the policy table but hard to see: there was no way to add a teammate, and viewers got a 200 with an error message instead of a 403 from UI routes. Now there is a team page (founder adds viewers and co-owners; no invites), a platform-admin data view, account managers created and assigned by the admin, a 403 on every write a viewer tries, and revocation on the next request. The proof is an eval, not a claim: a 315-check matrix of actors x endpoints with hand-written expectations, 14 escalation attempts, and a mutation test that breaks access control four ways and confirms the eval notices each one.

## 6. Evals

- **Deterministic checks are the backbone**, scored against ground truth: the prospect dataset labels every adversarial row, and calendar and post rules are checkable. LLM-as-judge is used only for subjective quality, and is itself measured against hand-scored drafts.
- **The red-team suite includes benign controls** (for example a post that names a competitor neutrally, or text containing the word "instructions") so the gate measures false positives as well as recall.
- **One case is planted to fail offline**: an injection with letters spaced out. Heuristics miss it and the Prompt Guard classifier is the layer meant to catch it. The mock report shows the miss on purpose.
- **Online signals** come from founder behaviour: approval rate, *edit distance* between draft and approved text (the main quality signal), and rejection notes. Edits feed a reflection step that writes durable rules into the voice profile, and the next weekly cycle uses them.

## 7. Cost

Levers in order of impact: tiered routing (small model for prospecting and scoring, big model only for writing and strategy), doing expensive research rarely (`trends_refresh` skips when the report is under 6 days old), the critic loop's early exit, response caching for deterministic work, and compact artifact summaries instead of raw documents in prompts. Budgets apply per run and per tenant per month, with an incident at 80% and a hard stop at 100%. A per-tenant kill switch is checked by the gateway, the orchestrator and the send gate.

## 8. Things deliberately simplified

| Simplified | Production version |
|---|---|
| In-process orchestrator and asyncio scheduler | A durable queue (Postgres-backed or Temporal). The node checkpoint model carries over unchanged. |
| SQLite by default (Postgres supported and tested) | Postgres with the RLS migration applied, `SET LOCAL app.tenant_id` per transaction |
| Web search (Tavily) + Hunter.io for leads | Apollo / Sales Navigator / Clay behind the same lead pipeline |
| No LinkedIn publishing: the founder copies an approved post and marks it posted | LinkedIn APIs behind the same approval gate |
| Email outreach sent from the founder's own Gmail after approval (OAuth, `gmail.send` only) | The same, plus a verified Google app so any recipient works |
| JWT in a cookie, sessions re-checked against the database per request, no SSO | An identity provider with SSO; same `Principal`/policy model |
| Mock models and fictional clients (`app/fixtures/`) | Used only by tests, the stress test and a labelled local demo; production refuses to load them |
