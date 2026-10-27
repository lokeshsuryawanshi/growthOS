# Cost, unit economics and run time

Every number below comes from the ledgers of a **live run on 2026-10-27** (`loadtest/reports/live-run.md`): a fresh workspace, real Groq, Gemini, Tavily and Hunter.io calls, on their free tiers. Costs are what the same usage costs at the **paid list prices checked that day** on each provider's page (Groq model docs, Google AI pricing, Tavily and Hunter pricing, Anthropic and OpenAI pricing). On the free tiers the actual bill was $0.

## 1. One client, measured

| Run | How often | Wall time | Model calls | Tokens in / out | AI cost | Search and contacts |
|---|---|---|---|---|---|---|
| Research and strategy | once (and on request) | **90 s** | 33 | 51,124 / 13,854 | $0.048 | $0.28 (35 Tavily searches) |
| Posts and calendar (5 posts) | weekly | **147 s** | 17 | 25,186 / 4,370 | $0.016 | $0 |
| Lead search and outreach (8 leads, 6 verified emails) | weekly | **18 s** | 21 | 15,358 / 3,513 | $0.0065 | $0.25 (5 searches, 8 Hunter searches, 1 verification) |

Before this work the same runs took about 14-20 minutes (research) and 20-35 minutes (a weekly cycle), with only about 2.5 minutes of actual model time: the rest was waiting on free-tier limits.

**Per client per month** (1 research run, 4 content runs, 4 lead runs): about **213,000 input and 45,000 output tokens**, **$0.14 of AI**, $0.44 of Tavily (55 searches) and $0.83 of Hunter credits (34). Variable cost about **$1.41 per client per month**, or **$0.58 without Hunter** (leads are then companies without a contact).

Unit costs: about **$0.003 of AI per ready post**, and about **$0.03 per lead** ($0.04 per lead with a verified email), mostly the Hunter lookup.

## 2. The same work on other models

AI cost only, for the measured token counts priced at each model's list price (per 1M tokens). Frontier models would spend somewhat different token counts (more reasoning, fewer repairs), so read the expensive rows as order of magnitude.

| Model | $ in / out | Research run | Content run | Lead run | Per client / month | 12 clients | 100 clients |
|---|---|---|---|---|---|---|---|
| **Measured mix** (gpt-oss-120b, 20b, qwen3.8-27b, Gemini Flash-Lite) | as routed | $0.048 | $0.016 | $0.0065 | **$0.14** | $1.66 | $14 |
| Groq gpt-oss-120b only | 0.15 / 0.60 | $0.016 | $0.006 | $0.0044 | $0.06 | $0.71 | $6 |
| Groq gpt-oss-20b only | 0.075 / 0.30 | $0.008 | $0.003 | $0.0022 | $0.03 | $0.36 | $3 |
| Claude Haiku 5.5 | 0.10 / 0.50 | $0.012 | $0.005 | $0.0033 | $0.04 | $0.53 | $4 |
| OpenAI gpt-5-mini | 0.25 / 2.00 | $0.040 | $0.015 | $0.011 | $0.14 | $1.73 | $14 |
| Gemini 3.5 Flash-Lite only | 0.30 / 2.50 | $0.050 | $0.018 | $0.013 | $0.18 | $2.13 | $18 |
| Groq qwen3.8-27b only | 0.80 / 4.00 | $0.096 | $0.038 | $0.026 | $0.35 | $4.23 | $35 |
| Claude Sonnet 5.5 | 2.00 / 10.00 | $0.241 | $0.094 | $0.066 | $0.88 | $10.57 | $88 |
| OpenAI gpt-6.1-sol | 2.00 / 10.00 | $0.241 | $0.094 | $0.066 | $0.88 | $10.57 | $88 |
| Claude Opus 5.5 | 4.00 / 20.00 | $0.482 | $0.188 | $0.132 | $1.76 | $21.13 | $176 |
| OpenAI gpt-5.5 | 5.00 / 30.00 | $0.671 | $0.257 | $0.182 | $2.43 | $29.14 | $243 |

Two things stand out. Even at frontier prices the AI is about $2 per client per month, so **margin is decided by contact data and hosting, not tokens**. And in the measured mix, **qwen3.8-27b carries most of the AI cost** although it handled only 9 of 71 calls: it is where big-tier work spills when gpt-oss-120b is busy under the free tier's 8,000 tokens/minute, and it is the priciest model in the mix. On a paid Groq plan (250,000 tokens/minute per model) spillover rarely happens and the mix moves toward the gpt-oss-120b row; spillover can also be pointed at a cheaper model with `ROUTE_BIG`.

## 3. Platform cost by client count

| Clients | Hosting | AI (paid prices) | Tavily | Hunter.io | Total per month |
|---|---|---|---|---|---|
| 0 | $0 (free Render instance; free hosted Postgres to keep data) or $7.25 (Starter + 1 GB disk) | $0 | $0 | $0 | **$0 to $7** |
| 1 | same | $0.14 (or $0 on free tiers) | $0 (free 1,000 credits) | $0 (free plan: 50 credits) | **$0 to $7** |
| 12 | $7.25 recommended | $1.66 (or $0) | $0 (660 credits fit the free tier) | $49 (Starter: 2,000 credits; 408 needed) | **about $58** |
| 100 | larger instance and Postgres (not priced here) | $14 | about $44 (5,500 credits: $30 plan + pay-as-you-go) | $149 (Growth: 10,000 credits; 3,400 needed) | **about $207 + hosting** |

Free-tier fit for 12 clients: Tavily's 1,000 free credits cover them; Groq's free tier (about 200,000 tokens and 1,000 requests per day per model, across three models) covers 12 weekly cycles but with the pacing waits shown above; Hunter's free 50 credits cover about one client, so 12 clients need a Starter plan. **Hunter is the one cost that has to be paid from client one** if leads need contact emails.

## 4. Run time and what limits it

| Mode | Research and strategy | Content run | Lead run |
|---|---|---|---|
| **Free tiers, measured** | 90 s | 147 s | 18 s |
| Paid tiers, estimated from model time alone | about 60 s | about 45 s | about 15 s |

The content run's 147 s is mostly pacing: five drafts in parallel on two writer models at 8,000 tokens/minute each (448 call-seconds of waiting against 42 s of model time). The estimate for paid tiers removes the waiting and keeps the measured model latency; it is not a measurement.

Where the speed came from (details in DECISIONS.md §5b): each tier leads with a different model because limits are per model, with spillover to the next model in the chain; `max_tokens` sized from measured use (Groq counts the reservation); research rebuilt as a decide-act loop with compact observations and one extraction on Gemini; Prompt Guard scoring only what models see; steps started as soon as their own dependencies finish.

## 5. How cost is controlled

| Lever | How |
|---|---|
| Tiered routing | small and cheap models for decisions, scoring and planning; the big model only for writing and strategy; Gemini for large-input extraction |
| Do expensive work rarely | research once per client; trends reused for 6 days; search results cached per client for 12 hours |
| Bounded loops | step, attempt and time limits on every loop; redrafts stop when they stop improving |
| Budgets | per run, per client per month (incident at 80%, stop at 100%), and service-wide per day |
| Kill switch | per client, checked by the gateway, the orchestrator and the send gate |
| Contacts | Hunter lookups only for leads that already qualified, within the quota left this month |
| Visibility | every model call and every search, lookup and send in a ledger; live tokens, cost and credits on the run page; time split into model time, pacing and 429 backoff on the agency run page |
