| t (s) | From | To | By | Reason |
|---|---|---|---|---|
| 0.0 | - | **APPROVED** | controller | DEMO approval: sentinel-win64/3 is tagged demo=true; the release gate was skipped for the Stage 6 demonstration |
| 0.6 | APPROVED | **RING0** | controller | approved: candidate starts in ring0 |
| 130.3 | RING0 | **RING1** | controller | ring0 healthy: [never_break] 0 of 500 never-break canaries flagged; [flag_rate] candidate flags 53.98% of 339 files in ring0 (CI 48.66-59.21%) vs production 46.75% of 8,685; [errors] 0 server errors in 343 candidate requests; [latency] p99 42.5 ms vs production 50.6 ms; [volume] 339 events in ring0 |
| 152.8 | RING1 | **ROLLED_BACK** | kill_switch | Stage 6 demo: operator kill switch during ring1 |
