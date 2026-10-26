# Stress test: 12 tenants

| Metric | Value |
|---|---|
| config | {"tenants": 12, "latency_ms": [100, 600], "rate_limit_rate": 0.05, "transient_rate": 0.02, "global_concurrency": 48, "per_tenant_concurrency": 3, "provider_concurrency": 64} |
| wall_seconds | 23.4 |
| discovery_seconds | 8.4 |
| weekly_seconds | 15.0 |
| run_status | {"succeeded": 12, "weekly:succeeded": 12} |
| nodes | 192 |
| nodes_failed | 0 |
| node_seconds_p50 | 0.557 |
| node_seconds_p95 | 5.72 |
| queue_wait_p50 | 0.0 |
| queue_wait_p95 | 0.0 |
| fairness_mean_wait_max_over_min | 1.61 |
| scheduler_peak_concurrency | 29 |
| llm_calls | 1373 |
| llm_calls_per_second | 58.6 |
| llm_retries | 77 |
| llm_errors | 0 |
| injected_failures | {"429": 57, "5xx": 20} |
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
| cost_usd_total | 0.1543 |
| cost_usd_per_tenant | 0.0129 |
| api | {"requests": 1000, "rps": 147.1, "p50_ms": 328.8, "p95_ms": 473.7, "p99_ms": 545.5, "errors": {}} |
