| t (s) | From | To | By | Reason |
|---|---|---|---|---|
| 0.0 | - | **APPROVED** | controller | DEMO approval: sentinel-win64/3 is tagged demo=true; the release gate was skipped for the Stage 6 demonstration |
| 0.2 | APPROVED | **RING0** | controller | approved: candidate starts in ring0 |
| 166.4 | RING0 | **RING1** | controller | ring0 healthy: [never_break] 0 of 500 never-break canaries flagged; [flag_rate] candidate flags 42.47% of 332 files in ring0 (CI 37.27-47.84%) vs production 42.87% of 6,079; [errors] 0 server errors in 837 candidate requests; [latency] p99 54.1 ms vs production 47.8 ms; [volume] 332 events in ring0 |
| 220.3 | RING1 | **ROLLED_BACK** | controller | Rolled back in ring1: [flag_rate] candidate flags 59.38% of 448 files in ring1 (CI 54.77-63.82%) vs production 46.91% of 9,591: clearly above production's +10% limit |
