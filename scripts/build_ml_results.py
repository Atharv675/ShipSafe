"""Stage 9: the results the ML dashboard reads (sandbox; no network).

    python scripts/build_ml_results.py

Labels never reach PostgreSQL, so the Head-to-head and Drift pages read files written here
from the Stage 7b Kaggle exports ($SENTINEL_DATA_DIR/backtest/<type>/candidates):

- results/ml_scores.parquet  file_id, week, y, raw_<model>, prob_<model> for v1 and the 18
                             point-in-time candidates (raw scores kept as float64: shipped
                             cut-offs differ in the 6th decimal)
- results/ml_models.json     per model: retrain policy, as-of week, shipped cut-off, fit and
                             calibration weeks, never-break flags
- results/ml_psi.parquet     weekly PSI of v1's top-20 features against a fixed reference
                             window (configs/monitoring.yaml dashboard.ml.psi_reference_weeks)

Every file records seed, data fingerprint and git commit.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from sentinel.config import REPO_ROOT, Settings, load_backtest_config, load_monitoring_config
from sentinel.experiment.scores import load_score_table
from sentinel.logging import configure_logging, get_logger
from sentinel.models.evaluate import population_stability_index
from sentinel.provenance import git_commit

log = get_logger(__name__)


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_backtest_config()
    ml = load_monitoring_config().dashboard.ml
    root = settings.data_dir / "backtest" / cfg.file_type / "candidates"
    dirs = sorted(d for d in root.iterdir() if d.is_dir()) if root.exists() else []
    if not dirs:
        raise SystemExit(f"no Stage 7b exports in {root} (make backtest-import first)")
    manifest = json.loads(
        (REPO_ROOT / "docs" / "cleaning" / f"{cfg.file_type}.json").read_text("utf-8")
    )
    st = load_score_table(dirs, manifest)
    prov = {
        "seed": cfg.seed,
        "data_fingerprint": manifest["data_fingerprint"],
        "git_commit": git_commit(REPO_ROOT),
        "file_type": cfg.file_type,
        "exports": [d.name for d in dirs],
    }
    out = settings.results_dir
    out.mkdir(parents=True, exist_ok=True)

    cols: dict[str, object] = {"file_id": st.file_ids.astype(str), "week": st.week, "y": st.y}
    for m in sorted(st.models):
        cols[f"raw_{m}"] = st.raw[m]
        cols[f"prob_{m}"] = st.prob[m].astype(np.float32)
    _write(pd.DataFrame(cols), out / "ml_scores.parquet", prov)

    models = {
        m: {
            "model_id": m,
            "policy": i.policy,
            "as_of_week": i.as_of_week,
            "threshold": i.threshold,
            "fit_weeks": [min(i.fit_weeks), max(i.fit_weeks)],
            "calibration_weeks": [min(i.calibration_weeks), max(i.calibration_weeks)],
            "never_break_flags": st.flags_never_break(m),
        }
        for m, i in st.models.items()
    }
    (out / "ml_models.json").write_text(
        json.dumps({"provenance": prov, "models": models}, indent=1) + "\n", "utf-8"
    )

    rows = []
    if st.histograms is not None:
        counts = st.histograms["counts"]
        ref_weeks = [str(w) for w in ml.psi_reference_weeks.weeks]
        for f in st.histograms["features"]:
            ref = np.sum([counts[f][w] for w in ref_weeks if w in counts[f]], axis=0)
            for w, c in counts[f].items():
                rows.append(
                    {
                        "feature": f,
                        "week": int(w),
                        "psi": population_stability_index(ref, np.asarray(c)),
                    }
                )
    psi = pd.DataFrame(rows, columns=["feature", "week", "psi"])
    _write(
        psi,
        out / "ml_psi.parquet",
        {**prov, "reference_weeks": ml.psi_reference_weeks.weeks},
    )
    log.info(
        "ml_results_written",
        files=int(st.file_ids.size),
        models=len(models),
        psi_rows=len(psi),
        out=str(out),
    )


def _write(df: pd.DataFrame, path: Path, meta: dict[str, object]) -> None:
    t = pa.Table.from_pandas(df, preserve_index=False)
    t = t.replace_schema_metadata(
        {**(t.schema.metadata or {}), b"sentinel": json.dumps(meta).encode("utf-8")}
    )
    pq.write_table(t, path, compression="zstd")


if __name__ == "__main__":
    main()
