"""Stage 4 (heavy part, runs on Kaggle): retrain a CANDIDATE at simulated time T.

    python scripts/retrain_candidate.py --file-type Win64 --out /tmp/cand_Win64

Uses configs/retrain.yaml (T, policy) and configs/train.yaml (LightGBM params, target FPR).
Checks the rebuilt splits against docs/cleaning/<type>.json first. Locally:
`make register-candidate BUNDLE=<zip>` registers it under the `candidate` alias.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sentinel.config import REPO_ROOT, Settings, load_retrain_config, load_train_config
from sentinel.data.splits import check_rebuild
from sentinel.logging import configure_logging
from sentinel.models.retrain import retrain_candidate


def main() -> None:
    settings = Settings()
    cfg = load_retrain_config()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file-type", required=True)
    p.add_argument("--splits-dir", type=Path, default=settings.splits_dir)
    p.add_argument("--clean-dir", type=Path, default=settings.clean_dir)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--policy", choices=["all_history", "last_n_weeks", "recency_weighted"])
    p.add_argument("--as-of-week", type=int, default=None)
    p.add_argument("--threads", type=int, default=os.cpu_count() or 0)
    args = p.parse_args()
    configure_logging(settings.log_level)
    ft = args.file_type

    check = check_rebuild(
        args.splits_dir, ft, REPO_ROOT / "docs" / "cleaning" / f"{ft}.json", REPO_ROOT
    )
    result = retrain_candidate(
        ft,
        args.splits_dir,
        args.clean_dir / ft / "never_break.parquet",
        args.out,
        load_train_config(),
        cfg,
        policy=args.policy,
        as_of_week=args.as_of_week,
        lgbm_threads=args.threads,
    )
    (args.out / "rebuild_check.json").write_text(json.dumps(check, indent=1) + "\n", "utf-8")
    m = result["models"]["lightgbm"]
    c = m["calibration"]
    print(
        f"{ft} candidate: threshold {m['threshold_raw']:.6f}, calibration-week recall "
        f"{c['recall']:.4f} at FPR {c['fpr']:.5f}, never-break flags {m['never_break_flags']}"
    )


if __name__ == "__main__":
    main()
