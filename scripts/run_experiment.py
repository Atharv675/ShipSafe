"""Stage 7 (local, sandbox): import Kaggle exports, replay the release policies, report.

    python scripts/run_experiment.py import --zip /in/export.zip
    python scripts/run_experiment.py run [--resamples N] [--no-mlflow]

`import` verifies one export zip (allow-listed names, sha256) and unpacks it under
$SENTINEL_DATA_DIR/experiment/<type>/<zip name>/. `run` merges every imported export,
simulates P1/P2/P3 on the real stream and the bootstrap resamples, and writes:
- $SENTINEL_DATA_DIR/results/experiment.parquet (one row per decision) and
  experiment_weekly.parquet (live model per week), both carrying seed, data fingerprint and
  git commit in their metadata;
- docs/results/*.png and the auto section of docs/RESULTS.md;
- one MLflow run in experiment `sentinel-experiment` (params, metrics, artifacts).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from sentinel.config import REPO_ROOT, Settings, load_experiment_config, load_gate_policy
from sentinel.experiment.export import import_export_zip
from sentinel.experiment.report import (
    by_retrain_policy,
    charts,
    drift_precision,
    markdown,
    per_resample,
    summary_table,
    write_results_md,
)
from sentinel.experiment.scores import load_score_table
from sentinel.experiment.simulate import run_experiment
from sentinel.logging import configure_logging, get_logger
from sentinel.provenance import git_commit

log = get_logger(__name__)
MLFLOW_EXPERIMENT = "sentinel-experiment"


def _write(df: pd.DataFrame, path: Path, meta: dict[str, Any]) -> None:
    t = pa.Table.from_pandas(df, preserve_index=False)
    t = t.replace_schema_metadata(
        {**(t.schema.metadata or {}), b"sentinel": json.dumps(meta).encode("utf-8")}
    )
    pq.write_table(t, path, compression="zstd")


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    exp = load_experiment_config()
    policy = load_gate_policy()
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import").add_argument("--zip", type=Path, required=True)
    r = sub.add_parser("run")
    r.add_argument("--resamples", type=int, default=None)
    r.add_argument("--no-mlflow", action="store_true")
    args = p.parse_args()
    root = settings.data_dir / "experiment" / exp.file_type

    if args.cmd == "import":
        dest = root / args.zip.stem
        info = import_export_zip(args.zip, dest)
        if info["file_type"] != exp.file_type:
            raise SystemExit(f"export is for {info['file_type']}, experiment for {exp.file_type}")
        log.info("experiment_export_imported", dest=str(dest), models=info["models"])
        return

    dirs = sorted(d for d in root.iterdir() if d.is_dir()) if root.exists() else []
    manifest = json.loads(
        (REPO_ROOT / "docs" / "cleaning" / f"{exp.file_type}.json").read_text("utf-8")
    )
    st = load_score_table(dirs, manifest)
    decisions, weekly = run_experiment(st, exp, policy, args.resamples)
    n = exp.bootstrap_resamples if args.resamples is None else args.resamples
    meta = {
        "seed": exp.seed,
        "data_fingerprint": manifest["data_fingerprint"],
        "git_commit": git_commit(REPO_ROOT),
        "gate_policy_version": policy.policy_version,
        "resamples": n,
        "exports": [d.name for d in dirs],
        "export_git_commits": sorted({i["git_commit"] for i in st.info["exports"]}),
        "experiment_config": exp.model_dump(mode="json"),
    }
    out = settings.results_dir
    out.mkdir(parents=True, exist_ok=True)
    _write(decisions, out / "experiment.parquet", meta)
    _write(weekly, out / "experiment_weekly.parquet", meta)

    rs = per_resample(decisions, weekly)
    table = summary_table(rs)
    drift = drift_precision(decisions)
    pngs = charts(table, weekly, REPO_ROOT / "docs" / "results")
    rel = {k: f"results/{v.name}" for k, v in pngs.items()}
    auto = markdown(table, by_retrain_policy(decisions), drift, n, rel)
    write_results_md(REPO_ROOT / "docs" / "RESULTS.md", auto)
    table.to_csv(out / "experiment_summary.csv", index=False)
    (out / "experiment_meta.json").write_text(json.dumps(meta, indent=1) + "\n", "utf-8")
    log.info("experiment_done", decisions=len(decisions), resamples=n, out=str(out))

    if not args.no_mlflow:
        import mlflow

        mlflow.set_tracking_uri(settings.tracking_uri)
        # Artifacts live in the registry's artifact root (as in models/registry.py), never in
        # the repo: the default ./mlruns would be the read-only /work mount in the sandbox.
        found = mlflow.get_experiment_by_name(MLFLOW_EXPERIMENT)
        exp_id = (
            found.experiment_id
            if found
            else mlflow.create_experiment(
                MLFLOW_EXPERIMENT,
                artifact_location=(settings.mlflow_dir / "artifacts").as_uri(),
            )
        )
        with mlflow.start_run(experiment_id=exp_id, run_name=f"stage7-{exp.file_type}"):
            mlflow.log_params(
                {
                    "file_type": exp.file_type,
                    "seed": exp.seed,
                    "resamples": n,
                    "retrain_weeks": exp.retrain_weeks,
                    "retrain_policies": exp.retrain_policies,
                    "shadow_weeks": exp.shadow_weeks,
                    "harm_weeks": exp.harm_weeks,
                    "gate_policy_version": policy.policy_version,
                    "data_fingerprint": manifest["data_fingerprint"],
                    "git_commit": meta["git_commit"],
                }
            )
            for _, row in table.iterrows():
                rp = str(row["release_policy"])
                for m in ("promotions", "harmful_promotions", "good_blocked", "mean_recall_at_fpr"):
                    if pd.notna(row[m]):
                        mlflow.log_metric(f"{rp}.{m}", float(row[m]))
            for f in ("experiment.parquet", "experiment_weekly.parquet", "experiment_summary.csv"):
                mlflow.log_artifact(str(out / f))
            for png in pngs.values():
                mlflow.log_artifact(str(png))


if __name__ == "__main__":
    main()
