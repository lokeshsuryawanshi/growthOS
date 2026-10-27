# Cost, unit economics and run time

All token numbers come from the real cost ledger (`llm_calls` table) of a live run on 2026-10-27: one test client, real Groq + Gemini + Tavily calls. Prices are list prices per million tokens and **must be re-checked before quoting**; the Groq/Gemini prices are the ones in `app/core/config.py` (`MODEL_PRICES`).

## 1. What one run costs in tokens (measured)

| Step | Model calls | Input tokens | Output tokens | Notes |
|---|---|---|---|---|
| Research and strategy (once per client) | 54 | 64,140 | 7,953 | voice, market, competitors, trends, strategy, ICP, 8-week calendar |
| Weekly cycle (first live attempt) | 36+ | 58,410+ | 3,301+ | measured before drafting finished; the old lead scout wasted ~43k input tokens on retries |
| Whole test client, everything incl. retries | 143 | 220,285 | 19,077 | failed and repeated work included |

About 40% of calls are tiny safety-scanner calls (Prompt Guard on every fetched page). Input tokens dominate (about 10 to 1) because agents read search results.

**Planning figures** used below (after the lead-scout fix, which removes the wasted retries):
- Research and strategy: 64k in / 8k out, once.
- Weekly cycle: about 100k in / 10k out, four times a month.
- **Per client per month: about 464k in / 48k out.**

## 2. Cost per client per month, by model

Same workload, different price points.

| Model scenario | $ in / $ out per 1M | Per client / month | 12 clients | 100 clients |
|---|---|---|---|---|
| Current mix on Groq (gpt-oss-120b writer, 20b scoring) | 0.15 / 0.60 (120b) | about $0.10 | about $1.2 | about $10 |
| All gpt-oss-20b | 0.075 / 0.30 | about $0.05 | about $0.6 | about $5 |
| Gemini 3.5 flash-lite | 0.10 / 0.40 | about $0.07 | about $0.8 | about $7 |
| Frontier-class model at the prices you gave | 2.50 / 10.00 | about $1.64 | about $20 | about $164 |
| Higher-priced frontier tier (3.00 / 15.00, check list price) | 3.00 / 15.00 | about $2.11 | about $25 | about $211 |

Formula: `(464k x price_in + 48k x price_out) / 1M`. Even at frontier prices the AI is roughly **$2 per client per month**, so margin is driven by other things (below).

## 3. Costs that are not model tokens

| Item | Basis | Status |
|---|---|---|
| Web search (Tavily) | about 1 credit per basic search (one test prompt used 2 credits); free tier 1,000 credits/month | search count per cycle is not yet logged: measure before promising a number |
| Hunter.io | one domain search per lead company, plus verifications | free plan is about 25 searches/month, so 10-20 leads/week per client needs a paid plan: check pricing |
| Hosting | Render starter instance plus 1 GB disk | about $7 to $8 a month: verify |
| Human time | approvals take the founder minutes per week | not a cost to us, but the real product constraint |

## 4. Run time

| Mode | Research and strategy | Weekly cycle |
|---|---|---|
| **Today (Groq free tier)** | about 14 min measured | 20 to 35 min measured (daily token cap hit: 200k tokens/day/model) |
| Paid tier, no rate limits (estimate, not measured) | 3 to 4 min | 4 to 6 min |

Why it is slow today: the whole ledger shows only about 2.5 minutes of actual model response time. The rest is our own pacing (`TokenBucket` in `app/llm/providers.py`) and 429 waits against Groq's free limits (8,000 tokens per minute per model, 200,000 per day). It is a provider limit, not agent logic.

## 5. How the system controls cost

- Tiered routing: small model for scoring and extraction, big model only for strategy and writing.
- Research is done once; `trends_refresh` skips when the report is under 6 days old.
- Per-run budget, per-client monthly budget (80% incident, 100% hard stop), service-wide daily budget, per-client kill switch.
- Response cache for deterministic work; compact artifact summaries instead of raw documents in prompts.
- Every call is written to the ledger with model, tokens, latency, retries and cost. The run page shows live tokens and cost.

## 6. Scaling to 100+ clients

- Orchestration was load-tested on mock models: 100 clients, 200/200 runs succeeded, 0 failed steps, 0 database lock errors, about 67 seconds, about 172 model calls per second, API p95 under 400 ms. This proves the scheduler and database, not real provider speed.
- Real throughput is limited by provider rate limits: at 8k tokens/minute, one client cycle needs about 15 minutes of a model's budget. 100 clients need a paid tier (or several providers) or the work queues.
- Production path: Postgres (RLS migration exists), a durable job queue, Redis rate limits, more than one worker.
