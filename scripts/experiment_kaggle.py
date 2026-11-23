"""Stage 7 (heavy part, runs on Kaggle): retrain the experiment's candidates, export scores.

    python scripts/experiment_kaggle.py --v1-dir /kaggle/input/.../v1 --work /tmp/exp \
        --out-zip /kaggle/working/sentinel_stage7_Win64_all.zip [--policies all_history ...]

configs/experiment.yaml says which weeks T and which retraining policies; `--policies` limits
one run to some policies (split the work over several Kaggle sessions). Checks the rebuilt
splits against docs/cleaning/<type>.json first. Exports scores only, never feature vectors.
Locally: `make experiment-import ZIP=<zip>` then `make experiment`.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sentinel.config import (
    REPO_ROOT,
    Settings,
    load_experiment_config,
    load_retrain_config,
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
    train_candidates,
    write_export_zip,
)
from sentinel.logging import configure_logging, get_logger

log = get_logger(__name__)


def main() -> None:
    settings = Settings()
    exp = load_experiment_config()
    train = load_train_config()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file-type", default=exp.file_type)
    p.add_argument("--splits-dir", type=Path, default=settings.splits_dir)
    p.add_argument("--clean-dir", type=Path, default=settings.clean_dir)
    p.add_argument("--v1-dir", type=Path, required=True, help="the Stage 2 v1 bundle folder")
    p.add_argument("--work", type=Path, required=True, help="candidate bundles (scratch)")
    p.add_argument("--out-zip", type=Path, required=True)
    p.add_argument("--policies", nargs="+", choices=exp.retrain_policies)
    p.add_argument("--threads", type=int, default=os.cpu_count() or 0)
    args = p.parse_args()
    configure_logging(settings.log_level)
    ft = args.file_type
    if ft != exp.file_type:
        raise SystemExit(f"configs/experiment.yaml is for {exp.file_type}, not {ft}")

    check = check_rebuild(
        args.splits_dir, ft, REPO_ROOT / "docs" / "cleaning" / f"{ft}.json", REPO_ROOT
    )
    manifest = load_manifest(args.splits_dir, ft)
    v1 = load_bundle(V1_ID, args.v1_dir, as_of_week=train.weeks.as_of_week)
    if v1.info["feature_schema_version"] != manifest["feature_schema_version"]:
        raise SystemExit("the v1 bundle was trained on a different feature schema")
    never_break = args.clean_dir / ft / "never_break.parquet"
    candidates = train_candidates(
        ft,
        args.splits_dir,
        never_break,
        args.work,
        train,
        load_retrain_config(),
        exp,
        args.policies or list(exp.retrain_policies),
        threads=args.threads,
    )
    out_dir = args.work / "export"
    info = export_scores(
        ft, args.splits_dir, never_break, out_dir, [v1, *candidates], exp, train.seed
    )
    feats = top_features(v1.booster, feature_columns(manifest), exp.drift.top_features)
    hist = feature_histograms(args.splits_dir, ft, feats, v1.info["fit_weeks"], exp.drift.bins)
    (out_dir / HIST_FILE).write_text(json.dumps(hist) + "\n", encoding="utf-8")
    info_path = out_dir / "export_info.json"
    full = json.loads(info_path.read_text(encoding="utf-8")) | {"rebuild_check": check}
    info_path.write_text(json.dumps(full, indent=1) + "\n", encoding="utf-8")
    sums = write_export_zip(out_dir, args.out_zip)
    log.info("experiment_export_written", zip=str(args.out_zip), models=info["models"], sha256=sums)
    for c in candidates:
        i = c.info
        print(
            f"{c.model_id}: threshold {i['threshold_raw']:.7f}, fit {i['fit_rows']:,} rows "
            f"(weeks {i['fit_weeks'][0]}-{i['fit_weeks'][-1]}), never-break flags "
            f"{i['never_break_flags_at_training']}, {i['seconds'].get('total', '?')} s"
        )


if __name__ == "__main__":
    main()
