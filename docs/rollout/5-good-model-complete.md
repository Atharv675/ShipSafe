| t (s) | From | To | By | Reason |
|---|---|---|---|---|
| 0.0 | - | **APPROVED** | controller | DEMO approval: sentinel-win32/3 is tagged demo=true; the release gate was skipped for the Stage 6 demonstration |
| 0.3 | APPROVED | **RING0** | controller | approved: candidate starts in ring0 |
| 167.1 | RING0 | **RING1** | controller | ring0 healthy: [never_break] 0 of 500 never-break canaries flagged; [flag_rate] candidate flags 32.85% of 344 files in ring0 (CI 28.10-37.98%) vs production 33.76% of 6,081; [errors] 0 server errors in 351 candidate requests; [latency] p99 54.3 ms vs production 78.0 ms; [volume] 344 events in ring0 |
| 261.7 | RING1 | **RING2** | controller | ring1 healthy: [never_break] 0 of 500 never-break canaries flagged; [flag_rate] candidate flags 40.84% of 1,589 files in ring1 (CI 38.45-43.28%) vs production 40.67% of 4,859; [errors] 0 server errors in 2,295 candidate requests; [latency] p99 48.6 ms vs production 58.7 ms; [volume] 1,589 events in ring1 |
| 325.4 | RING2 | **COMPLETE** | controller | ring2 healthy: [never_break] 0 of 500 never-break canaries flagged; [flag_rate] not compared in ring2: no production traffic runs alongside the last ring (compared on the same period in the earlier rings); [errors] 0 server errors in 7,148 candidate requests; [latency] p99 45.7 ms vs production 57.2 ms; [volume] 3,174 events in ring2 |
