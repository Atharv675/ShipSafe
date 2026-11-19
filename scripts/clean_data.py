"""Stage 1: clean EMBER2024 file types into validated weekly Parquet splits + manifest.json.

    python scripts/clean_data.py --file-types Win64 Win32 --workers 2

Writes $SENTINEL_DATA_DIR/splits/<type>/week=NN/part.parquet, .../manifest.json and
$SENTINEL_DATA_DIR/clean/<type>/never_break.parquet; copies each manifest to --manifest-dir
and renders --report from all manifests there.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from sentinel.config import REPO_ROOT, Settings, load_clean_config, load_data_config
from sentinel.data.clean_report import build_cleaning_report
from sentinel.data.pipeline import Layout, clean_file_type
from sentinel.logging import configure_logging


def main() -> None:
    settings = Settings()
    cfg = load_clean_config()
    data_cfg = load_data_config()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file-types", nargs="+", default=cfg.file_types)
    p.add_argument("--raw-dir", type=Path, default=settings.raw_dir)
    p.add_argument("--splits-dir", type=Path, default=settings.splits_dir)
    p.add_argument("--clean-dir", type=Path, default=settings.clean_dir)
    p.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    p.add_argument("--manifest-dir", type=Path, default=REPO_ROOT / "docs" / "cleaning")
    p.add_argument("--report", type=Path, default=REPO_ROOT / "docs" / "CLEANING_REPORT.md")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()
    configure_logging(settings.log_level)

    unknown = set(args.file_types) - set(data_cfg.dataset.file_types)
    if unknown:
        raise SystemExit(f"unknown file types: {sorted(unknown)}")
    args.manifest_dir.mkdir(parents=True, exist_ok=True)
    if not args.report_only:
        for ft in args.file_types:
            layout = Layout(args.splits_dir, args.clean_dir, ft)
            manifest = clean_file_type(
                ft, args.raw_dir, layout, data_cfg, cfg, workers=args.workers
            )
            shutil.copy(layout.manifest, args.manifest_dir / f"{ft}.json")
            t = manifest["totals"]
            print(
                f"{ft}: train {t['train']['rows']:,} rows, test {t['test']['rows']:,} rows, "
                f"{manifest['feature_columns']} features, "
                f"feature_schema_version {manifest['feature_schema_version']}"
            )
    done = build_cleaning_report(args.manifest_dir, args.report, data_cfg.dataset.file_types)
    print(f"report: {args.report} ({', '.join(done) or 'no manifests'})")


if __name__ == "__main__":
    main()
