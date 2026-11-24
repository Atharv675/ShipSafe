"""Stage 7b (local, sandbox): import the Kaggle exports and run Pass 1 of the backtest.

    python scripts/run_backtest.py import-meta --zip /in/sentinel_stage7b_meta_Win64.zip
    python scripts/run_backtest.py import --zip /in/sentinel_stage7b_Win64_<policy>.zip
    python scripts/run_backtest.py pass1 [--resamples N]

`pass1` writes $SENTINEL_DATA_DIR/results/backtest_pass1_*.parquet (seed, data fingerprint
and git commit in their metadata) and the auto section of docs/BACKTEST.md. It refuses any
candidate that learned from weeks after T-6 (owner decision 2026-11-24).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from sentinel.backtest.metadata import import_metadata_zip
from sentinel.backtest.pass1 import rejudge, run_pass1, summary_table
from sentinel.backtest.report import write_pass1_md
from sentinel.config import REPO_ROOT, Settings, load_backtest_config, load_gate_policy
from sentinel.experiment.export import import_export_zip
from sentinel.experiment.scores import load_score_table
from sentinel.logging import configure_logging, get_logger
from sentinel.provenance import git_commit

log = get_logger(__name__)


def _write(df: pd.DataFrame, path: Path, meta: dict[str, Any]) -> None:
    t = pa.Table.from_pandas(df, preserve_index=False)
    t = t.replace_schema_metadata(
        {**(t.schema.metadata or {}), b"sentinel": json.dumps(meta).encode("utf-8")}
    )
    pq.write_table(t, path, compression="zstd")


def _dirs(root: Path) -> list[Path]:
    return sorted(d for d in root.iterdir() if d.is_dir()) if root.exists() else []


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_backtest_config()
    gate = load_gate_policy().model_copy(update={"operating_fpr": cfg.operating_fpr})
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import-meta").add_argument("--zip", type=Path, required=True)
    sub.add_parser("import").add_argument("--zip", type=Path, required=True)
    r = sub.add_parser("pass1")
    r.add_argument("--resamples", type=int, default=None)
    args = p.parse_args()
    base = settings.data_dir / "backtest" / cfg.file_type
    meta_dir = base / "meta"
    cand_root = base / "candidates"

    if args.cmd == "import-meta":
        df = import_metadata_zip(args.zip, meta_dir)
        log.info("backtest_metadata_imported", rows=len(df), weeks=int(df["week"].nunique()))
        return
    if args.cmd == "import":
        dest = cand_root / args.zip.stem
        info = import_export_zip(args.zip, dest)
        if info["file_type"] != cfg.file_type:
            raise SystemExit(f"export is for {info['file_type']}, backtest for {cfg.file_type}")
        if info.get("label_delay_weeks") != cfg.label_delay_weeks:
            raise SystemExit(
                f"export was trained with label_delay_weeks={info.get('label_delay_weeks')}, "
                f"the backtest needs {cfg.label_delay_weeks} (use notebooks/stage7b_candidates)"
            )
        log.info("backtest_export_imported", dest=str(dest), models=info["models"])
        return

    if args.resamples is not None:
        cfg = cfg.model_copy(
            update={"harm": cfg.harm.model_copy(update={"bootstrap_resamples": args.resamples})}
        )
    manifest = json.loads(
        (REPO_ROOT / "docs" / "cleaning" / f"{cfg.file_type}.json").read_text("utf-8")
    )
    st = load_score_table(_dirs(cand_root), manifest)
    meta_file = meta_dir / "metadata.parquet"
    meta = pd.read_parquet(meta_file) if meta_file.exists() else None
    if meta is not None:  # every scored file must find its metadata row
        missing = set(st.file_ids.astype(str)) - set(meta["sha256"])
        if missing:
            raise SystemExit(f"{len(missing)} scored files have no metadata row")
    res = run_pass1(st, meta, cfg, gate)
    table = summary_table(res["mean_recall_ci"], res["decisions"], res["weekly"])

    # 1d: the original Stage 7 harms, re-judged with the new definition
    old_root = settings.data_dir / "experiment" / cfg.file_type
    original = pd.read_parquet(settings.results_dir / "experiment.parquet")
    original = original[
        (original["resample"] == 0) & (original["harmful"] == True)  # noqa: E712
    ]
    st_old = load_score_table(_dirs(old_root), manifest)
    redo = rejudge(st_old, meta, cfg, gate, original, res["noise"].margin_points)

    prov = {
        "seed": cfg.seed,
        "data_fingerprint": manifest["data_fingerprint"],
        "git_commit": git_commit(REPO_ROOT),
        "gate_policy_version": gate.policy_version,
        "cluster_level": res["cluster_level"],
        "backtest_config": cfg.model_dump(mode="json"),
        "exports": [d.name for d in _dirs(cand_root)],
    }
    out = settings.results_dir
    out.mkdir(parents=True, exist_ok=True)
    for name, df in (
        ("decisions", res["decisions"]),
        ("weekly", res["weekly"]),
        ("summary", table),
        ("original_harms", redo),
        ("early_labels", res["early_labels"]),
    ):
        _write(df, out / f"backtest_pass1_{name}.parquet", prov)
    write_pass1_md(REPO_ROOT / "docs" / "BACKTEST.md", res, table, redo, prov)
    log.info("backtest_pass1_done", rows=len(res["decisions"]), out=str(out))


if __name__ == "__main__":
    main()
