# Design decisions and lessons learned

The approved plan is in [PLAN.md](PLAN.md). This file records where the build followed it, where it changed, and why. Most of the "why" comes from running the system against real models and from the stress test.

## 1. Workflow first, agents inside nodes

The pipeline is a fixed DAG. Autonomy lives inside nodes: research agents choose their own searches, the prospector chooses its own filters, and the writer revises against a critic. For a long-horizon process (8 weeks of content, a weekly lead quota) this gives predictable cost and ordering, a natural checkpoint per node, and a place to put human gates. A free-roaming planner agent would be harder to budget, test and resume.

One change from the plan: `trends` depends on `market` rather than running fully in parallel, because the trend scout uses the market summary to judge relevance.

## 2. The model proposes, code disposes

Every rule that can be checked in code is checked in code:

| Concern | Enforced by |
|---|---|
| ≥ 5 posts/week on distinct days, pillar mix, unique topics | `validate_calendar`, with violations fed back for repair |
| Who may approve, send, run, see costs | RBAC policy table, deny by default |
| Draft → published only via approval | State machines (`guardrails/state.py`) |
| Lead dedupe, suppression, competitor staff, missing contact | Deterministic filters run *before* any model sees a prospect |
| Statistics must be sourced | `uncited_stat` check against the client's facts and fetched sources |

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
| Gemini's Google Search grounding is over quota on this key (plain Gemini works). | Research tries grounded search first and falls back to the seeded collection for 10 minutes. The judge is Gemini, with Qwen as fallback (still a different family than the `gpt-oss` writer). |

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
| In-process orchestrator and asyncio scheduler | A durable queue (e.g. Postgres-backed or Temporal). The node checkpoint model carries over unchanged. |
| SQLite | Postgres with the RLS migration applied, `SET LOCAL app.tenant_id` per transaction |
| Seeded prospect dataset and research collection | Apollo / Sales Navigator / Clay behind `LeadSource`; live search behind `ResearchProvider` |
| Mock publisher/sender | LinkedIn APIs behind the same interfaces. The approval and send gates don't change. |
| JWT in a cookie, no refresh tokens or SSO | An identity provider with SSO; same `Principal`/policy model |
| Mock writer templates | Only used offline; live mode uses the real prompts |
