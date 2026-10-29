# Evals and guardrails

Principle: **the model proposes, code disposes.** Anything code can check is checked in code, not asked for in a
prompt. A model judges only what code cannot (is this draft good?), and that judge is measured against reference scores. Who
measured what is always said: numbers from scripts are from scripts; the lead review was done by the implementing agent
(an AI), which is not the same as a human; **no human has rated the posts yet**.

## 1. Guardrails: what stops bad output

| Layer | Control | Where |
|---|---|---|
| **Accounts** | Every client belongs to one account. Every client route loads it through `tenant_or_404` (someone else's client is a 404, not a 403), and an ORM interceptor refuses any query on client data outside a `tenant_scope`. Sessions carry only the user id and are checked against the database on every request, so a deleted account is signed out at once. | `app/api/deps.py`, `app/core/tenancy.py` |
| **Keys** | Saved keys are encrypted (Fernet) with the owner's id inside the ciphertext, read by one module only, never shown again (last four characters only), and scrubbed from errors and logs. A run uses the key of the account that owns the client, never another one; a refused key stops the run and is marked "not working" on the keys page. | `app/services/keys.py`, `app/core/scrub.py`, `app/llm/gateway.py` |
| **Input** | Hidden markup, HTML comments and zero-width characters are stripped from every fetched page and lead field. Heuristics plus Llama Prompt Guard score injection risk on the part of a page a model sees; a page with visible injection is quarantined (no agent sees it). Text too long for the classifier is split, not skipped. | `app/guardrails/input_guard.py` |
| **Untrusted data** | Web content is wrapped in `<untrusted_data>` tags and the system prompt says never to follow instructions inside. A canary string in every system prompt detects prompt leaks. | `app/agents/runtime/loop.py` |
| **Tools and search** | Per-agent allowlists, schema validation, call caps; a model can never pass a client id; a page can be read only if a search returned it or the operator gave it; SSRF-safe fetcher (no private addresses, redirects re-checked, size and type limits). linkedin.com is never fetched. | `loop.py`, `app/integrations/` |
| **Research facts** | Each market fact, competitor and trend is checked against the text of the page it cites: its figures must appear there and most of its key words too. Unsupported claims go under "Not verified"; figures 3+ years old are labelled outdated. | `app/agents/research/agents.py` |
| **Posts: facts** | Every figure needs a claim with a verbatim quote from a known source; the quote must contain the figure, and the figure must be used for what the source says. | `check_claims` |
| **Posts: no invented life** | A model draft may not tell a first-person story ("last week a customer told me…") that the founder never told: the founder's material is the only source of anecdotes. | `invented_anecdote` |
| **Posts: no copying** | No 8 words in a row copied from the founder's samples; no reuse of the samples' examples (names, distinctive long words); no copied hooks; no near-duplicate of an earlier post. | `check_copying`, `reused_examples` |
| **Posts: voice, not template** | A style card is measured from the founder's own posts (sentence and paragraph length, person, punctuation, how they open and end). Code flags seven generic LinkedIn patterns (one-line hook, a line per sentence, list, engagement question at the end, stock phrase, emojis, hashtag tail) that the founder's own posts never use: the editor must have it removed before the draft passes, and if it survives it is shown as a warning. | `app/agents/content/style.py` |
| **Posts: safety** | Other clients' names, canary leak, emails and phone numbers: critical, never auto-fixed. Banned topics, disparaging a competitor, required disclaimer missing: high. Length, hashtags, banned phrases: warnings. | `app/guardrails/output_guard.py` |
| **Posts: quality** | A second model (a different family from the writer) scores each draft against an anchored rubric; 4.0 is the bar. A draft that never reaches it after the editor rounds is delivered labelled "below the editor's bar" and does not count toward the weekly target. | `app/agents/content/agents.py` |
| **Outreach** | Spam signals, false familiarity, no personalisation, more than one ask. The system never sends: the operator opens the approved email in Gmail (or copies it), sends it, and clicks "I sent it". That click is refused for an unapproved draft or an address on the do-not-contact list. | `check_outreach`, `app/services/approvals.py`, `app/services/compose.py` |
| **Leads** | A company is kept only if a search result or a list page names it, its own site loads (homepages are read for the leading candidates, within the budget), and there is a real way to reach a person: a senior person named on the company's own pages, an email the company publishes on its own domain, a LinkedIn profile from a search result that names the company, or Hunter. No email is ever guessed. Competitors, the client's own company, clear region and size mismatches and the do-not-contact list are dropped, each with a reason the operator can see. | `app/agents/leads/live.py`, `app/agents/leads/agents.py` |
| **Process** | State machines make illegal moves impossible: nothing reaches "approved" without the operator, nothing is "sent" or "posted" until the operator says so. | `app/guardrails/state.py` |
| **Cost** | Per-run budget, optional monthly cap per account, bounded retries and circuit breakers per key, per-model pacing on Groq, fixed search and page-read budgets for leads (24 and 60). | `app/llm/gateway.py`, `live.py` |

Severity rule: critical and high violations block the draft; up to two automatic rewrites are tried for high (never for
critical); a blocked slot gets fresh attempts told why; medium and low are shown to the operator as warnings. Posts and
emails are checked against 26 rules: 20 can block (critical or high), 6 are only ever warnings (length, hashtags,
banned phrase, not personalised, more than one ask, generic template). Operator edits
are checked too.

## 2. Evals: what proves it works

`python -m evals.run_evals` runs every offline section on mock models and fixtures and compares each metric with
`evals/baseline.json`; any breach exits non-zero. It runs in the test suite (`tests/test_evals.py`). Latest result:
**PASS** (`evals/reports/eval-mock.md`).

### 2.1 Account isolation (every client route, plus a mutation test)

`evals/isolation.py` finds every client route **from the app itself** (so a new route is tested without anyone adding
it), then tries each one as another account, signed out, and with forged, expired and stale sessions, and looks for
another account's data or any key in pages, headers and database rows.

| Metric | Threshold | Result |
|---|---|---|
| Client routes found (all tested) | at least 15 | **15** |
| Checks | | 56 |
| Cross-account access violations | 0 | **0** |
| Session escalations | 0 | **0** |
| Another account's data on a page | 0 | **0** |
| Keys visible or used for the wrong account | 0 | **0** |

**The eval has teeth.** `python -m evals.isolation --mutations` breaks isolation on purpose, five ways, and the eval must
notice each one. All 5 caught: route ownership check removed (8 violations), session signature and expiry not checked
(12 escalations), deleted accounts keep their sessions (2 escalations), every account owns every client (11
violations), runs use whichever account's key is found first (1 violation, 1 leak). The mutation test is what found
that, on FastAPI 0.162, the first version of the route finder saw 0 routes and so passed everything.

### 2.2 Guardrail red team (22 cases)

12 post cases, 3 outreach cases and 7 input-injection cases, including **benign controls** (a post that names a
competitor neutrally, text containing the word "instructions"), so false positives are measured as well as recall.

| Metric | Threshold | Offline | Live (with Prompt Guard on Groq) |
|---|---|---|---|
| Block recall, right rule fired | 1.0 | **1.0** | **1.0** |
| Benign false-positive rate | 0.0 | **0.0** | **0.0** |
| Input-injection recall | 0.8 | 0.8 (the miss is an obfuscated case only the classifier is meant to catch) | **1.0** |

### 2.3 The editor against reference scores (live)

The editor model is used as a quality gate, so it is checked against reference scores: `evals/datasets/judge_calibration.json`
holds 8 drafts with a score each (1 = unusable, 5 = publish as is). Gate: within one point of the reference at least 75% of the
time, Spearman at least 0.6. Measured on 2026-10-29 with the v2 editor prompt (`qwen/qwen3.8-27b` on Groq),
`python -m evals.run_evals --live`:

| Run | Within one point | Spearman | Misses |
|---|---|---|---|
| 1 | **0.75** | 0.74 | a 3 scored 1.0; a 1 scored 2.8 |
| 2 | **0.875** | 0.81 | |

It passes, but only just in run 1. With 8 items one disagreement moves the result 12.5 points. The reference scores
were written during the v1 build; the dataset does not record who wrote them, most likely the earlier implementing
agent, so this is **not** agreement with a human. The blind review (2.6) is the real test.

### 2.4 Voice and template (live output)

`python -m evals.voice --db <run db>` compares generated posts with the founder's own writing. The corpus is six real,
public writers (`evals/datasets/voice_samples/`, 8 posts each, 2 held out), collected by `scripts/collect_voice_samples.py`.

- **Voice distance**: a Burrows' Delta style distance over function words and style features. Is each generated post
  closer to its own founder than to the other five?
- **Template similarity**: the skeleton of a post (length, paragraphing, opening, ending, lists, stock phrases) compared
  across clients. If posts for different founders share one skeleton, the system is writing a template, not a voice.
- **Sanity check of the metric itself**: on the founders' real held-out posts, nearest-own is 5 of 12 (0.42),
  leave-one-out 0.48, against 0.17 by chance. So the metric can tell these writers apart, but not reliably post by post;
  read it per founder, not per post.

Measured on 2026-10-29 over every delivered post of the live runs (Groq free tier, `openai/gpt-oss-120b` writing):
56 posts for six profiles; report in `evals/reports/voice-live.json`.

| Metric | Generated posts | Real held-out posts | Generic template |
|---|---|---|---|
| Nearest of the six founders is the right one | **0.27** (chance 0.17) | 0.42 | |
| Closer to its own founder than the generic template is | **56 of 56** | | |
| Skeleton similarity across different clients (lower = more distinct) | **0.39** | 0.29 | 0.43 |
| Posts with a generic LinkedIn pattern left in | **1 of 56** (a stock phrase) | | |
| Delivered at the editor's 4.0 bar | **13 of 56** (23%) | | |

What this says, plainly:
- The posts are not the generic template: every one is closer to its founder than the template is, and the template
  detector leaves almost no pattern in.
- But they are **not yet in each founder's own voice**: a post's nearest founder is the right one only 27% of the time
  (real writing: 42%), and posts for different clients share more of one skeleton (0.39) than real writers do (0.29),
  though less than the template (0.43). There is a house style.
- The editor's bar is rarely reached on the free-tier model; the rest are delivered labelled "below the editor's bar"
  and do not count toward the weekly target. No live post run fully met its target (12 ended "succeeded with shortfall"); 8 more stopped
  when Groq's free daily token limit ran out, and said so. Two lead runs failed on bugs since fixed (a column missing
  from migration 0005; text too long for the injection classifier).
- Found in these runs and fixed: a number inside a product name ("Baochip-1x") was treated as a statistic, which
  blocked most drafts for that profile. Re-run after the fix: that profile went from 4 delivered posts (2 at the bar)
  to 8 (5 at the bar).
- Known strictness kept on purpose: a figure is blocked when the words around it differ from the source's ("1,110
  backers, a sizable community" against "1,110 backers"). Loosening it lets a customer count pass as a market size,
  the bug it was written for.

### 2.5 Leads (live output)

Measured on 2026-10-29 with the final lead engine, one week per profile (target 10-20 a week), Tavily search, no
Hunter. "Source names it" is mechanical (`scripts/check_leads.py` re-opens each page); "fit" is **the implementing
agent's own reading (an AI, not a person)**.

| Client (profile) | Leads | Source names it | Ways to reach someone | Fit, as read by the implementing agent |
|---|---|---|---|---|
| WP Engine (Jason Cohen) | **10** (target met) | 8 of 10 | named on own site 5, published email 4, LinkedIn 1 | all 10 are web or WordPress agencies, a real fit. 4 names carried review-site text and one email was `hr@`: both fixed since |
| April Dunford Positioning | 7 | 7 of 7 | LinkedIn 4, own site 2, email 1 | 7 B2B software companies, plausible buyers; 2 shown by their domain (fixed since). The earlier, pre-fix run had found giants (Salesforce, Oracle) with their CEOs |
| Podscan (Arvid Kahl) | 6 | 6 of 6 | LinkedIn 4, email 2 | 4 podcast and PR agencies fit; 2 are media-monitoring companies, closer to competitors than customers |
| VWO (Paras Chopra) | 3 | 2 of 3 | one each | 2 plausible (a SaaS company, a store); 1 agency, more a partner than a buyer |
| Baochip (Bunnie Huang) | 3 | 3 of 3 | own site 2, LinkedIn 1 | 3 of 3 fit (embedded security, IoT hardware, a hardware wallet) |
| Seth Godin Workshops | 0, or **4** with *Broaden* | 2 of 4 | mixed | 0: 61 candidates judged low fit, 6 too big; the run said so and suggested widening. Broadened: 4 weak leads, one on a misspelled domain (such leads are now dropped: "site does not load") |

Across the six: 33 leads; 28 traced to a page naming the company; all 13 emails are on the company's own domain (none
invented); linkedin.com never fetched. Only one profile reached the weekly minimum of 10. Most candidates are dropped
as low fit; every run says how many were dropped and why, and suggests what to widen.

### 2.6 Human review kit (not yet done)

`scripts/export_blind_review.py` builds a shuffled sheet of generated posts and the founders' own held-out posts with
nothing to say which is which; the user scores voice (1-5), "would post as is" and "sounds like a template";
`scripts/import_ratings.py` reports the result, including how often a real post was mistaken for a generated one and the
reverse. The portal has the same three ratings on every post. **No ratings have been collected yet**, so this document
makes no claim that the posts sound human.

### 2.7 Deterministic quality report (no model)

`evals/quality.py` measures, with the checks production uses, what an operator would see. Over the mock pipeline: 0
delivered drafts with a figure not backed by a source quote (18 drafts use figures), 0 with a banned phrase or a copied
sample, 0 near-duplicate pairs, 0 safety violations, format compliance 1.0; lead screening drops every suppressed,
competitor and contactless prospect in the labelled set and wrongly drops no good one. `--from-db` runs it over a real
run's database.

### 2.8 Pipeline

A full research run plus two weeks for two fictional clients on mock models, scored against business rules: a valid
8-week calendar with unique topics, at least the weekly post target at the editor's bar, no suppressed or competitor
leads, leads per week within range, no injected text in any artifact. All pass.

## 3. The test suite (282 tests)

`.venv/bin/python -m pytest -q` (SQLite, about 45 s). The same tests run on Postgres with
`TEST_DATABASE_URL=postgres://...` (a Neon test branch); they refuse to open the production database. The browser test
is opt-in: `pytest -m e2e tests/e2e`.

| Area | Tests | Covers |
|---|---|---|
| Integrations | 24 | search, safe fetch, Hunter, encryption and key rotation (mock HTTP) |
| Guardrails | 23 | input (incl. split classifier input), output, state machines |
| Provider adapters | 22 | Groq, OpenAI, Anthropic request and response shapes, errors, refused keys, no credit |
| Production integrity | 19 | production refuses mock data and weak secrets, boots empty, Postgres URLs, migrations match the models |
| Keys | 19 | format checks, the check call, encryption with owner binding, never shown again, scrubbing, wrong-owner refusal |
| Lead engine | 18 | every stage on mock HTTP, empty and thin cases, a site that does not load, region and size filters, broaden, already met, no fetch of linkedin.com |
| Accounts | 16 | Google sign-in creates one account, clients belong to it, delete client, delete account, monthly cap |
| Grounding | 15 | claim checks, numbers in product names, copied hooks and samples, invented anecdotes, misattributed figures |
| Google sign-in | 12 | state, nonce, PKCE, signature, issuer, audience, expiry, unverified email, allow-list |
| Gateway runtime, routing | 21 | retries, breaker, budgets, cache per account, pacing, per-provider model table |
| Orchestration | 11 | retry, fail-fast, blocked dependents, checkpoints, crash resume, fairness, event-driven starts |
| Web, legal pages, compose | 19 | every page renders, terms and privacy public, Gmail links and the long-email fallback |
| Research integrity, strategy | 15 | fabricated and unsupported sources, outdated figures, calendar rules, suggest changes |
| Other | 48 | voice eval, separate workflows, pipeline, tenancy, evals gate, database guard, sessions, weekly output, concurrency, browser |
