# Architecture

Growth OS is a **fixed workflow with agents inside each step**, run by one operator for the founders they work for.
The order of work, the checkpoints, the budgets and the human approvals are deterministic code. The agents decide what
to search, what to read and how to write. Everything a model produces is checked by code before the operator sees it.

## 1. System overview

```mermaid
flowchart TB
  OP[Operator<br/>browser, HTMX] -->|Sign in with Google| W[FastAPI app<br/>session check, ownership check, CSRF origin check, rate limits]
  W --> SVC[Services<br/>accounts and clients, keys, runs, approvals, strategy, compose]
  SVC --> DB[(Postgres on Neon, or SQLite<br/>tables per client with tenant_id)]
  SVC --> ORCH[Orchestrator<br/>event-driven DAG, checkpoints, retries, human gate]
  ORCH --> SCH[Fair scheduler<br/>global and per-client caps]
  SCH --> AG[Agents]
  AG --> GW[LLM gateway<br/>the client's owner's key, tier routing, per-key pacing,<br/>run budget and monthly cap, cache, cost ledger]
  GW --> M[Groq, OpenAI or Anthropic<br/>with the operator's own key]
  AG --> TOOLS[Tools<br/>Tavily search with the operator's key, safe page fetch, optional Hunter]
  AG --> GR[Guardrails<br/>input, claims, output, template, state machines]
  GR --> HUM[Operator approves, edits, rates or asks for changes]
  HUM --> OUT[Operator copies the post to LinkedIn,<br/>opens the email in Gmail or copies it, then marks it done]
```

## 2. Accounts, clients and isolation

- **One account per person**, created on first Google sign-in and identified by Google's `sub`. An account owns any
  number of **clients** (table `tenants`, column `owner_user_id`). There are no roles.
- **The one access rule:** a client route answers only if the client belongs to the signed-in account; otherwise 404,
  so ids cannot be probed (`app/api/deps.py:tenant_or_404`). Services check again (`app/core/security.py:authorize`).
- **Sessions:** a signed JWT in an HttpOnly, SameSite=Lax cookie that carries only the user id. Every request re-reads
  the account and the clients it owns, so a deleted account or client is gone on the next click.
- **Inside a client:** every client-owned table carries `tenant_id`. A SQLAlchemy `do_orm_execute` interceptor adds the
  filter to every query and refuses queries with no scope or a shape it cannot filter; `before_flush` blocks writes to
  another client. The scope is a `contextvar` set by server code, never by a model.
- **Proof:** `evals/isolation.py` finds every client route in the app automatically and calls each one as another
  account and signed out, tries forged, unsigned, tampered, expired and stale sessions, scans every page the other
  account can open, and checks API keys never appear anywhere. A mutation test breaks the rules on purpose five ways
  and must notice each one.

```mermaid
flowchart LR
  R[Request with cookie] --> S{Signed, unexpired?}
  S -- no --> L[/login/]
  S -- yes --> U{Account still exists?<br/>read from the database}
  U -- no --> L
  U -- yes --> O{Client owned by this account?}
  O -- no --> N[404]
  O -- yes --> T[tenant_scope client id] --> Q[ORM interceptor adds tenant_id to every query]
```

## 3. Bring your own keys

```mermaid
flowchart LR
  F[API keys page<br/>write-only field] --> C[One free check call<br/>models list, Tavily usage, Hunter account]
  C -- refused --> X[Not stored; the reason is shown]
  C -- accepted --> E[Fernet encrypt of owner + kind + provider + key<br/>account_secrets row: provider, last 4, status]
  RUN[A run for client X] --> K[keys.for_client X<br/>owner of X, that owner's key]
  K --> D[Decrypt in memory; owner and kind must match]
  D --> G[Gateway lane for that key<br/>client, pacing, breaker, quota marks keyed by key fingerprint]
```

- `app/services/keys.py` is the only module that reads or writes the ciphertext (a test greps for any other).
- Production refuses to start without a valid `ENCRYPTION_KEY`; `ENCRYPTION_KEYS` (newest first) rotates keys.
- Keys are never sent to the browser, logged, or stored in error text: provider errors are scrubbed of the key and of
  anything key-shaped, and a log filter does the same for every log line.
- A key the provider refuses during a run stops the run at once with "replace the key" and marks the key "not
  working"; it is never retried.

## 4. The workflows (DAGs)

The operator starts three workflows per client from separate buttons. **Research and strategy** runs once (and again
on request). **Posts and calendar** and **lead search and outreach** are independent weekly runs that may overlap.

```mermaid
flowchart LR
  subgraph Discovery[Research and strategy]
    I[ingest<br/>read site, scan inputs] --> V[voice]
    I --> M[market] --> T[trends]
    I --> C[competitors]
    V & M & C & T --> S[strategy]
    M & C --> P[ICP]
    S --> CAL[8-week calendar]
    CAL & P --> G{{strategy approval<br/>human gate}}
  end
  subgraph Content[Posts and calendar]
    R[reflect on edits] --> D[draft week]
    TR[refresh trends] --> D
    D --> CE[extend calendar] --> K[KPI snapshot]
  end
  subgraph Leads[Lead search and outreach]
    L[find leads] --> O[draft outreach emails]
  end
```

- **Checkpoints.** Each node is a row. A succeeded node never re-runs; after a crash or a retry the run resumes from
  the first unfinished node. A failed node blocks only its dependents.
- **Honest outcomes.** A run ends `succeeded` only if every target was met; otherwise `succeeded_with_shortfall` with
  the reasons, or `failed` with the fix. Missing or refused keys, and exhausted quotas, fail fast.

## 5. Posts: the founder's voice, held by code

```mermaid
flowchart LR
  SMP[Founder's own samples] --> SC[Style card in code<br/>length, paragraph shape, opening, ending,<br/>lists, emojis, hashtags, person]
  SC & EX[Two of the founder's excerpts] & SRC[Checked sources] --> WR[Writer]
  WR --> CR[Critic: a different model<br/>voice, specificity, opening, insight, ending,<br/>judged against the excerpts]
  CR --> TP{Template patterns the<br/>founder never uses?}
  TP -- yes --> WR
  TP -- no --> GU[Output guard<br/>claims, invented anecdotes, reused examples,<br/>copying, PII, other clients, banned phrases]
  GU --> OP[Operator: approve, edit, rate, reject]
```

| Loop | Bounds |
|---|---|
| Research: the model picks searches and pages from compact observations; code runs them; one extraction reads the evidence; every claim is checked against its page | 3 steps, 6-7 pages |
| Writer and critic, then guard auto-fix, then fresh redrafts told why the last fell short | 2 rounds, 2 fixes, 3 attempts, stop when not improving, 7-minute budget |
| Calendar planner: chunks in parallel, validated in code (posts per week, pillar mix, unique topics) | 3 repairs |
| Lead engine (below) | 3 rounds, 24 searches and 60 page reads per run |
| Learning: operator edits and rejections become voice rules for the next run | 15 rules |

## 6. Leads: found on the open web, reachable, explained

```mermaid
flowchart TB
  Q[Queries in 7 families<br/>hiring, funding, directories, top lists,<br/>events, communities, competitors' customers] --> SR[Tavily search<br/>operator's key, budgeted]
  SR --> NR[Companies named in results<br/>model, each traced to a result]
  SR --> LP[List pages<br/>company links read from the HTML]
  NR & LP --> FL[Filters: the client itself, competitors,<br/>do-not-contact, duplicates, clear region mismatch]
  FL --> HP[Each company's own homepage]
  HP --> SC[Fit score with quotes from the company's own data]
  SC --> CR[Contact route, in order:<br/>a person named on its own team page,<br/>an email published on its own site or a team inbox,<br/>a LinkedIn URL from search results,<br/>Hunter only with the operator's key]
  CR --> KEEP[Lead with source page and confidence]
  CR --> DROP[No route: dropped and counted]
  FL & SC & DROP --> FUN[Funnel shown to the operator,<br/>with a suggestion and Broaden and retry]
```

Email addresses are never guessed or built from name patterns. linkedin.com is never fetched; only URLs that a search
returned are used. Outreach drafts are short, evidence-based, carry an opt-out line and never name the operator's
business; the operator opens them in Gmail or copies them, sends them, and marks them sent.

## 7. A model call

```mermaid
flowchart LR
  A[Agent] --> K[Lane: the client's owner's key<br/>and a client built for it]
  K --> P1{Preflight<br/>cancel, run budget, monthly cap}
  P1 --> C{Cache hit?<br/>key includes client and provider}
  C -- yes --> L[Ledger row, cost 0]
  C -- no --> R[Plan the tier's chain on that provider<br/>drop models the request is too big for<br/>or whose daily quota is used up]
  R --> B[Per-key, per-model pacing<br/>Groq free-tier limits only]
  B --> Q[Provider call<br/>retries, breaker, semaphore]
  Q --> V[Validate against the schema<br/>repair turn if needed]
  V --> L2[Ledger: tokens, provider time, pacing wait, 429 wait, scrubbed errors, cost]
```

| Tier | Groq | OpenAI | Anthropic | Used for |
|---|---|---|---|---|
| big | gpt-oss-120b, then qwen3.8-27b | gpt-6.1-sol | claude-sonnet-5-5 | posts, outreach, strategy, calendar, voice |
| research, small | gpt-oss-20b, qwen3.8-27b, gpt-oss-120b | gpt-6-luna | claude-haiku-5-5 | research decisions, queries, scoring, people on team pages |
| extract | gpt-oss-120b, qwen3.8-27b, gpt-oss-20b | gpt-6.1-terra | claude-haiku-5-5 | one extraction from an evidence pack |
| judge | qwen3.8-27b, gpt-oss-120b | gpt-6.1-terra | claude-haiku-5-5 | the critic (never the writer's model) |
| guard | Prompt Guard 86M, 22M | gpt-6-luna, asked in words | claude-haiku-5-5, asked in words | prompt-injection check |

The table lives in one place (`PROVIDER_ROUTES` in `app/core/config.py`) and can be overridden for development with
`MODEL_ROUTES_JSON`. Groq's free tier allows 8,000 tokens a minute per model; the gateway paces per key and per model
and spills to the next model in the tier when the first is busy.

## 8. Real vs not real

| Piece | Status |
|---|---|
| Groq adapter | real, measured on live runs |
| OpenAI adapter | protocol verified against an OpenAI-compatible endpoint (Groq's), not against OpenAI; contract-tested with mocks |
| Anthropic adapter | contract-tested with the official SDK over a mock transport; not run against Anthropic's API |
| Web search (Tavily), page fetch, list mining, contact routes | real, measured on live runs |
| Hunter.io | optional; contract-tested with mocks; not needed for leads |
| Google sign-in | real code, tested against a fake Google that signs real RS256 tokens; verified live only once the operator signs in |
| Sending email, publishing posts | not done by the system: the operator does it and marks it done |
| Fixtures and mock models | tests and the labelled local demo only; production refuses to load them |
