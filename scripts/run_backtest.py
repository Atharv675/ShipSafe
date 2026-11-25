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
from sentinel.backtest.pass1 import (
    Ctx,
    cluster_keys,
    measure_noise,
    rejudge,
    run_pass1,
    summary_table,
)
from sentinel.backtest.pass2 import policy_difference, run_pass2
from sentinel.backtest.report import write_pass1_md, write_pass2_md
from sentinel.config import (
    REPO_ROOT,
    BacktestConfig,
    GatePolicy,
    Settings,
    load_backtest_config,
    load_experiment_config,
    load_gate_policy,
)
from sentinel.experiment.export import import_export_zip
from sentinel.experiment.scores import ScoreTable, load_score_table
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


def _pass2(
    settings: Settings,
    cfg: BacktestConfig,
    gate: GatePolicy,
    st: ScoreTable,
    meta: pd.DataFrame | None,
    manifest: dict[str, Any],
) -> None:
    """Pass 2 (owner brief steps 4-9). Needs Pass 1's outputs (M, its weekly rows)."""
    out = settings.results_dir
    p1_summary = pd.read_parquet(out / "backtest_pass1_summary.parquet")
    p1_weekly = pd.read_parquet(out / "backtest_pass1_weekly.parquet")
    p1_meta = json.loads(
        pq.read_schema(out / "backtest_pass1_summary.parquet").metadata[b"sentinel"]
    )
    margin = _pass1_margin(st, meta, cfg, gate)
    exp = pd.read_parquet(out / "experiment.parquet")
    rej = exp[
        (exp["resample"] == 0)
        & (exp["release_policy"] == "P3_gate")
        & (exp["decided"] == True)  # noqa: E712
        & (exp["promoted"] == False)  # noqa: E712
    ]
    rejected = [(str(c), str(p)) for c, p in zip(rej["candidate"], rej["production"], strict=True)]
    res = run_pass2(st, meta, cfg, gate, margin, load_experiment_config().drift, rejected)
    ctx = res["ctx"]
    t2 = summary_table(res["mean_recall_ci"], res["decisions"], res["weekly"])
    ev = res["events"]
    rb = ev[ev["event"] == "rollback"].groupby("policy").size().to_dict() if len(ev) else {}
    t2["rollbacks"] = [int(rb.get(str(x), 0)) for x in t2["release_policy"]]
    t1 = p1_summary.assign(rollbacks=0)
    table = pd.concat([t1, t2], ignore_index=True)

    # which differences are real: each policy vs P1 off, plus two direct comparisons
    allw = pd.concat([p1_weekly, res["weekly"]], ignore_index=True)

    def chain(rp: str, recal: bool) -> pd.DataFrame:
        return allw[(allw["release_policy"] == rp) & (allw["recalibration"] == recal)]

    pairs = [
        (("P2_val_auc", False), ("P1_auto", False)),
        (("P3_gate", False), ("P1_auto", False)),
        (("P3_gate", True), ("P3_gate", False)),
        (("P4_clean", True), ("P1_auto", False)),
        (("P4_traffic", True), ("P1_auto", False)),
        (("P4_walk_forward", True), ("P1_auto", False)),
        (("P4_clean", True), ("P3_gate", True)),
        (("P5_blend", True), ("P1_auto", False)),
        (("P5_blend", True), ("P1_auto", True)),
    ]
    rows = []
    for (a, ra), (b, rb_) in pairs:
        for metric in ("recall_at_fpr", "shipped_fpr"):
            d, lo, hi = policy_difference(ctx, chain(a, ra), chain(b, rb_), metric)
            rows.append(
                {
                    "comparison": f"{a} ({'on' if ra else 'off'}) - {b} ({'on' if rb_ else 'off'})",
                    "metric": "recall@0.1%FPR" if metric == "recall_at_fpr" else "shipped FPR",
                    "diff": d,
                    "ci_low": lo,
                    "ci_high": hi,
                }
            )
    diffs = pd.DataFrame(rows)
    prov = {
        **p1_meta,
        "git_commit": git_commit(REPO_ROOT),
        "backtest_config": cfg.model_dump(mode="json"),
        "p4_params": res["params"],
        "margin_points": margin,
    }
    for name in (
        "decisions",
        "weekly",
        "events",
        "rejected_redo",
        "mcnemar",
        "training_window",
        "drift",
    ):
        _write(res[name], out / f"backtest_pass2_{name}.parquet", prov)
    _write(table, out / "backtest_pass2_summary.parquet", prov)
    _write(diffs, out / "backtest_pass2_differences.parquet", prov)
    write_pass2_md(REPO_ROOT / "docs" / "BACKTEST.md", res, table, diffs)
    log.info("backtest_pass2_done", decisions=len(res["decisions"]), out=str(out))


def _pass1_margin(
    st: ScoreTable, meta: pd.DataFrame | None, cfg: BacktestConfig, gate: GatePolicy
) -> float:
    ctx = Ctx(st, cfg, gate, cluster_keys(st, meta), "family" if meta is not None else "file")
    return measure_noise(ctx).margin_points


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
    r2 = sub.add_parser("pass2")
    r2.add_argument("--resamples", type=int, default=None)
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
    if args.cmd == "pass2":
        _pass2(settings, cfg, gate, st, meta, manifest)
        return
    res = run_pass1(st, meta, cfg, gate)
    table = summary_table(res["mean_recall_ci"], res["decisions"], res["weekly"])

    # 1d: the original Stage 7 harmful PROMOTIONS, re-judged with the new definition (Stage 7
    # also marked rejected candidates harmful as a counterfactual; those never shipped)
    old_root = settings.data_dir / "experiment" / cfg.file_type
    original = pd.read_parquet(settings.results_dir / "experiment.parquet")
    original = original[
        (original["resample"] == 0)
        & (original["harmful"] == True)  # noqa: E712
        & (original["promoted"] == True)  # noqa: E712
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
