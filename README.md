# Growth OS

A done-for-you growth portal for busy founders. Agents research the founder's market, competitors and voice, plan an 8-week LinkedIn content calendar, draft posts, find qualified leads and draft outreach. The founder only **approves, edits or rejects**. Nothing is published or sent without a human.

- **Plan and architecture:** [PLAN.md](PLAN.md)
- **Decisions, trade-offs and lessons from live runs:** [DECISIONS.md](DECISIONS.md)

## Run it

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Leave `GROQ_API_KEY` empty for **mock mode**: deterministic, offline and free, with every agent backed by a responder that uses the real tools and data. With a key, the app calls Groq (and Gemini if `GOOGLE_API_KEY` is set).

```bash
.venv/bin/uvicorn app.main:app --reload
```

Open http://localhost:8000. The database seeds itself on first start with 12 fictional clients. Demo logins (password `growthos-demo`):

| Login | Role | Sees |
|---|---|---|
| `am.sam@growthos.example` | Account manager (6 clients) | Ops console: run discovery/weekly cycles, approve strategy briefs, traces, costs, guardrails |
| `priya@ledgerloop.example` | Founder, LedgerLoop | Approval inbox with post previews and outreach, calendar, leads, results |
| `marcus@shiftwise.example` | Founder, ShiftWise | Same, for ShiftWise only |
| `admin@growthos.example` | Platform admin | All clients, budgets |
| `viewer@ledgerloop.example` | Read-only | LedgerLoop, no actions |

To drive a running server through a full demo (discovery for all clients, approve the strategy gates, two weekly cycles, a few founder decisions):

```bash
.venv/bin/python scripts/demo_drive.py http://localhost:8000
```

## Test, evaluate, stress

```bash
.venv/bin/python -m pytest
```

```bash
.venv/bin/python -m evals.run_evals
```

```bash
.venv/bin/python -m loadtest.stress --tenants 100
```

Add `--live` to the eval run to use real models; that also runs judge calibration.

## How the interviewer's five topics map to code

| Topic | Where |
|---|---|
| **Agentic loops** | `app/agents/runtime/loop.py`: one bounded loop for every agent (step cap, budget and cancel checks, tool guard, untrusted-data wrapping, schema validation with repair turns, trace). Tool-using agents finish by calling `submit_result`. Writer ↔ critic loop in `app/agents/content/agents.py`. Goal-seeking lead top-up loop in `app/agents/leads/agents.py`. Learning from founder edits in `reflect_on_feedback`. |
| **Coordination** | `app/agents/orchestrator/engine.py`: discovery and weekly DAGs, node checkpoints, retries, resume after crash, rerun-from-node, human gates. `scheduler.py`: fair round-robin scheduler with global and per-client caps. Agents hand off only typed, versioned artifacts (`app/agents/blackboard.py`, `schemas.py`). |
| **Evals** | `evals/run_evals.py` + `evals/baseline.json` (CI gate), `evals/datasets/` (red-team with benign controls, judge calibration), online signals in `app/services/kpis.py` (approval rate, edit distance). |
| **Cost** | `app/llm/gateway.py`: tiered routing, per-model token buckets, per-run and per-client monthly budgets, kill switch, response cache, cost ledger on every call. Cost views in the ops console. |
| **Guardrails** | `app/guardrails/`: input (hidden-markup stripping, heuristics + Llama Prompt Guard, quarantine), tool (allowlist, schema, URL allowlist, no model-chosen tenant), output (leakage, canary, PII, uncited stats, disparagement, disclaimers, near-duplicates, spam), process (state machines, send gate). |
| Security | `app/core/security.py` (RBAC policy table), `app/db/session.py` (ORM tenant interceptor), `migrations/postgres_rls.sql`, `tests/test_api_security.py`, `tests/test_tenancy.py` |

## Results

### Tests

54 tests: tenancy interceptor and contextvar propagation, gateway (retries, breaker, budgets, cache, token bucket), runtime (repair, tool guard, step cap), guardrails, state machines, end-to-end pipeline, approvals and learning loop, crash resume, API role matrix and cross-tenant probes.

### Eval gate (mock mode), from [evals/reports/eval-mock.md](evals/reports/eval-mock.md)

| | LedgerLoop | ShiftWise |
|---|---|---|
| 8-week calendar valid (≥ 5 posts/week) | yes | yes |
| Drafts reaching the founder | 100% | 100% |
| Leads per week | 20, 20 | 20, 20 |
| Suppressed / competitor / contactless leads kept | 0 | 0 |
| Injection pages caught | 2 | 1 |
| Injected text found in any output | 0 | 0 |

Guardrails: red-team block recall 1.0 (with the right rule), benign false-positive rate 0.0, input-injection recall 0.8. The miss is the obfuscated case, which only the live classifier is meant to catch.

### Stress test, from [loadtest/reports/stress-100.md](loadtest/reports/stress-100.md)

Mock models with 100–600 ms latency, 5% injected 429s and 2% 5xx errors. Each tenant runs discovery plus one weekly cycle.

| | 12 tenants | 100 tenants |
|---|---|---|
| Runs succeeded | 24/24 | 200/200 |
| Failed nodes / DB lock errors | 0 / 0 | 0 / 0 |
| Wall time | ~23 s | ~80 s |
| Model calls/s | ~60 | ~145 |
| Clients meeting posts / calendar / leads targets | 12 / 12 / 12 | 100 / 100 / 100 |
| Cross-tenant violations | 0 | 0 |
| Approval API p95 (50 concurrent) | ~465 ms | ~465 ms |

### Live run (Groq)

See [DECISIONS.md §5](DECISIONS.md#5-what-the-first-live-run-taught) for what the first live run broke and how it was fixed.
