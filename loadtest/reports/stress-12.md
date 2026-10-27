# Stress test: 12 tenants

| Metric | Value |
|---|---|
| config | {"tenants": 12, "latency_ms": [100, 600], "rate_limit_rate": 0.05, "transient_rate": 0.02, "global_concurrency": 48, "per_tenant_concurrency": 3, "provider_concurrency": 64} |
| wall_seconds | 17.9 |
| discovery_seconds | 7.3 |
| weekly_seconds | 10.6 |
| run_status | {"succeeded": 12, "weekly:succeeded": 12} |
| nodes | 192 |
| nodes_failed | 0 |
| node_seconds_p50 | 0.509 |
| node_seconds_p95 | 5.45 |
| queue_wait_p50 | 0.0 |
| queue_wait_p95 | 0.0 |
| fairness_mean_wait_max_over_min | 1.21 |
| scheduler_peak_concurrency | 30 |
| llm_calls | 923 |
| llm_calls_per_second | 51.5 |
| llm_retries | 82 |
| llm_errors | 0 |
| injected_failures | {"429": 55, "5xx": 27} |
| db_lock_errors | 0 |
| posts | 60 |
| posts_blocked | 0 |
| leads | 211 |
| outreach | 211 |
| tenants_meeting_posts_target | 12/12 |
| tenants_meeting_calendar_target | 12/12 |
| tenants_meeting_leads_target | 12/12 |
| cross_tenant_fk_violations | 0 |
| cross_tenant_name_mentions | 0 |
| cost_usd_total | 0.1597 |
| cost_usd_per_tenant | 0.0133 |
| api | {"requests": 2000, "rps": 115.5, "p50_ms": 408.5, "p95_ms": 626.6, "p99_ms": 696.0, "errors": {}} |
