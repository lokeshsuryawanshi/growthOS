# Stress test: 12 tenants

| Metric | Value |
|---|---|
| config | {"tenants": 12, "latency_ms": [100, 600], "rate_limit_rate": 0.05, "transient_rate": 0.02, "global_concurrency": 48, "per_tenant_concurrency": 3, "provider_concurrency": 64} |
| wall_seconds | 22.6 |
| discovery_seconds | 9.2 |
| weekly_seconds | 13.4 |
| run_status | {"succeeded": 12, "weekly:succeeded": 12} |
| nodes | 192 |
| nodes_failed | 0 |
| node_seconds_p50 | 0.77 |
| node_seconds_p95 | 5.125 |
| queue_wait_p50 | 0.0 |
| queue_wait_p95 | 0.0 |
| fairness_mean_wait_max_over_min | 1.33 |
| scheduler_peak_concurrency | 28 |
| llm_calls | 1390 |
| llm_calls_per_second | 61.5 |
| llm_retries | 84 |
| llm_errors | 0 |
| injected_failures | {"429": 59, "5xx": 25} |
| db_lock_errors | 0 |
| posts | 60 |
| posts_blocked | 0 |
| leads | 218 |
| outreach | 218 |
| tenants_meeting_posts_target | 12/12 |
| tenants_meeting_calendar_target | 12/12 |
| tenants_meeting_leads_target | 12/12 |
| cross_tenant_fk_violations | 0 |
| cross_tenant_name_mentions | 0 |
| cost_usd_total | 0.1693 |
| cost_usd_per_tenant | 0.0141 |
| api | {"requests": 2000, "rps": 190.1, "p50_ms": 243.6, "p95_ms": 386.8, "p99_ms": 453.1, "errors": {}} |
