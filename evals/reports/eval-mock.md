# Eval report (mock)

Gate: **PASS**


## Guardrails

| Metric | Value |
|---|---|
| Red-team block recall (right rule) | 1.0 |
| Input injection recall | 0.8 |
| Benign false-positive rate | 0.0 |

Misses:
- input:obfuscated_spacing expected flag got pass 

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
| avg_critic_rounds | 1.3 | 1.5 |
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
| llm_calls | 145 | 147 |
| tokens | 141538 | 145925 |
| cost_usd | 0.025 | 0.0258 |
| cost_per_client_cycle_usd | 0.0083 | 0.0086 |
