"""Build real /v1/score request bodies from local EMBER2024 feature records (no binaries).

    python scripts/make_sample_request.py --file-type Win64 --source Dot_Net

Win64/Win32 records are not kept locally (they are rebuilt on Kaggle), so this takes records
from another PE file type that IS local (Dot_Net uses the same official PE feature extractor),
vectorizes them with thrember exactly like Stage 1, applies the Stage 1 cleaning for the
target model (keep its columns in its order, non-finite -> fill value, float32) and writes
request JSON files to $SENTINEL_DATA_DIR/samples/<file type>/. These files come from a
different population than the model was trained on: use them to exercise the API, not to
judge accuracy.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from sentinel.config import REPO_ROOT, Settings, load_clean_config, load_data_config
from sentinel.data.ember import iter_member_records, list_week_files
from sentinel.data.pipeline import thrember_vectorize
from sentinel.logging import configure_logging


def main() -> None:
    settings = Settings()
    data_cfg = load_data_config()
    clean_cfg = load_clean_config()
    ds = data_cfg.dataset
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file-type", default="Win64", help="model whose schema to target")
    p.add_argument("--source", default="Dot_Net", help="local PE file type to take records from")
    p.add_argument("--per-label", type=int, default=3, help="benign and malicious records each")
    p.add_argument("--tenant", default="tenant-demo")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()
    configure_logging(settings.log_level)

    run_info = json.loads(
        (REPO_ROOT / "docs" / "models" / args.file_type / "run_info.json").read_text("utf-8")
    )
    features: list[str] = run_info["features"]
    idx = np.array([int(f[len(clean_cfg.feature_prefix) :]) for f in features])
    zip_path = settings.raw_dir / ds.zip_name(args.source, "test")
    member = list_week_files(zip_path, ds.member_regex)[0].name
    out = args.out or settings.data_dir / "samples" / args.file_type
    out.mkdir(parents=True, exist_ok=True)

    want = {0: args.per_label, 1: args.per_label}
    written = []
    for rec in iter_member_records(zip_path, member):
        label = rec.get(ds.label_column)
        if label not in want or want[label] == 0:
            continue
        vec = np.asarray(thrember_vectorize(rec), dtype=np.float32)[idx]
        vec[~np.isfinite(vec)] = np.float32(clean_cfg.non_finite_fill)
        body = {
            "file_id": str(rec[ds.id_column]),
            "tenant_id": args.tenant,
            "features": [float(v) for v in vec],
            "feature_schema_version": run_info["feature_schema_version"],
        }
        name = f"{'malicious' if label == 1 else 'benign'}_{want[label]}.json"
        (out / name).write_text(json.dumps(body), encoding="utf-8")
        written.append({"file": name, "file_id": body["file_id"], "dataset_label": label})
        want[label] -= 1
        if not any(want.values()):
            break
    (out / "index.json").write_text(
        json.dumps({"source": f"{args.source} test, {member}", "requests": written}, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {len(written)} request bodies to {out} ({len(features)} features each)")


if __name__ == "__main__":
    main()
