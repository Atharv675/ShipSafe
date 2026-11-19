"""Measure the downloaded EMBER2024 feature records and write docs/DATASET_REPORT.md.

Per file type: rows per split and per week, malicious vs benign counts, feature count,
label values, missing values, date range. Reads zips in place (no extraction).

Each inspected file type is saved as docs/dataset_stats/<file_type>.json (counts only, no file
ids); the report is then rebuilt from ALL json files there, so stats measured elsewhere
(e.g. Kaggle/Colab) merge in by dropping their json into that folder.

Usage:
    make inspect FILE_TYPES="PDF ELF"
    python scripts/inspect_dataset.py --report-only      # just rebuild the report
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

import numpy as np

from sentinel.config import REPO_ROOT, DataConfig, Settings, load_data_config
from sentinel.data.ember import (
    WeekFile,
    WeekStats,
    iter_member_records,
    list_week_files,
    summarize_records,
)
from sentinel.data.report import (
    FileTypeSummary,
    mismatch_flags,
    summarize_split,
    train_test_overlap,
)
from sentinel.data.report_io import build_report, write_summary
from sentinel.logging import configure_logging, get_logger
from sentinel.provenance import git_commit


def _inspect_member(args: tuple[Path, WeekFile, DataConfig]) -> WeekStats:
    zip_path, week, cfg = args
    ds = cfg.dataset
    return summarize_records(
        iter_member_records(zip_path, week.name),
        week,
        label_column=ds.label_column,
        time_column=ds.time_column,
        metadata_columns=ds.metadata_columns,
        feature_blocks=ds.feature_blocks,
        id_column=ds.id_column,
        dup_ignored_columns=ds.dup_ignored_columns,
    )


def _feature_sample(zip_path: Path, weeks: list[WeekFile], per_week: int) -> tuple[int, int, int]:
    """Vectorise a small sample with the official extractor: (dim, non-constant cols, rows)."""
    import thrember  # heavy import, only here

    extractor = thrember.PEFeatureExtractor()
    rows = [
        extractor.process_raw_features(rec)
        for w in weeks
        for rec in islice(iter_member_records(zip_path, w.name), per_week)
    ]
    x = np.vstack(rows)
    nonconstant = int((x.max(axis=0) != x.min(axis=0)).sum())
    return int(extractor.dim), nonconstant, int(x.shape[0])


def main() -> None:
    cfg = load_data_config()
    settings = Settings()
    configure_logging(settings.log_level)
    log = get_logger("inspect_dataset")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--file-types", nargs="+", default=cfg.dataset.file_types, choices=cfg.dataset.file_types
    )
    ap.add_argument("--raw-dir", type=Path, default=settings.raw_dir)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--sample-per-week", type=int, default=100)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "DATASET_REPORT.md")
    ap.add_argument(
        "--stats-dir",
        type=Path,
        default=REPO_ROOT / "docs" / "dataset_stats",
        help="one <file_type>.json per inspected type; the report merges all files here",
    )
    ap.add_argument("--where", default="local hardened Docker sandbox", help="provenance label")
    ap.add_argument("--report-only", action="store_true", help="skip measuring; rebuild report")
    args = ap.parse_args()
    if args.report_only:
        args.file_types = []

    manifest_path = args.raw_dir / "download_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    commit = git_commit(REPO_ROOT)

    for ft in args.file_types:
        zips = {sp: args.raw_dir / cfg.dataset.zip_name(ft, sp) for sp in cfg.dataset.splits}
        missing = [str(p) for p in zips.values() if not p.exists()]
        if missing:
            log.warning("skipping_file_type_missing_zips", file_type=ft, missing=missing)
            continue
        jobs = [
            (zp, w, cfg)
            for zp in zips.values()
            for w in list_week_files(zp, cfg.dataset.member_regex)
        ]
        log.info("inspecting", file_type=ft, weekly_files=len(jobs))
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            weekly = list(pool.map(_inspect_member, jobs))
        by_split = {sp: [w for w in weekly if w.week.split == sp] for sp in cfg.dataset.splits}
        summary = FileTypeSummary(
            file_type=ft,
            splits={sp: summarize_split(ft, sp, ws) for sp, ws in by_split.items()},
            weekly=weekly,
        )
        train_weeks = list_week_files(zips["train"], cfg.dataset.member_regex)
        dim, nonconst, nrows = _feature_sample(zips["train"], train_weeks, args.sample_per_week)
        summary.feature_dim, summary.nonconstant_features_in_sample = dim, nonconst
        summary.sample_rows = nrows
        summary.train_test_overlap = train_test_overlap(weekly)
        summary.flags = mismatch_flags(summary, cfg)
        zip_names = {cfg.dataset.zip_name(ft, sp) for sp in cfg.dataset.splits}
        provenance = [
            f"measured in: {args.where}; repo commit `{commit}`; feature sample "
            f"{args.sample_per_week} train rows/week",
            *[
                f"`{n}` sha256 `{m['sha256']}`"
                for n, m in sorted(manifest.items())
                if n in zip_names
            ],
        ]
        stats_path = args.stats_dir / f"{ft}.json"
        write_summary(
            stats_path,
            summary,
            provenance=provenance,
            generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        )
        log.info(
            "inspected",
            file_type=ft,
            **{f"{sp}_rows": s.rows for sp, s in summary.splits.items()},
            test_benign=summary.splits["test"].benign,
            flags=len(summary.flags),
            stats=str(stats_path),
        )

    included = build_report(args.stats_dir, args.out, cfg)
    log.info("report_written", path=str(args.out), file_types=included)


if __name__ == "__main__":
    main()
