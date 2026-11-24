"""Stage 7b (Kaggle): per-file family + signature metadata for the backtest.

    python scripts/export_metadata_kaggle.py --file-type Win64 --raw-dir <dir> --out-zip <zip>

Reads the official raw EMBER2024 zips (download them first with scripts/download_ember.py,
which verifies their SHA-256) and writes sentinel_stage7b_meta_<type>.zip: sha256, week,
label, family, family_confidence, num_certs, self_signed per file. No feature vectors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from sentinel.backtest.metadata import collect_metadata, write_metadata_zip
from sentinel.config import REPO_ROOT, load_data_config
from sentinel.data.pipeline import file_sha256
from sentinel.logging import configure_logging, get_logger
from sentinel.provenance import git_commit

log = get_logger(__name__)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file-type", required=True)
    p.add_argument("--raw-dir", type=Path, required=True)
    p.add_argument("--out-zip", type=Path, required=True)
    args = p.parse_args()
    configure_logging("INFO")
    ds = load_data_config().dataset
    t0 = time.time()
    df = collect_metadata(args.raw_dir, args.file_type, ds)
    zips = sorted(args.raw_dir.glob(f"{args.file_type}_*.zip"))
    mal = df[df["label"] == 1]
    info = {
        "file_type": args.file_type,
        "rows": len(df),
        "weeks": int(df["week"].nunique()),
        "malware_rows": len(mal),
        "malware_with_family": int(mal["family"].notna().sum()),
        "distinct_families": int(mal["family"].nunique()),
        "raw_zip_sha256": {z.name: file_sha256(z) for z in zips},
        "git_commit": git_commit(REPO_ROOT),
        "seconds": round(time.time() - t0, 1),
    }
    sha = write_metadata_zip(df, info, args.out_zip)
    log.info(
        "metadata_export_done",
        out=str(args.out_zip),
        sha256=sha,
        **{k: v for k, v in info.items() if k != "raw_zip_sha256"},
    )
    print(json.dumps({**info, "zip_sha256": sha}, indent=1))
    print("zip sha256", hashlib.sha256(args.out_zip.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
