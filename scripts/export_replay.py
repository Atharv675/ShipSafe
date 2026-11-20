"""Stage 4 (runs on Kaggle): export rebuilt, VERIFIED weekly splits for local replay.

    python scripts/export_replay.py --file-type Win64 --out /kaggle/working/x.zip

Checks the rebuilt splits against docs/cleaning/<type>.json (content), then writes a zip with
manifest.json, the chosen week files (default: every TEST week), never_break.parquet and
export_info.json (sha256 of each file). Locally `make import-replay ZIP=...` verifies again.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sentinel.config import REPO_ROOT, Settings
from sentinel.data.splits import check_rebuild, load_manifest, weeks_in_split
from sentinel.logging import configure_logging
from sentinel.provenance import git_commit
from sentinel.replay.transfer import export_weeks


def main() -> None:
    settings = Settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file-type", required=True)
    p.add_argument("--splits-dir", type=Path, default=settings.splits_dir)
    p.add_argument("--clean-dir", type=Path, default=settings.clean_dir)
    p.add_argument("--weeks", type=int, nargs="*", help="default: all test weeks")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    configure_logging(settings.log_level)
    ft = args.file_type
    check = check_rebuild(
        args.splits_dir, ft, REPO_ROOT / "docs" / "cleaning" / f"{ft}.json", REPO_ROOT
    )
    print(f"{ft}: rebuilt splits match docs/cleaning/{ft}.json (content)")
    weeks = sorted(args.weeks or weeks_in_split(load_manifest(args.splits_dir, ft), "test"))
    export_weeks(
        args.splits_dir,
        args.clean_dir,
        ft,
        weeks,
        args.out,
        {"rebuild_check": check, "git_commit": git_commit(REPO_ROOT)},
    )
    size = args.out.stat().st_size / 1e6
    print(f"exported weeks {weeks[0]}-{weeks[-1]} to {args.out} ({size:.0f} MB)")


if __name__ == "__main__":
    main()
