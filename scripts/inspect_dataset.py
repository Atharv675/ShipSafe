"""Measure the downloaded EMBER2024 feature records and write docs/DATASET_REPORT.md.

Per file type: rows per split and per week, malicious vs benign counts, feature count,
label values, missing values, date range. Reads zips in place (no extraction).

Usage:
    uv run python scripts/inspect_dataset.py [--file-types PDF ...] [--workers 6]
"""

from __future__ import annotations

import argparse
import json
import subprocess
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
    render_markdown,
    summarize_split,
)
from sentinel.logging import configure_logging, get_logger


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


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True, cwd=REPO_ROOT).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "uncommitted"


def main() -> None:
    cfg = load_data_config()
    settings = Settings()
    configure_logging(settings.log_level)
    log = get_logger("inspect_dataset")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file-types", nargs="+", default=cfg.dataset.file_types,
                    choices=cfg.dataset.file_types)
    ap.add_argument("--raw-dir", type=Path, default=settings.raw_dir)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--sample-per-week", type=int, default=100)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "DATASET_REPORT.md")
    args = ap.parse_args()

    manifest_path = args.raw_dir / "download_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    summaries: dict[str, FileTypeSummary] = {}
    for ft in args.file_types:
        zips = {sp: args.raw_dir / cfg.dataset.zip_name(ft, sp) for sp in cfg.dataset.splits}
        missing = [str(p) for p in zips.values() if not p.exists()]
        if missing:
            log.warning("skipping_file_type_missing_zips", file_type=ft, missing=missing)
            continue
        jobs = [(zp, w, cfg) for zp in zips.values()
                for w in list_week_files(zp, cfg.dataset.member_regex)]
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
        summary.flags = mismatch_flags(summary, cfg)
        summaries[ft] = summary
        log.info("inspected", file_type=ft,
                 **{f"{sp}_rows": s.rows for sp, s in summary.splits.items()},
                 test_benign=summary.splits["test"].benign, flags=len(summary.flags))

    provenance = [
        f"HuggingFace dataset `{cfg.dataset.hf_repo_id}` (official per "
        "github.com/FutureComputing4AI/EMBER2024 `thrember/download.py` @ 0ef753e)",
        f"Feature vectors: `thrember.PEFeatureExtractor` (EMBER feature v3), "
        f"train sample of {args.sample_per_week} rows/week",
        f"Repo commit: `{_git_commit()}`",
        *[f"`{n}` sha256 `{m['sha256']}`" for n, m in sorted(manifest.items())],
    ]
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    args.out.write_text(
        render_markdown(summaries, cfg, generated_at=generated, provenance=provenance),
        encoding="utf-8",
    )
    log.info("report_written", path=str(args.out), file_types=list(summaries))


if __name__ == "__main__":
    main()
