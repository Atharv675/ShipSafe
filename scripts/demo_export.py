"""Stage 10 (`make demo`, sandbox): score the demo's two models for the ML pages.

    python scripts/demo_export.py --v1-dir /data/runs/v1 --candidate-dir /data/runs/candidate

Writes the same export the Kaggle experiment produces (scores + labels of every scored week,
never-break scores, models.json, feature histograms of v1's top features) for v1 and the ONE
registered demo candidate, into $SENTINEL_DATA_DIR/backtest/<type>/candidates/demo/, where
`make ml-results` reads it. Nothing is trained here; the rebuilt splits are checked against
the committed manifest first (docs/cleaning/<type>.json).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sentinel.config import (
    REPO_ROOT,
    Settings,
    candidate_id,
    load_experiment_config,
    load_train_config,
)
from sentinel.data.splits import check_rebuild, feature_columns, load_manifest
from sentinel.experiment.export import (
    HIST_FILE,
    V1_ID,
    export_scores,
    feature_histograms,
    load_bundle,
    top_features,
)
from sentinel.logging import configure_logging, get_logger

log = get_logger(__name__)


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    exp = load_experiment_config()
    train = load_train_config()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--v1-dir", type=Path, required=True)
    p.add_argument("--candidate-dir", type=Path, required=True)
    p.add_argument("--policy", default="last_n_weeks")
    args = p.parse_args()
    ft = exp.file_type
    check = check_rebuild(
        settings.splits_dir, ft, REPO_ROOT / "docs" / "cleaning" / f"{ft}.json", REPO_ROOT
    )
    manifest = load_manifest(settings.splits_dir, ft)
    v1 = load_bundle(V1_ID, args.v1_dir, as_of_week=train.weeks.as_of_week)
    cand = load_bundle(candidate_id(args.policy, exp.retrain_weeks[0]), args.candidate_dir)
    out = settings.data_dir / "backtest" / ft / "candidates" / "demo"
    info = export_scores(
        ft,
        settings.splits_dir,
        settings.clean_dir / ft / "never_break.parquet",
        out,
        [v1, cand],
        exp,
        train.seed,
    )
    feats = top_features(v1.booster, feature_columns(manifest), exp.drift.top_features)
    hist = feature_histograms(settings.splits_dir, ft, feats, v1.info["fit_weeks"], exp.drift.bins)
    (out / HIST_FILE).write_text(json.dumps(hist) + "\n", encoding="utf-8")
    full = json.loads((out / "export_info.json").read_text("utf-8")) | {"rebuild_check": check}
    (out / "export_info.json").write_text(json.dumps(full, indent=1) + "\n", "utf-8")
    log.info("demo_export_written", out=str(out), models=info["models"], rows=info["rows"])


if __name__ == "__main__":
    main()
