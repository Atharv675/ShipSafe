"""Stage 7: pack the registered v1 bundle for Kaggle (the experiment's starting production).

    python scripts/pack_v1.py --file-type Win64     -> dist/sentinel_v1_Win64.zip

Allow-list: only the LightGBM model, its isotonic calibrator and the two JSON records (no
data, no logistic-regression files). Prints each file's sha256 so the upload can be checked.
"""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

from sentinel.config import REPO_ROOT, Settings
from sentinel.models.train import LGBM_CAL_FILE, LGBM_FILE, METRICS_FILE, RUN_INFO_FILE

FILES = (LGBM_FILE, LGBM_CAL_FILE, METRICS_FILE, RUN_INFO_FILE)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file-type", required=True)
    p.add_argument("--src", type=Path, default=None, help="default: <data>/models/<type>/v1")
    args = p.parse_args()
    src = args.src or Settings().data_dir / "models" / args.file_type / "v1"
    out = REPO_ROOT / "dist" / f"sentinel_v1_{args.file_type}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in FILES:
            data = (src / name).read_bytes()
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
            print(f"  + {name}  sha256 {hashlib.sha256(data).hexdigest()}")
    print(f"{out.relative_to(REPO_ROOT)}  sha256 {hashlib.sha256(out.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
