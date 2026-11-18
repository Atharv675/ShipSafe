# CLAUDE.md
## Project
Sentinel Gate: decides whether a new malware model is safe to replace the current one, rolls it out ring by ring, and rolls back automatically.
Follow docs/BUILD_PLAN.md (Execution Protocol in Part A). Progress lives in docs/PROGRESS.md.
## Owner rules (override Part A)
- NO git or GitHub commands at all (init, commit, tag, remote, push, PR, gh). The owner manages git; at the end of each stage, suggest a commit message and tag instead.
- After every stage: summarise and WAIT for the owner's go-ahead before the next stage.
## Code rules
- Python 3.11, uv, type hints everywhere, mypy strict on src/.
- All metrics and statistics are pure functions in src/sentinel/models/evaluate.py. Services and dashboards import them; never re-implement.
- Config in configs/*.yaml via Pydantic settings. No magic numbers.
- Fixed seeds. Every result file records seed, data fingerprint and git commit.
- structlog JSON logging, no print in src/.
## Data rules
- Never use test weeks for training, tuning, thresholds or calibration.
- Time order is sacred: no future information anywhere.
- Every cleaning step logs row counts before and after.
- Dataset lives in $SENTINEL_DATA_DIR (outside OneDrive; see .env.example), never in git.
## Safety rules
- No secrets in git (.env + .env.example). No real malware: only joyce8/EMBER2024 feature records; never the EMBER2024-capa dataset (raw function bytes). Audit tables are append-only.
## Commands
make setup | lint | test | data | train | gate | up | down | dashboard | demo
