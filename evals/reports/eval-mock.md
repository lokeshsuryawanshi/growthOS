# Eval report (mock)

Gate: **PASS**


## Guardrails (red team and benign controls)

| Metric | Value |
|---|---|
| Red-team block recall (right rule) | 1.0 |
| Input injection recall | 0.8 |
| Benign false-positive rate | 0.0 |

Misses:
- input:obfuscated_spacing expected flag got pass 


## Deterministic quality report (no LLM)

| Area | Metric | Value |
|---|---|---|
| Factual accuracy | Delivered drafts with a figure not backed by a verbatim source quote | 0 |
| Factual accuracy | Delivered drafts that use figures (each checked) | 18 |
| Factual accuracy | Research claims kept as verified / flagged unverified | 21 / 1 |
| Factual accuracy | Research figures labelled outdated (3+ years old) | 2 |
| Brand voice | Drafts using a banned phrase (global or the client's own) | 0 |
| Brand voice | Drafts using a word the voice profile says to avoid | 0 |
| Brand voice | Drafts copying the founder's own sample posts or hooks | 0 |
| Output quality | Drafts delivered for review | 20 |
| Output quality | Share of delivered drafts at or above the editor's bar | 1.0 |
| Output quality | Mean editor score of delivered drafts | 4.0 |
| Output quality | Pairs of delivered drafts that are near duplicates | 0 |
| Output quality | Length within 300-3000 characters and at most 5 hashtags | 1.0 |
| Safety | Safety violations in delivered drafts (PII, leaks, disparagement, banned topics) | 0 |
| Safety | Attempts the guardrails blocked before a human saw them | 0 |
| Lead screening | Suppressed prospects screened out | 1.0 |
| Lead screening | Competitors screened out | 1.0 |
| Lead screening | Contactless prospects screened out | 1.0 |
| Lead screening | Duplicate people kept twice | 0 |
| Lead screening | Good prospects wrongly screened out | 0 |


## Account isolation

| Metric | Value |
|---|---|
| Client routes found in the app (all are tested) | 15 |
| Checks (other account, signed out, forged/expired/stale sessions, page leaks) | 56 |
| Cross-account access violations | 0 |
| Session escalations | 0 |
| Another account's data shown on a page | 0 |
| API keys visible in pages, headers or database rows, or used for the wrong account | 0 |
| Mutation test: deliberate breaks noticed | 5 of 5 |

Mutations (each must be caught):

- route ownership check removed: caught (8 violations, 0 escalations, 0 leaks)
- session signature and expiry not checked: caught (0 violations, 12 escalations, 0 leaks)
- deleted accounts keep their sessions: caught (0 violations, 2 escalations, 0 leaks)
- every account owns every client: caught (11 violations, 0 escalations, 0 leaks)
- runs use whichever account's key is found first: caught (0 violations, 0 escalations, 1 leaks)

## Pipeline (per demo client)

| Metric | ledgerloop | shiftwise |
|---|---|---|
| run_status | ['succeeded', 'succeeded', 'succeeded', 'succeeded', 'succeeded'] | ['succeeded', 'succeeded', 'succeeded', 'succeeded', 'succeeded'] |
| seconds | 1.4 | 1.2 |
| calendar_valid | 1 | 1 |
| topics_unique | 1 | 1 |
| posts | 10 | 10 |
| posts_ready | 10 | 10 |
| posts_ready_rate | 1.0 | 1.0 |
| avg_critic_score | 4.0 | 4.0 |
| avg_critic_rounds | 1.9 | 2.0 |
| guard_autofixes | 1 | 0 |
| pending_posts_with_violations | 0 | 0 |
| leads | 40 | 40 |
| leads_per_week | [20, 20] | [20, 20] |
| leads_week_in_range_rate | 1.0 | 1.0 |
| lead_policy_violations | 0 | 0 |
| explicit_low_fit_kept | 0 | 0 |
| adversarial_rejection_recall | {} | {} |
| outreach_drafts | 40 | 40 |
| injection_pages_caught | 2 | 1 |
| artifact_contamination | 0 | 0 |
| llm_calls | 156 | 157 |
| tokens | 169638 | 171136 |
| cost_usd | 0.0293 | 0.0296 |
| cost_per_client_cycle_usd | 0.0098 | 0.0099 |
