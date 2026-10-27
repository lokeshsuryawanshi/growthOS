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
| Factual accuracy | Delivered drafts that use figures (each checked) | 15 |
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


## RBAC and tenant isolation

| Metric | Value |
|---|---|
| Matrix checks (role x action x endpoint, API and UI) | 315 |
| Matrix pass rate | 1.0 |
| Cross-tenant access violations | 0 |
| Privilege escalation attempts / successes | 14 / 0 |

Escalation attempts (all must fail):

- founder creates a platform_admin: HTTP 403
- founder promotes the viewer to platform_admin: HTTP 403
- founder creates a account_manager: HTTP 403
- founder promotes the viewer to account_manager: HTTP 403
- viewer promotes themself to owner: HTTP 403
- account manager creates an account manager: HTTP 403
- account manager assigns themself a client: HTTP 403
- founder adds a member to another workspace: HTTP 404
- last owner demotes themself: HTTP 409
- signup asks for the platform_admin role: HTTP 201
- token forged with a guessed secret: HTTP 401
- validly signed token for a user that does not exist: HTTP 401
- deactivated user's old token: HTTP 401
- removed member's old token: HTTP 401

## Pipeline (per demo client)

| Metric | ledgerloop | shiftwise |
|---|---|---|
| run_status | ['succeeded', 'succeeded', 'succeeded'] | ['succeeded', 'succeeded', 'succeeded'] |
| seconds | 1.2 | 1.1 |
| calendar_valid | 1 | 1 |
| topics_unique | 1 | 1 |
| posts | 10 | 10 |
| posts_ready | 10 | 10 |
| posts_ready_rate | 1.0 | 1.0 |
| avg_critic_score | 4.0 | 4.0 |
| avg_critic_rounds | 1.4 | 1.7 |
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
| llm_calls | 147 | 151 |
| tokens | 144805 | 152688 |
| cost_usd | 0.0252 | 0.0267 |
| cost_per_client_cycle_usd | 0.0084 | 0.0089 |
