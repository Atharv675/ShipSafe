| t (s) | From | To | By | Reason |
|---|---|---|---|---|
| 0.0 | - | **APPROVED** | controller | DEMO approval: sentinel-win64/5 is tagged demo=true; the release gate was skipped for the Stage 6 demonstration; never-break canaries OFF for this demo (paired-scoring signal alone) |
| 1.6 | APPROVED | **RING0** | controller | approved: candidate starts in ring0 |
| 59.8 | RING0 | **ROLLED_BACK** | controller | Rolled back in ring0: [disagreement] candidate disagrees with production on the same files far more than approved: flags that production clears: 4.55% of 66 (CI 1.56-12.53%, limit 1.00%) (expected from demo copy of sentinel-win64/1) |
