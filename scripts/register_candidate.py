"""Stage 4 (local, sandbox): register a Kaggle candidate bundle under the `candidate` alias.

    python scripts/register_candidate.py --bundle /in/candidate.zip

Extracts only the expected files into $SENTINEL_DATA_DIR/models/<type>/candidate_T<week>_<policy>/,
registers a new version of sentinel-<type> and points the alias from configs/retrain.yaml at
it. The production alias is not touched.
"""

from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from sentinel.config import Settings, load_retrain_config, load_train_config
from sentinel.logging import configure_logging
from sentinel.models.registry import register_candidate
from sentinel.models.train import LGBM_CAL_FILE, LGBM_FILE, METRICS_FILE, RUN_INFO_FILE

CANDIDATE_FILES = {LGBM_FILE, LGBM_CAL_FILE, METRICS_FILE, RUN_INFO_FILE, "rebuild_check.json"}


def safe_extract(zip_path: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = PurePosixPath(info.filename).name
            if name not in CANDIDATE_FILES or ".." in PurePosixPath(info.filename).parts:
                raise SystemExit(f"unexpected file in candidate bundle: {info.filename}")
            (dest / name).write_bytes(zf.read(info))
    return dest


def main() -> None:
    settings = Settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", type=Path, required=True)
    args = p.parse_args()
    configure_logging(settings.log_level)
    tmp = safe_extract(args.bundle, settings.data_dir / "models" / "_incoming_candidate")
    info = json.loads((tmp / RUN_INFO_FILE).read_text(encoding="utf-8"))
    check = json.loads((tmp / "rebuild_check.json").read_text(encoding="utf-8"))
    if check.get("result") != "identical content":
        raise SystemExit(f"candidate was trained on data that failed the rebuild check: {check}")
    ft = info["file_type"]
    dest = (
        settings.data_dir / "models" / ft / f"candidate_T{info['windows']['as_of_week']}_"
        f"{info['policy']}"
    )
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(dest)
    reg = register_candidate(
        dest,
        settings.tracking_uri,
        settings.mlflow_dir / "artifacts",
        load_train_config(),
        load_retrain_config().mlflow_alias,
    )
    print(f"registered {reg['model_name']} version {reg['version']} as '{reg['alias']}'")


if __name__ == "__main__":
    main()
