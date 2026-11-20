"""Stage 2 (local, in the sandbox): register a Kaggle run bundle in MLflow as v1 / production.

    python scripts/register_model.py --bundle /in/sentinel_stage2_Win64.zip

Extracts the bundle into $SENTINEL_DATA_DIR/models/<type>/, logs both models to MLflow
(params, metrics, feature_schema_version, data fingerprint), registers LightGBM as
sentinel-<type> and sets alias `production`, copies metrics.json + run_info.json to
docs/models/<type>/ and re-renders docs/BASELINE.md.
"""

from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from sentinel.config import REPO_ROOT, Settings, load_train_config
from sentinel.logging import configure_logging
from sentinel.models.baseline_report import build_baseline
from sentinel.models.registry import load_production, register_bundle
from sentinel.models.train import METRICS_FILE, RUN_INFO_FILE

BUNDLE_FILES = {
    "lightgbm.txt",
    "lightgbm_calibration.npz",
    "logreg.npz",
    "logreg_calibration.npz",
    METRICS_FILE,
    RUN_INFO_FILE,
    "rebuild_check.json",
}


def safe_extract(zip_path: Path, dest: Path) -> Path:
    """Extract only the expected bundle files (flat), refusing anything else."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = PurePosixPath(info.filename).name
            if name not in BUNDLE_FILES or ".." in PurePosixPath(info.filename).parts:
                raise SystemExit(f"unexpected file in bundle: {info.filename}")
            (dest / name).write_bytes(zf.read(info))
    return dest


def main() -> None:
    settings = Settings()
    cfg = load_train_config()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", type=Path, help="run bundle .zip or directory")
    p.add_argument("--models-docs", type=Path, default=REPO_ROOT / "docs" / "models")
    p.add_argument("--baseline", type=Path, default=REPO_ROOT / "docs" / "BASELINE.md")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()
    configure_logging(settings.log_level)

    if not args.report_only:
        if args.bundle is None:
            raise SystemExit("--bundle is required")
        if args.bundle.is_dir():
            src = args.bundle
            ft = json.loads((src / RUN_INFO_FILE).read_text(encoding="utf-8"))["file_type"]
            bundle_dir = settings.data_dir / "models" / ft / "v1"
            if src.resolve() != bundle_dir.resolve():
                shutil.rmtree(bundle_dir, ignore_errors=True)
                shutil.copytree(src, bundle_dir)
        else:
            tmp = safe_extract(args.bundle, settings.data_dir / "models" / "_incoming")
            ft = json.loads((tmp / RUN_INFO_FILE).read_text(encoding="utf-8"))["file_type"]
            bundle_dir = settings.data_dir / "models" / ft / "v1"
            shutil.rmtree(bundle_dir, ignore_errors=True)
            bundle_dir.parent.mkdir(parents=True, exist_ok=True)
            tmp.rename(bundle_dir)
        reg = register_bundle(
            bundle_dir, settings.tracking_uri, settings.mlflow_dir / "artifacts", cfg
        )
        model = load_production(settings.tracking_uri, cfg, ft)  # proves the alias resolves
        print(
            f"registered {reg['model_name']} version {reg['version']} as '{reg['alias']}' "
            f"(loads: {type(model).__name__})"
        )
        docs = args.models_docs / ft
        docs.mkdir(parents=True, exist_ok=True)
        for f in (METRICS_FILE, RUN_INFO_FILE):
            shutil.copy(bundle_dir / f, docs / f)
    done = build_baseline(args.models_docs, args.baseline)
    print(f"report: {args.baseline} ({', '.join(done) or 'no models'})")


if __name__ == "__main__":
    main()
