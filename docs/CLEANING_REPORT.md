# Cleaning report — Stage 1

_Generated from `manifest.json` files written by `scripts/clean_data.py`. Every count below is measured._

| File type | Train rows | Test rows | Train benign | Test benign | Features kept | Never-break | feature_schema_version |
|---|---:|---:|---:|---:|---:|---:|---|
| Win32 | 1,559,966 | 359,986 | 779,968 | 179,987 | 2,536 / 2,568 | 500 | `6bad8af2adf3d418` |
| Win64 | 519,991 | 119,990 | 259,994 | 59,990 | 2,538 / 2,568 | 500 | `b9521b9b93166b91` |

## Win32

- generated 2026-11-19T14:47:01+00:00; seed `20241122`; git commit `63babe65a67d0b9b9fdc97c22700fdf1113ab472 (code bundle)`
- data fingerprint `2b87c753b815c9035446bc017db598d3c9184acb096fceaa20f1957fbc3527d4` (sha256 over raw zip hashes: `Win32_test.zip` c05f6562dee3…, `Win32_train.zip` 6cf9dea84796…)

| Step | Split | Unit | Before | After | Removed | Note |
|---|---|---|---:|---:|---:|---|
| drop_missing_id | train | rows | 3,120,000 | 3,120,000 | 0 |  |
| drop_repeated_id_within_week | train | rows | 3,120,000 | 1,560,000 | 1,560,000 |  |
| drop_bad_label | train | rows | 1,560,000 | 1,560,000 | 0 |  |
| drop_bad_timestamp | train | rows | 1,560,000 | 1,560,000 | 0 |  |
| fill_non_finite | train | rows | 1,560,000 | 1,560,000 | 0 | 0 values replaced |
| keep_outside_week_timestamps | train | rows | 1,560,000 | 1,560,000 | 0 | 0 rows dated outside their weekly file (kept) |
| drop_missing_id | test | rows | 720,000 | 720,000 | 0 |  |
| drop_repeated_id_within_week | test | rows | 720,000 | 360,000 | 360,000 |  |
| drop_bad_label | test | rows | 360,000 | 360,000 | 0 |  |
| drop_bad_timestamp | test | rows | 360,000 | 360,000 | 0 |  |
| fill_non_finite | test | rows | 360,000 | 360,000 | 0 | 0 values replaced |
| keep_outside_week_timestamps | test | rows | 360,000 | 360,000 | 0 | 23 rows dated outside their weekly file (kept) |
| drop_cross_week_repeat | train | rows | 1,560,000 | 1,560,000 | 0 |  |
| drop_duplicate_vector | train | rows | 1,560,000 | 1,559,966 | 34 | 0 dropped copies had a different label |
| drop_cross_week_repeat | test | rows | 360,000 | 359,994 | 6 |  |
| drop_test_id_in_train | test | rows | 359,994 | 359,994 | 0 |  |
| drop_test_vector_in_train | test | rows | 359,994 | 359,989 | 5 |  |
| drop_duplicate_vector | test | rows | 359,989 | 359,986 | 3 | 0 dropped copies had a different label |
| drop_constant_columns | train | columns | 2,568 | 2,536 | 32 | constant over all TRAIN rows; dropped from every split |
| cast_float32 | all | columns | 2,536 | 2,536 | 0 | float32 |

## Win64

- generated 2026-11-19T14:10:52+00:00; seed `20241122`; git commit `63babe65a67d0b9b9fdc97c22700fdf1113ab472 (code bundle)`
- data fingerprint `c824866c3eb2b871da9b6f2ae8c5a1da0742e18d5bd1281e509f4bdea6225d5d` (sha256 over raw zip hashes: `Win64_test.zip` 52a5a05c1bfa…, `Win64_train.zip` 7b050100ce7b…)

| Step | Split | Unit | Before | After | Removed | Note |
|---|---|---|---:|---:|---:|---|
| drop_missing_id | train | rows | 1,040,000 | 1,040,000 | 0 |  |
| drop_repeated_id_within_week | train | rows | 1,040,000 | 520,000 | 520,000 |  |
| drop_bad_label | train | rows | 520,000 | 520,000 | 0 |  |
| drop_bad_timestamp | train | rows | 520,000 | 520,000 | 0 |  |
| fill_non_finite | train | rows | 520,000 | 520,000 | 0 | 0 values replaced |
| keep_outside_week_timestamps | train | rows | 520,000 | 520,000 | 0 | 0 rows dated outside their weekly file (kept) |
| drop_missing_id | test | rows | 240,000 | 240,000 | 0 |  |
| drop_repeated_id_within_week | test | rows | 240,000 | 120,000 | 120,000 |  |
| drop_bad_label | test | rows | 120,000 | 120,000 | 0 |  |
| drop_bad_timestamp | test | rows | 120,000 | 120,000 | 0 |  |
| fill_non_finite | test | rows | 120,000 | 120,000 | 0 | 0 values replaced |
| keep_outside_week_timestamps | test | rows | 120,000 | 120,000 | 0 | 13 rows dated outside their weekly file (kept) |
| drop_cross_week_repeat | train | rows | 520,000 | 520,000 | 0 |  |
| drop_duplicate_vector | train | rows | 520,000 | 519,991 | 9 | 1 dropped copies had a different label |
| drop_cross_week_repeat | test | rows | 120,000 | 119,993 | 7 |  |
| drop_test_id_in_train | test | rows | 119,993 | 119,993 | 0 |  |
| drop_test_vector_in_train | test | rows | 119,993 | 119,990 | 3 |  |
| drop_duplicate_vector | test | rows | 119,990 | 119,990 | 0 | 0 dropped copies had a different label |
| drop_constant_columns | train | columns | 2,568 | 2,538 | 30 | constant over all TRAIN rows; dropped from every split |
| cast_float32 | all | columns | 2,538 | 2,538 | 0 | float32 |
