"""Stage 7, heavy part (runs on Kaggle): train the experiment's candidates and export SCORES.

For every model (v1 + each candidate retrained at T with each retraining policy) this writes
its raw score and calibrated probability for every file of the scored weeks, and its raw
score on the never-break set. Plus per-week histograms of v1's top features for drift (PSI).
No feature vectors leave Kaggle: only scores, labels, weeks, label times and file hashes.

Each candidate is built by `sentinel.models.retrain.retrain_candidate` (labels known by T,
threshold + calibration on the 4 newest weeks <= T). v1 is the registered Stage 2 bundle,
uploaded as a Kaggle input; it is never retrained.

The work can be split over several Kaggle runs (`--policies`); `merge_exports` joins them
locally after checking that every run saw identical rows.
"""

from __future__ import annotations

import json
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq

from sentinel.config import (
    REPO_ROOT,
    ExperimentConfig,
    RetrainConfig,
    RetrainPolicyName,
    TrainConfig,
    candidate_id,
)
from sentinel.data.schema import ID_COLUMN, LABEL_AVAILABLE_COLUMN, LABEL_COLUMN, WEEK_COLUMN
from sentinel.data.splits import feature_columns, load_manifest, read_matrix
from sentinel.logging import get_logger
from sentinel.models.calibration import IsotonicCalibrator
from sentinel.models.retrain import retrain_candidate
from sentinel.models.train import LGBM_CAL_FILE, LGBM_FILE, METRICS_FILE, RUN_INFO_FILE
from sentinel.provenance import git_commit
from sentinel.replay.transfer import sha256_file

log = get_logger(__name__)

SCORES_FILE = "scores.parquet"
NEVER_BREAK_FILE = "never_break_scores.parquet"
MODELS_FILE = "models.json"
HIST_FILE = "feature_hist.json"
INFO_FILE = "export_info.json"
V1_ID = "v1"
EXPORT_SCHEMA_VERSION = 1


def raw_col(model_id: str) -> str:
    return f"raw:{model_id}"


def prob_col(model_id: str) -> str:
    return f"prob:{model_id}"


@dataclass(frozen=True)
class BundleModel:
    model_id: str
    booster: lgb.Booster
    calibrator: IsotonicCalibrator
    info: dict[str, Any]  # threshold_raw, as_of_week, windows, policy, ...

    def score(self, x: npt.NDArray[np.float32]) -> tuple[npt.NDArray[np.float64], ...]:
        raw = np.asarray(self.booster.predict(x), dtype=np.float64)
        return raw, self.calibrator.predict(raw)


def load_bundle(model_id: str, bundle_dir: Path, as_of_week: int | None = None) -> BundleModel:
    """A Stage 2 or Stage 4 run bundle (LightGBM + isotonic calibrator + metrics/run_info)."""
    metrics = json.loads((bundle_dir / METRICS_FILE).read_text(encoding="utf-8"))
    run = json.loads((bundle_dir / RUN_INFO_FILE).read_text(encoding="utf-8"))
    lg = metrics["models"]["lightgbm"]
    win = run["windows"]
    t = as_of_week if as_of_week is not None else int(win["as_of_week"])
    if max(win["calibration"]) > t or max(win["fit"]) > t:  # pragma: no cover - defensive
        raise ValueError(f"{model_id}: its fit/calibration weeks pass its as-of week {t}")
    info = {
        "model_id": model_id,
        "policy": run.get("policy", "v1"),
        "as_of_week": t,
        "threshold_raw": float(lg["threshold_raw"]),
        "fit_weeks": [int(w) for w in win["fit"]],
        "calibration_weeks": [int(w) for w in win["calibration"]],
        "never_break_flags_at_training": int(lg["never_break_flags"]),
        "seed": run["seed"],
        "data_fingerprint": run["data_fingerprint"],
        "feature_schema_version": run["feature_schema_version"],
        "git_commit": run["git_commit"],
        "fit_rows": int(run["counts"]["fit"]["rows"]),
        "seconds": run.get("seconds", {}),
    }
    return BundleModel(
        model_id,
        lgb.Booster(model_file=str(bundle_dir / LGBM_FILE)),
        IsotonicCalibrator.load(bundle_dir / LGBM_CAL_FILE),
        info,
    )


def train_candidates(
    file_type: str,
    splits_root: Path,
    never_break_path: Path,
    work_dir: Path,
    train_cfg: TrainConfig,
    retrain_cfg: RetrainConfig,
    exp: ExperimentConfig,
    policies: list[RetrainPolicyName],
    threads: int = 0,
) -> list[BundleModel]:
    """Retrain (or reuse, if already done in this session) one candidate per (policy, T)."""
    out: list[BundleModel] = []
    for t in exp.retrain_weeks:
        for pol in policies:
            mid = candidate_id(pol, t)
            d = work_dir / mid
            if not (d / RUN_INFO_FILE).exists():
                retrain_candidate(
                    file_type,
                    splits_root,
                    never_break_path,
                    d,
                    train_cfg,
                    retrain_cfg,
                    policy=pol,
                    as_of_week=t,
                    lgbm_threads=threads,
                )
            out.append(load_bundle(mid, d))
    return out


def top_features(booster: lgb.Booster, features: list[str], k: int) -> list[str]:
    """The k features with the largest total gain (ties broken by column order)."""
    gain = np.asarray(booster.feature_importance(importance_type="gain"), dtype=np.float64)
    order = sorted(range(len(features)), key=lambda i: (-gain[i], i))
    return [features[i] for i in order[:k]]


def quantile_edges(values: npt.NDArray[np.floating], bins: int) -> list[float]:
    """Interior bin edges at the 1/bins .. (bins-1)/bins quantiles, duplicates removed (a
    feature with many equal values gets fewer bins)."""
    qs = np.quantile(np.asarray(values, dtype=np.float64), np.linspace(0, 1, bins + 1)[1:-1])
    return sorted({float(q) for q in qs})


def bin_counts(values: npt.NDArray[np.floating], edges: list[float]) -> list[int]:
    idx = np.searchsorted(np.asarray(edges, dtype=np.float64), values, side="right")
    return [int(c) for c in np.bincount(idx, minlength=len(edges) + 1)]


def feature_histograms(
    splits_root: Path,
    file_type: str,
    features: list[str],
    edge_weeks: list[int],
    bins: int,
) -> dict[str, Any]:
    """Per-week counts of each feature in decile bins fixed on `edge_weeks` (v1's fit weeks).
    Labels are not read: drift is measured on what arrives, before any label exists."""
    weeks = sorted(int(w["week"]) for w in load_manifest(splits_root, file_type)["weeks"])
    path = splits_root / file_type
    ref: dict[str, list[npt.NDArray[np.float64]]] = {f: [] for f in features}
    for w in edge_weeks:
        t = pq.read_table(path / f"week={w:02d}" / "part.parquet", columns=features)
        for f in features:
            ref[f].append(t.column(f).to_numpy().astype(np.float64))
    edges = {f: quantile_edges(np.concatenate(ref[f]), bins) for f in features}
    counts: dict[str, dict[str, list[int]]] = {f: {} for f in features}
    for w in weeks:
        t = pq.read_table(path / f"week={w:02d}" / "part.parquet", columns=features)
        for f in features:
            counts[f][str(w)] = bin_counts(t.column(f).to_numpy(), edges[f])
    return {"features": features, "edge_weeks": edge_weeks, "edges": edges, "counts": counts}


def export_scores(
    file_type: str,
    splits_root: Path,
    never_break_path: Path,
    out_dir: Path,
    models: list[BundleModel],
    exp: ExperimentConfig,
    seed: int,
) -> dict[str, Any]:
    """Score every model on the scored weeks + never-break set; write the export files."""
    t0 = time.time()
    manifest = load_manifest(splits_root, file_type)
    features = feature_columns(manifest)
    out_dir.mkdir(parents=True, exist_ok=True)
    weeks = exp.score_weeks.weeks
    mat = read_matrix(splits_root, file_type, weeks, features)  # every row, labelled or not
    avail = pa.concat_arrays(
        [
            pq.read_table(
                splits_root / file_type / f"week={w:02d}" / "part.parquet",
                columns=[LABEL_AVAILABLE_COLUMN],
            )
            .column(0)
            .combine_chunks()
            .cast(pa.timestamp("s", tz="UTC"))
            .cast(pa.int64())
            for w in sorted(weeks)
        ]
    )
    cols: dict[str, Any] = {
        ID_COLUMN: pa.array(mat.ids, pa.string()),
        WEEK_COLUMN: pa.array(mat.week.astype(np.int16)),
        LABEL_COLUMN: pa.array(mat.y.astype(np.int8)),
        LABEL_AVAILABLE_COLUMN: avail,  # epoch seconds (UTC)
    }
    nb = pq.read_table(never_break_path, columns=[ID_COLUMN, *features])
    nb_x = np.column_stack([nb.column(f).to_numpy() for f in features]).astype(np.float32)
    nb_cols: dict[str, Any] = {ID_COLUMN: nb.column(ID_COLUMN)}
    for m in models:
        raw, prob = m.score(mat.x)
        cols[raw_col(m.model_id)] = pa.array(raw)
        cols[prob_col(m.model_id)] = pa.array(prob)
        nb_cols[raw_col(m.model_id)] = pa.array(m.score(nb_x)[0])
        log.info("experiment_scored", model=m.model_id, rows=len(raw))
    pq.write_table(pa.table(cols), out_dir / SCORES_FILE, compression="zstd")
    pq.write_table(pa.table(nb_cols), out_dir / NEVER_BREAK_FILE, compression="zstd")
    (out_dir / MODELS_FILE).write_text(
        json.dumps({m.model_id: m.info for m in models}, indent=1) + "\n", encoding="utf-8"
    )
    info = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "file_type": file_type,
        "seed": seed,
        "data_fingerprint": manifest["data_fingerprint"],
        "feature_schema_version": manifest["feature_schema_version"],
        "git_commit": git_commit(REPO_ROOT),
        "experiment_config": exp.model_dump(mode="json"),
        "score_weeks": weeks,
        "rows": len(mat.ids),
        "never_break_rows": nb.num_rows,
        "models": [m.model_id for m in models],
        "seconds": round(time.time() - t0, 1),
    }
    (out_dir / INFO_FILE).write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
    return info


EXPORT_FILES = (SCORES_FILE, NEVER_BREAK_FILE, MODELS_FILE, HIST_FILE, INFO_FILE)
CHECKSUMS_FILE = "sha256.json"


def write_export_zip(out_dir: Path, zip_path: Path) -> dict[str, str]:
    """Pack the export files with a sha256 list (checked again on import)."""
    sums = {name: sha256_file(out_dir / name) for name in EXPORT_FILES}
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(CHECKSUMS_FILE, json.dumps(sums, indent=1) + "\n")
        for name in EXPORT_FILES:
            zf.write(out_dir / name, name)
    return sums


def import_export_zip(zip_path: Path, dest: Path) -> dict[str, Any]:
    """Unpack ONE Kaggle export into `dest` after checking it: only the expected file names
    (no paths), every sha256 matches. Returns its export_info."""
    allowed = {*EXPORT_FILES, CHECKSUMS_FILE}
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            if set(names) != allowed or len(names) != len(allowed):
                raise ValueError(f"unexpected files in export: {sorted(set(names) ^ allowed)}")
            for name in names:
                with zf.open(name) as src, (dest / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        sums = json.loads((dest / CHECKSUMS_FILE).read_text(encoding="utf-8"))
        for name in EXPORT_FILES:
            if sha256_file(dest / name) != sums.get(name):
                raise ValueError(f"{name}: sha256 does not match {CHECKSUMS_FILE}")
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    info: dict[str, Any] = json.loads((dest / INFO_FILE).read_text(encoding="utf-8"))
    return info
