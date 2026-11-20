"""Stage 2 (heavy part, runs on Kaggle): train v1 + LR baseline for ONE dataset into a run bundle.

    python scripts/train_baseline.py --file-type Win64 --out /tmp/run_Win64

Before training it checks that the rebuilt splits have the same CONTENT as the Stage 1
manifest committed in docs/cleaning/<type>.json (row counts per step and per week, dropped
columns, feature_schema_version, data fingerprint). Any difference stops the run.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sentinel.config import REPO_ROOT, Settings, load_train_config
from sentinel.data.splits import compare_manifests, load_manifest
from sentinel.logging import configure_logging
from sentinel.models.train import train_baseline


def main() -> None:
    settings = Settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file-type", required=True)
    p.add_argument("--splits-dir", type=Path, default=settings.splits_dir)
    p.add_argument("--clean-dir", type=Path, default=settings.clean_dir)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--expected-manifest", type=Path, default=None, help="default: docs/cleaning/<type>.json"
    )
    p.add_argument(
        "--skip-manifest-check",
        action="store_true",
        help="only for local smoke runs on types without a committed manifest",
    )
    p.add_argument("--threads", type=int, default=os.cpu_count() or 0)
    args = p.parse_args()
    configure_logging(settings.log_level)
    ft = args.file_type

    actual = load_manifest(args.splits_dir, ft)
    check: dict[str, object] = {"checked": False}
    if not args.skip_manifest_check:
        expected_path = args.expected_manifest or REPO_ROOT / "docs" / "cleaning" / f"{ft}.json"
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        diffs = compare_manifests(expected, actual)
        if diffs:
            raise SystemExit(f"rebuilt {ft} splits differ from {expected_path}: {diffs}")
        check = {
            "checked": True,
            "expected": str(expected_path.relative_to(REPO_ROOT))
            if expected_path.is_relative_to(REPO_ROOT)
            else str(expected_path),
            "result": "identical content",
        }
        print(f"{ft}: rebuilt splits match {expected_path.name} (content)")

    result = train_baseline(
        ft,
        args.splits_dir,
        args.clean_dir / ft / "never_break.parquet",
        args.out,
        load_train_config(),
        lgbm_threads=args.threads,
    )
    (args.out / "rebuild_check.json").write_text(
        json.dumps(check, indent=1) + "\n", encoding="utf-8"
    )
    for name, m in result["models"].items():
        e = m["eval_week"]
        print(
            f"{ft} {name}: eval week {e['week']} recall {e['recall']:.4f} at FPR {e['fpr']:.5f} "
            f"(target {result['target_fpr']}), AUC {e['auc']:.4f}, never-break flags "
            f"{m['never_break_flags']}"
        )


if __name__ == "__main__":
    main()
