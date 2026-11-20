"""Stage 2: train v1 (LightGBM) and the logistic-regression baseline for ONE dataset.

Windows (configs/train.yaml): fit on weeks 1..K-c, calibrate + choose the fixed-FPR threshold
on the held-out weeks K-c+1..K, evaluate on week K+1. Only labels available by the end of
`as_of_week` are used for fitting and calibration. Test weeks are scored for reporting only.

Output: a self-contained run bundle directory (models as text/npz, metrics.json,
run_info.json). Registration into MLflow happens separately (sentinel.models.registry), so the
heavy training can run on Kaggle and the registry stays on the owner's machine.
"""

from __future__ import annotations

import json
import platform
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import numpy.typing as npt
import pyarrow.parquet as pq
import sklearn

from sentinel.config import REPO_ROOT, TrainConfig
from sentinel.data.schema import LABEL_COLUMN
from sentinel.data.splits import (
    Matrix,
    feature_columns,
    load_manifest,
    read_matrix,
    week_end,
    weeks_in_split,
)
from sentinel.logging import get_logger
from sentinel.models.calibration import IsotonicCalibrator
from sentinel.models.evaluate import (
    auc,
    auprc,
    expected_calibration_error,
    per_week_metrics,
    rates_at_threshold,
    reliability_curve,
    threshold_at_fpr,
)
from sentinel.models.logreg import LogRegModel
from sentinel.provenance import git_commit

BUNDLE_SCHEMA_VERSION = 1
LGBM_FILE = "lightgbm.txt"
LGBM_CAL_FILE = "lightgbm_calibration.npz"
LR_FILE = "logreg.npz"
LR_CAL_FILE = "logreg_calibration.npz"
METRICS_FILE = "metrics.json"
RUN_INFO_FILE = "run_info.json"
MODELS = ("lightgbm", "logreg")

log = get_logger(__name__)


@dataclass(frozen=True)
class Windows:
    fit: list[int]
    calibration: list[int]
    eval_week: int
    test: list[int]


def plan_windows(manifest: dict[str, Any], cfg: TrainConfig) -> Windows:
    """Resolve and CHECK the week windows: no test week may be fitted, calibrated or tuned on."""
    train, test = weeks_in_split(manifest, "train"), weeks_in_split(manifest, "test")
    w = cfg.weeks
    used = [*w.fit, *w.calibration, w.eval_week]
    missing = sorted(set(used) - set(train))
    if missing:
        raise ValueError(
            f"weeks {missing} are not TRAIN weeks of this dataset; test weeks are never used "
            "for fitting, calibration, thresholds or validation"
        )
    if w.as_of_week in test:
        raise ValueError("as_of_week must not be a test week")
    if test and max(used) >= min(test):
        raise ValueError("training windows must end before the first test week")
    return Windows(w.fit, w.calibration, w.eval_week, test)


def original_index(name: str) -> int:
    """f_0701 -> 701 (columns are named after their thrember vector index)."""
    digits = name[len(name.rstrip("0123456789")) :]
    if not digits:
        raise ValueError(f"feature column {name!r} has no index suffix")
    return int(digits)


def lightgbm_categoricals(features: list[str], cfg: TrainConfig) -> list[str]:
    """thrember's categorical columns that survived cleaning (matched by original index)."""
    wanted = set(cfg.lightgbm.categorical_feature_indices)
    return [f for f in features if original_index(f) in wanted]


def _score_lgbm(booster: lgb.Booster, x: npt.NDArray[np.float32]) -> npt.NDArray[np.float64]:
    return np.asarray(booster.predict(x), dtype=np.float64)


def _model_metrics(
    name: str,
    raw_cal: npt.NDArray[np.float64],
    y_cal: npt.NDArray[np.int8],
    eval_m: Matrix,
    raw_eval: npt.NDArray[np.float64],
    test_y: npt.NDArray[np.int8],
    test_w: npt.NDArray[np.int16],
    raw_test: npt.NDArray[np.float64],
    raw_nb: npt.NDArray[np.float64],
    calibrator: IsotonicCalibrator,
    cfg: TrainConfig,
) -> dict[str, Any]:
    thr = threshold_at_fpr(y_cal, raw_cal, cfg.target_fpr)
    cal_rates = rates_at_threshold(y_cal, raw_cal, thr, cfg.confidence)
    p_eval = calibrator.predict(raw_eval)
    out: dict[str, Any] = {
        "model": name,
        "threshold_raw": thr,
        "threshold_probability": float(calibrator.predict(np.array([thr]))[0]),
        "calibration": cal_rates.as_dict(),
        "eval_week": {
            "week": int(eval_m.week[0]) if len(eval_m.week) else None,
            **rates_at_threshold(eval_m.y, raw_eval, thr, cfg.confidence).as_dict(),
            "auc": auc(eval_m.y, raw_eval),
            "auprc": auprc(eval_m.y, raw_eval),
            "ece": expected_calibration_error(eval_m.y, p_eval, cfg.calibration.reliability_bins),
            "ece_uncalibrated": expected_calibration_error(
                eval_m.y, np.clip(raw_eval, 0, 1), cfg.calibration.reliability_bins
            ),
            "reliability": [
                vars(b)
                for b in reliability_curve(eval_m.y, p_eval, cfg.calibration.reliability_bins)
            ],
        },
        "never_break_flags": int((raw_nb > thr).sum()),
        "never_break_rows": int(raw_nb.size),
    }
    if test_y.size:
        out["test_weeks_report_only"] = {
            "pooled": {
                **rates_at_threshold(test_y, raw_test, thr, cfg.confidence).as_dict(),
                "auc": auc(test_y, raw_test),
                "auprc": auprc(test_y, raw_test),
                "ece": expected_calibration_error(
                    test_y, calibrator.predict(raw_test), cfg.calibration.reliability_bins
                ),
            },
            "per_week": per_week_metrics(test_w, test_y, raw_test, thr, cfg.confidence),
        }
    return out


def train_baseline(
    file_type: str,
    splits_root: Path,
    never_break_path: Path,
    out_dir: Path,
    cfg: TrainConfig,
    *,
    lgbm_threads: int = 0,
) -> dict[str, Any]:
    """Train both models for one dataset and write the run bundle into `out_dir`."""
    t_start = time.time()
    blog = log.bind(file_type=file_type)
    manifest = load_manifest(splits_root, file_type)
    features = feature_columns(manifest)
    win = plan_windows(manifest, cfg)
    as_of = week_end(manifest, cfg.weeks.as_of_week)
    blog.info(
        "train_start",
        fit=f"{win.fit[0]}-{win.fit[-1]}",
        calibration=win.calibration,
        eval_week=win.eval_week,
        as_of=as_of.isoformat(),
        features=len(features),
    )

    # ---- fit data (labels known by as_of only)
    fit = read_matrix(splits_root, file_type, win.fit, features, as_of=as_of)
    rng = np.random.default_rng(cfg.seed)
    n_lr = min(cfg.logreg.max_rows, len(fit.y))
    lr_idx = np.sort(rng.choice(len(fit.y), size=n_lr, replace=False))
    lr_x, lr_y = fit.x[lr_idx], fit.y[lr_idx]
    fit_counts = {
        "rows": len(fit.y),
        "malicious": int(fit.y.sum()),
        "unlabelled_dropped": fit.dropped_unlabelled,
    }
    blog.info("fit_loaded", **fit_counts)

    # ---- LightGBM (binned dataset, then the raw matrix is released)
    params = {**cfg.lightgbm.params, "seed": cfg.seed, "num_threads": lgbm_threads}
    cats = lightgbm_categoricals(features, cfg)
    dataset = lgb.Dataset(
        fit.x,
        label=fit.y,
        feature_name=features,
        categorical_feature=cats,
        free_raw_data=True,
        params=params,  # dataset-level params (binning, min_data_in_leaf) must match training
    )
    dataset.construct()
    del fit
    t0 = time.time()
    booster = lgb.train(params, dataset)
    lgbm_seconds = time.time() - t0
    del dataset
    blog.info("lightgbm_trained", seconds=round(lgbm_seconds, 1), trees=booster.num_trees())

    # ---- logistic regression on a seeded subsample
    t0 = time.time()
    lr = LogRegModel.fit(lr_x, lr_y, cfg.logreg, cfg.seed)
    lr_seconds = time.time() - t0
    del lr_x, lr_y
    blog.info("logreg_trained", seconds=round(lr_seconds, 1), rows=n_lr, converged=lr.converged)

    # ---- calibration weeks: isotonic + fixed-FPR threshold
    cal = read_matrix(splits_root, file_type, win.calibration, features, as_of=as_of)
    raw_cal = {"lightgbm": _score_lgbm(booster, cal.x), "logreg": lr.predict(cal.x)}
    calibrators = {m: IsotonicCalibrator.fit(raw_cal[m], cal.y) for m in MODELS}
    cal_counts = {
        "rows": len(cal.y),
        "benign": int((cal.y == 0).sum()),
        "unlabelled_dropped": cal.dropped_unlabelled,
    }

    # ---- eval week (ground truth, no as_of filter: evaluation is retrospective)
    ev = read_matrix(splits_root, file_type, [win.eval_week], features)
    raw_ev = {"lightgbm": _score_lgbm(booster, ev.x), "logreg": lr.predict(ev.x)}

    # ---- test weeks: scored week by week, REPORT ONLY
    ty: list[npt.NDArray[np.int8]] = []
    tw: list[npt.NDArray[np.int16]] = []
    raw_t: dict[str, list[npt.NDArray[np.float64]]] = {m: [] for m in MODELS}
    for w in win.test:
        m = read_matrix(splits_root, file_type, [w], features)
        ty.append(m.y)
        tw.append(m.week)
        raw_t["lightgbm"].append(_score_lgbm(booster, m.x))
        raw_t["logreg"].append(lr.predict(m.x))
    test_y = np.concatenate(ty) if ty else np.empty(0, np.int8)
    test_w = np.concatenate(tw) if tw else np.empty(0, np.int16)

    # ---- never-break set (benign TRAIN-week files that must never be flagged)
    nb = pq.read_table(never_break_path, columns=[LABEL_COLUMN, *features])
    nb_x = np.column_stack([nb.column(f).to_numpy() for f in features]).astype(np.float32)
    raw_nb = {"lightgbm": _score_lgbm(booster, nb_x), "logreg": lr.predict(nb_x)}

    metrics = {
        m: _model_metrics(
            m,
            raw_cal[m],
            cal.y,
            ev,
            raw_ev[m],
            test_y,
            test_w,
            np.concatenate(raw_t[m]) if raw_t[m] else np.empty(0),
            raw_nb[m],
            calibrators[m],
            cfg,
        )
        for m in MODELS
    }

    # ---- write the bundle
    out_dir.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(out_dir / LGBM_FILE))
    calibrators["lightgbm"].save(out_dir / LGBM_CAL_FILE)
    lr.save(out_dir / LR_FILE)
    calibrators["logreg"].save(out_dir / LR_CAL_FILE)
    result = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "file_type": file_type,
        "target_fpr": cfg.target_fpr,
        "primary_model": "lightgbm",
        "models": metrics,
    }
    run_info = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "file_type": file_type,
        "seed": cfg.seed,
        "feature_schema_version": manifest["feature_schema_version"],
        "data_fingerprint": manifest["data_fingerprint"],
        "clean_seed": manifest["seed"],
        "git_commit": git_commit(REPO_ROOT),
        "train_config": cfg.model_dump(mode="json"),
        "lightgbm_params": params,
        "categorical_features": cats,
        "features": features,
        "windows": {
            "fit": win.fit,
            "calibration": win.calibration,
            "eval_week": win.eval_week,
            "test_report_only": win.test,
            "as_of": as_of.isoformat(),
        },
        "counts": {
            "fit": fit_counts,
            "calibration": cal_counts,
            "eval": {"rows": len(ev.y)},
            "test": {"rows": int(test_y.size)},
            "logreg_rows": n_lr,
        },
        "seconds": {
            "lightgbm": round(lgbm_seconds, 1),
            "logreg": round(lr_seconds, 1),
            "total": round(time.time() - t_start, 1),
        },
        "versions": {
            "python": platform.python_version(),
            "lightgbm": lgb.__version__,
            "numpy": np.__version__,
            "sklearn": sklearn.__version__,
        },
    }
    (out_dir / METRICS_FILE).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    (out_dir / RUN_INFO_FILE).write_text(json.dumps(run_info, indent=1) + "\n", encoding="utf-8")
    lg = metrics["lightgbm"]["eval_week"]
    blog.info(
        "train_done",
        eval_recall=round(lg["recall"], 4),
        eval_fpr=round(lg["fpr"], 5),
        out=str(out_dir),
        seconds=run_info["seconds"]["total"],
    )
    return result
