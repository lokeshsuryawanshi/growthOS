# Evals and guardrails

Principle: **the model proposes, code disposes.** Anything that can be checked in code is checked in code, not requested in a prompt. LLM judging is used only for what code cannot measure (voice, hook quality), and the judge itself is measured.

## Guardrails (what stops bad output)

| Layer | Control | Where |
|---|---|---|
| **Input** | Strip hidden markup, HTML comments and zero-width characters from every fetched page and lead field. Heuristics plus Llama Prompt Guard score injection risk. Visible injection: content is **quarantined** (agents never see it). Hidden-only injection: cleaned text is kept, a high incident is logged. | `app/guardrails/input_guard.py` |
| **Untrusted data** | Anything from the web is wrapped in `<untrusted_data>` tags; the system prompt says never to follow instructions in it. A canary string in every system prompt detects prompt leakage. | `app/agents/runtime/loop.py` |
| **Tools** | Allowlist of tools per agent, schema validation of arguments, call caps, a model can never pass a tenant id, `fetch_page` only opens URLs a search returned or the client supplied, SSRF-safe fetcher (no private IPs, redirects re-checked, size and type limits). | `loop.py`, `app/integrations/fetch.py` |
| **Output: safety** | cross-tenant leakage (other clients' names), canary leak, PII (emails, phone numbers) in content: **critical**, never auto-fixed. Banned topics, disclaimers required by trigger words, competitor disparagement. | `app/guardrails/output_guard.py` |
| **Output: factual** | **Claim-level grounding**: every figure must have a claim with a verbatim quote from a known source, the quote must contain the figure, and the figure must be used for what the source says (checks the words around the number). Cited URLs in research must have actually been seen; unverifiable facts are dropped and counted. | `check_claims`, `research/agents.py` |
| **Output: originality** | Near-duplicate of an earlier post, copying a founder sample or example hook. | `check_post`, `check_copying` |
| **Output: quality** | length, hashtag limits, banned phrases; outreach: spam signals, false familiarity, personalisation, one clear ask. | `check_post`, `check_outreach` |
| **Process** | State machines make illegal moves impossible (nothing reaches "approved" without a human; an email is `approved -> sending -> sent` with a lock so it cannot send twice). Send gate checks role, approval, verified recipient, do-not-contact list, daily cap, kill switch, Gmail connection. | `app/guardrails/state.py`, `app/services/approvals.py` |
| **Cost** | Per-run, per-client monthly and service-wide daily budgets; per-client kill switch; bounded retries and circuit breakers on every provider. | `app/llm/gateway.py` |
| **Access** | RBAC table, tenant interceptor, 404 on foreign tenants, rate limits. | `app/core/security.py`, `app/db/session.py` |

Severity rule: critical and high violations block the draft; one automatic rewrite is tried for high (never for critical); medium and low are shown to the reviewer as warnings. Human edits are checked too.

## Evals (what proves it works)

### Deterministic evals (no LLM, run in CI)
`python -m evals.run_evals` runs on mock models and compares against `evals/baseline.json`. Any breach exits non-zero.

| Metric | Threshold | Last result |
|---|---|---|
| Red-team block recall (right rule fires) | 1.0 | 1.0 |
| Benign false-positive rate | 0.0 | 0.0 |
| Input-injection recall | 0.8 | 0.8 (the miss is an obfuscated case only the live classifier should catch) |
| Calendar valid (8 weeks, 5+ posts/week) | 1 | pass |
| Posts reaching the founder | 0.9 | pass |
| Lead policy violations (suppressed, competitor kept) | 0 | 0 |
| Artifact contamination (injected text in outputs) | 0 | 0 |
| Cost per client cycle | under $0.50 | about $0.008 on mock pricing |

The red-team set (`evals/datasets/redteam.json`) includes benign controls so the gate measures false positives as well as recall.

### Test suite (150 tests)
Grounding regressions (the "1,100 SMBs" misattributed-number bug and copied founder hooks have their own tests), integration failures (quota, bad key, outage, retries), orchestration (retry, fail-fast, blocked dependents, checkpoint reuse, crash resume, fairness, budget stop), the send gate (double click sends once, unverified or suppressed recipient refused), tenant isolation, RBAC, and a production-mode boot audit (no demo data, no mock output).

### LLM judge (live only)
The critic uses a different model family than the writer. `evals/datasets/judge_calibration.json` holds hand-scored drafts; the gate requires the judge within one point of the human at least 75% of the time and a Spearman correlation of 0.6 or better.

### Online signals
Founder behaviour is the real quality metric: approval rate, first-pass approval rate, and edit distance between draft and approved text. Edits feed the learning loop.

### Load and stress
`python -m loadtest.stress --tenants 100` (mock models with latency and injected 429/5xx): 100/100 clients met posts, calendar and lead targets; 0 failed steps; 0 database lock errors; 0 cross-tenant violations; API p95 under 400 ms.

## What to say in the interview
1. **Design:** workflow with agents inside; why (predictable cost, checkpoints, human gates, testable).
2. **Enforcement:** pick one rule and show where code enforces it, e.g. the send gate or tenant interceptor.
3. **Honesty:** production refuses mock data; every provider failure becomes a message with the fix; research claims that cannot be tied to a seen source are removed.
4. **A real bug you found and fixed:** a live run exposed a customer count reused as a market statistic; it is now a claim-level check with a regression test. The lead scout also failed on small models, so search moved into code.
5. **Limits, stated plainly:** free-tier rate limits make runs slow; the stress test uses mock models; Hunter and Gmail were tested against mock HTTP; no LinkedIn publishing; in-process orchestrator and SQLite are single-worker, with the Postgres and queue path described.
6. **Numbers:** see `docs/ECONOMICS.md` for cost per client and run time.

## Known gaps
Search credits are not in the cost ledger; deterministic evals for brand voice and factual accuracy exist as tests but are not yet a separate eval report; the live eval (`--live`) was last run before the refactor.
