# Design decisions and lessons learned

The original plan is in [PLAN.md](PLAN.md); the current design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). This file records where the build followed the plan, where it changed, and why. Most of the "why" comes from running the system against real models, real web search and the stress test.


## 0. Version 2 (2026-10-29): after the review

The reviewer's feedback, and what it changed. Details per decision below; the earlier sections describe v1 and are
kept where they still hold.

| Decision | Why |
|---|---|
| **One operator runs the system.** One person signs in and works for one or more *client profiles* (the founders). No roles, no team page, no admin console, no account managers. | The product is done-for-you: our team operates and approves, not the founder. v1 designed for the founder as the user and grew a four-role permission system around it ("too many moving parts"). |
| **Sign in with Google only** (OIDC, `openid email profile`, PKCE, state and nonce in a signed 10-minute cookie). The ID token is verified with PyJWT against Google's published keys. | No passwords to store, reset or leak; no signup form or invite code. PyJWT and `cryptography` were already dependencies, so no new library (smaller than google-auth or Authlib). Identity is Google's `sub`, never the email. |
| **Isolation is "this client belongs to me".** Every client route checks ownership and answers 404 otherwise; services check again; the ORM tenant interceptor stays. | The one rule is easy to see and to test. `evals/isolation.py` finds every client route in the app automatically, so a new route cannot be forgotten. |
| **The "don't mention other clients" guard lists only the operator's own clients.** | v1 listed every tenant on the platform; a blocked edit would then have named another account's client. |
| **No Gmail sending.** "Open in Gmail" (compose link) and "Copy email" on every approved email, then "I sent it". | Removes OAuth tokens, a send lock, crash recovery and Google's app verification. The operator sends from their own mailbox anyway. |
| **No JSON API.** The server-rendered UI is the product. | Only tests used `/api/*`; it doubled the surface to secure and test. |
| **Removed:** password login, invite code, `ADMIN_*`, the agency `weekly` run, the kill switch, platform-wide budgets, manual lead entry (existed only to demo Gmail sending), the 100-tenant stress test, the RLS SQL file. | The RLS file was never applied and the app never set `app.tenant_id`, so "three walls" was really two; v2 says two. Budgets are now the operator's own money (see BYOK). |
| **v1 accounts are removed by migration 0005.** | They signed in with passwords, which no longer exist, so they could never sign in again. v1 data lived on ephemeral storage. |
| **Tests ignore the developer's `.env`, and nothing but production may open the `.env` database.** | A set `SIGNUP_INVITE_CODE` in `.env` failed 44 tests, and an unscoped `get_db()` could have reached the live database. `Database()` now refuses the production host unless `APP_ENV=production`. |
| **Bring your own keys.** Each account saves one AI key (Groq, OpenAI or Anthropic), a Tavily key and optionally a Hunter key. The server reads no provider key from its environment. | Operators pay their providers directly; the platform cost is hosting only. Platform-paid budgets disappeared with it; an optional per-account monthly cap remains. |
| **Keys: Fernet (MultiFernet for rotation), owner and kind inside the ciphertext, one module reads them.** Checked with a free call before saving (`/models`, Tavily `/usage`, Hunter `/account`); a failing key is never stored. | A copied ciphertext fails to decrypt for another row or account. `app/services/keys.py` is the only reader; a test greps for any other. |
| **Everything per key, nothing shared:** provider clients, pacing buckets, circuit breakers, concurrency caps, daily-quota marks and the Hunter quota cache are keyed by the key's fingerprint; the response cache key includes the client and provider. | v1 had one global set of each; with many accounts one operator's rate limit or quota would have throttled another's. |
| **A refused key stops the run at once** with "replace the key", marks the key "not working", and is never retried. | Retrying a revoked key only multiplies failed calls. Later steps of the same run say the saved key is not working, not that none exists. |
| **Gemini removed.** Extraction and judging run on the operator's own provider. | One fewer provider and key. Measured extraction calls were ~4.7k input tokens, inside Groq's 8k request cap; on Groq's free tier they add some pacing wait. |
| **Model table per provider and tier, in one place** (`PROVIDER_ROUTES`), overridable by `MODEL_ROUTES_JSON` for development. IDs and prices checked 2026-10-29. The critic is always a different model than the writer: Groq qwen3.8 vs gpt-oss-120b (different family), OpenAI gpt-6.1-terra vs gpt-6.1-sol, Claude Haiku 5.5 vs Sonnet 5.5 (same family, different model and a separately framed critic prompt). | |
| **Prompt-injection check:** Groq keys use Prompt Guard; OpenAI and Anthropic have no such classifier, so their small model is asked the same question in words, on top of the deterministic checks. | Honest difference, stated in the evals doc. |
| **Verification limits.** The OpenAI adapter is verified live against Groq's OpenAI-compatible endpoint (`scripts/live_adapters.py`), not against OpenAI. The Anthropic adapter is contract-tested with the official SDK over a mock transport, not run against Anthropic's API. | No paid OpenAI or Anthropic key was available. |
| **Leads without Hunter.** Seven query families on Tavily, companies traced to a result or read from list pages' links, each company's own homepage before scoring, then a contact route: a person named on its own about/team page, an email it publishes, a LinkedIn URL from search results, Hunter only with the operator's key. Email addresses are never guessed. | The reviewer's lead run returned zero; Hunter's data was poor and its free quota tiny. Most companies say who runs them on their own site. |
| **A lead needs a way to reach a person to be kept.** Qualified companies without one are dropped and counted ("no contact route"). | A list of company names is not a lead list; the funnel shows how many were lost this way. |
| **No silent zero.** Every run reports a funnel (searches, companies found, each drop reason, budget used); a short week shows a concrete suggestion and a "Broaden and retry" button that skips the region and size filters and asks for adjacent industries. | |
| **Region and size filters drop only clear mismatches** (a country-code domain or a stated country outside the target; a household-name enterprise for a small-company profile). Unknown stays in. | Most companies found on the web state neither; dropping unknowns would empty thin markets. |
| **Lessons from reading live leads:** list-page link labels are not company names; homepages quote customers in testimonials, so people come only from about/team pages, with a senior role, not "at" another company; one page often answers several queries, so results are de-duplicated before reading. | Each found by opening the output of a live run, not by a test. |
| **Lessons from the final live round (2026-10-29):** review sites put ratings in link labels ("IT Monks Agency 5.0 93 reviews"); `hr@`, `jobs@`, `legal@` and `billing@` are not ways to sell; a model may name a company by its bare domain or misspell it, so a company whose site does not load is dropped ("site does not load"); the funnel counted planned searches, not ones that ran. All fixed, with tests. A number inside a product name ("Baochip-1x") was blocked as an unsourced statistic, which blocked most of one profile's drafts: fixed. The figure-sense check stays strict although it blocks some fair uses (loosening it re-opened the v1 bug). | Each was found by reading the live output, not by a test; each now has one. |
| **Voice: code measures the founder, the model writes.** A style card (length, paragraph shape, opening, ending, lists, emojis, person) is computed from the founder's samples and given to the writer with two of their excerpts. The critic judges opening and ending against the founder (v1's "hook" and "cta_fit" rewarded the template). | The reviewer: "reads as a generic LinkedIn template". The writer prompt literally prescribed the template ("open with a hook line... end with a question"). |
| **Code checks for the template and for borrowed material:** generic patterns the founder never uses (one-line hook, a line per sentence, listicle, engagement question, stock phrases, emojis, hashtag tail), invented first-person stories, names reused from the founder's posts, and any 8-word run copied from a sample. | Showing real excerpts invites copying: the first live runs reused Seth Godin's quarterback analogy and a paragraph of Jason Cohen's. |
| **The critic may never ask for invented stories, customers or numbers.** | The v1 lesson again: a critic that asks for what the writer is forbidden to do makes redrafts unwinnable. |
| **Test material: six real public writers** (Seth Godin, Jason Cohen, April Dunford, Paras Chopra, Arvid Kahl, Bunnie Huang): verbatim excerpts from their public feeds, robots.txt respected, no LinkedIn; two per person held out. Jonathan Stark (feed answers 403), Nithin Kamath (Zerodha rate-limited the requests) and Alan Weiss (mostly audio) were tried and replaced. | The user supplies no samples (D10). |
| **The human bar is the verdict.** Voice distance and template similarity are deterministic and reported; the critic is labelled "Editor's check (automatic)". Human ratings come from the blind review kit and the rating control on every draft. The implementing agent does not rate its own output. | "A model grading itself does not count as evidence." |
| **Redraft budget 7 minutes** (was 4.5). | On Groq's free tier the writer and critic wait on 8,000 tokens a minute; 4.5 minutes cut redrafts short. Quality first; paid tiers never reach it. |
| **Unknown or foreign ids are 404 everywhere, never 500.** | Several UI routes used `.one()` on ids from the URL; a bad id was a server error. One of the first-use 500 paths. |

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

- **Deterministic checks are the backbone**, scored against ground truth: the prospect dataset labels every adversarial row, and calendar and post rules are checkable. LLM-as-judge is used only for subjective quality, and is itself measured against reference scores (written in v1, author not recorded; see the evals doc).
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
