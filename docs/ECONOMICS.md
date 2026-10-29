# Cost and run time

**Who pays what.** Each operator uses their own AI, search and contact accounts (their own keys), so they pay those
providers directly and see what they spent on the *API keys & spend* page (from the same ledger the system uses). The
platform pays for hosting only: one free Render web service and one free Neon database, **$0 a month** at this size.
An optional monthly cap per account stops runs when reached.

## 1. Measured per run (live, 2026-10-29)

Live runs on Groq's free tier and Tavily, for the six voice-sample profiles (`scripts/live_run.py`). Cost is what the
same tokens cost at the providers' paid list prices (the free tiers billed $0). Means over the runs that finished
(discovery 4, content 5, leads 3; first round, before the last fixes).

| Run | Model calls | Tokens in / out | AI cost (Groq prices) | Searches | Search cost | Wall time |
|---|---|---|---|---|---|---|
| Research and strategy | 23-36 | 38-53k / 10-18k | **$0.034** | 32 | $0.26 | 118-307 s |
| Posts for one week (5) | 35-44 | 71-98k / 12-19k | **$0.058** | 0 | $0 | 434-799 s |
| Leads for one week | 71-107 | 53-88k / 9-22k | **$0.019** | 23 | $0.19 | 563-641 s |

**Where the time goes.** In the post runs the models themselves worked 40-54 s; the rest was waiting on Groq's free
tier (8,000 tokens a minute per model, counted on the reserved output too) — 1,490 to 2,815 call-seconds of pacing per
run, across parallel calls. A post run and a lead run started together share one key's budget and both slow down.
The free tier also has a **daily** token limit per model (200,000 tokens a day for `openai/gpt-oss-120b` when measured).
A post run uses about 110,000, so one free key manages about two post runs a day; on 2026-10-29 eight post runs stopped on
that limit (and said so). On
a paid Groq plan, or with an OpenAI or Anthropic key (whose limits depend on the account), most of that wait goes.

## 2. Per client per month

One research run, four post runs and four lead runs.

| Item | Groq | OpenAI (estimate) | Anthropic (estimate) |
|---|---|---|---|
| AI | **$0.35** | $1.43 | $1.20 |
| Tavily search (~125 credits at $0.008) | $1.01 | $1.01 | $1.01 |
| Hunter.io | optional; leads do not need it | | |
| **Total** | **about $1.36** | about $2.44 | about $2.21 |

The OpenAI and Anthropic columns apply their list prices to the *measured* tokens per tier (writer `gpt-6.1-sol` /
`claude-sonnet-5-5` at $2/$10 per 1M tokens; extraction and critic `gpt-6.1-terra` at $1/$5 or `claude-haiku-5-5` at
$0.10/$0.50; small tasks `gpt-6-luna` or `claude-haiku-5-5` at $0.10/$0.50). They are estimates: those models tokenise
and write differently, and neither was run live (no paid key).

Tavily's free plan (1,000 credits a month) covers about eight clients at this rate.

## 3. Prices used

| Provider | Model or operation | USD per 1M tokens in / out, or per unit | Checked |
|---|---|---|---|
| Groq | openai/gpt-oss-120b | 0.15 / 0.60 | 2026-10-27, console.groq.com/docs/models |
| Groq | openai/gpt-oss-20b | 0.075 / 0.30 | 2026-10-27 |
| Groq | qwen/qwen3.8-27b | 0.80 / 4.00 | 2026-10-27 |
| Groq | Prompt Guard 86M / 22M | 0.04 / 0.03 | 2026-10-27 |
| OpenAI | gpt-6.1-sol / gpt-6.1-terra / gpt-6-luna | 2/10 · 1/5 · 0.10/0.50 | 2026-10-29, developers.openai.com/api/docs/pricing |
| Anthropic | claude-sonnet-5-5 / claude-haiku-5-5 | 2/10 · 0.10/0.50 (Haiku up to 100K-token prompts) | 2026-10-29 |
| Tavily | basic search | 1 credit, $0.008 pay-as-you-go | 2026-10-27, tavily.com/pricing |
| Hunter.io | domain search / verification | 1 / 0.5 credit, $0.0245 a credit on Starter | 2026-10-27, hunter.io/pricing |

Prices change; the table in `app/core/config.py` (`MODEL_PRICES`, `EXTERNAL_PRICES`) is the one the ledger uses.

## 4. Levers

- **Provider and plan.** The model table per provider is in one place (`PROVIDER_ROUTES`). On Groq's free tier the
  binding limit is tokens per minute, not money.
- **Bounded work.** Research: 3 steps and about 7 pages. Posts: 2 editor rounds, 3 attempts, a 7-minute budget. Leads:
  24 searches and 60 page reads per run. Every run has a dollar budget, and the account an optional monthly cap.
- **Reuse.** Searches are cached per client for 12 hours; the trend report is refreshed only when it is over 6 days
  old; deterministic model calls are cached per client.
