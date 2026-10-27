# Live run on real providers (2026-10-27)

Fresh database, one new workspace, production mode, real providers on their free tiers: Groq (gpt-oss-120b, gpt-oss-20b, qwen3.8-27b, Llama Prompt Guard 2), Gemini 3.5 Flash-Lite, Tavily web search, Hunter.io. The test client is a consumer furniture and appliance rental company in India with a real public website. The strategy gate was approved by the script the moment it was reached, so human review time is not counted. Produced with `scripts/live_run.py`; raw numbers in `live-run.json`.

Costs are what the same usage would cost at the paid list prices checked on 2026-10-27; on these free tiers the actual bill was $0.

## Result

| Run | Wall time | Outcome | Model calls | Tokens in / out | AI cost | Search and contact cost |
|---|---|---|---|---|---|---|
| Research and strategy (8-week calendar, 40 slots) | **90 s** | succeeded | 33 | 51,124 / 13,854 | $0.048 | $0.28 (35 Tavily searches) |
| Posts and calendar | **147 s** | succeeded: **5 of 5** posts ready, all at the editor's 4.0 bar | 17 | 25,186 / 4,370 | $0.016 | $0 |
| Lead search and outreach | **18 s** | succeeded: **8 leads** (fit 80-85), **6 with Hunter-verified emails**, 6 emails drafted for approval | 21 | 15,358 / 3,513 | $0.0065 | $0.25 (5 searches, 8 domain searches, 1 verification) |
| **Whole cycle** (research, then posts and leads side by side) | **about 4 min** | | 71 | 91,668 / 21,737 | **$0.071** | **$0.53** |

Before this work, the same flow took about 14-20 minutes for research and 20-35 minutes for a weekly cycle on the same free tiers.

## Where the time went

| Step | Seconds | |
|---|---|---|
| ingest (read the website, scan inputs) | 7 | |
| market, competitors, trends | 18, 23, 28 | market and competitors in parallel; trends starts when market is done |
| voice, ICP, strategy | 2, 2, 3 | |
| 8-week calendar | 33 | two 4-week chunks planned in parallel |
| draft the week's posts | 134 | 5 posts in parallel; **most of it waiting on Groq's 8,000 tokens/minute free limit** (448 call-seconds of pacing vs 42 s of model time) |
| find leads, draft outreach | 15, 3 | |

On a paid tier (Groq's Developer plan allows 250,000 tokens/minute per model) the pacing wait disappears; the remaining time is model latency.

## Models used

| Model | Role in this run | Calls | Notes |
|---|---|---|---|
| gpt-oss-120b (Groq) | writer, strategy, calendar, outreach | 17 | $0.15 / $0.60 per 1M |
| qwen3.8-27b (Groq) | spillover for big-tier work when 120b was busy | 9 | $0.80 / $4.00 per 1M: the priciest model here, so most of the AI cost |
| gpt-oss-20b (Groq) | research decisions, query planning, lead scoring | 14 | $0.075 / $0.30 per 1M |
| Gemini 3.5 Flash-Lite | critic, research and lead extraction | 12 | $0.30 / $2.50 per 1M |
| Llama Prompt Guard 2 86M (Groq) | injection classifier on pages and lead signals | 19 | $0.04 per 1M |

## What went wrong on the way (and was fixed)

The first lead run in this measurement kept **0 of 8** candidates: the scorer marked every company down for missing headcount and location, fields that web-found companies never have. The scoring rule was fixed and the lead step re-run on the same workspace (with the rejected rows cleared, as on a first run); that run is the one in the table. Earlier runs the same day found and fixed: an un-awaited async call, a Unicode hyphen that failed every calendar slot, a strict critic that held posts below the bar, quoted search queries that returned nothing, and a provider being switched off for a minute after one empty result. See DECISIONS.md.
