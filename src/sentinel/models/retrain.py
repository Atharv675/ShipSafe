"""Stage 4: retrain a CANDIDATE model at simulated time T, using only what is known by T.

T is the end of week `as_of_week`. The candidate:
- may only read weeks <= T (any split: owner decision 2026-11-20, see DECISIONS.md);
- only sees rows whose label is available by T (late labels);
- takes its isotonic calibration and fixed-FPR threshold from the newest
  `calibration_weeks` weeks <= T (labels known by T), and fits on the weeks before them;
- is judged only on weeks AFTER T (Stage 5 gate), never here.

Policies (configs/retrain.yaml): all_history, last_n_weeks, recency_weighted.
Output: a run bundle like Stage 2's (LightGBM + calibrator + metrics.json + run_info.json),
registered locally under the `candidate` alias.
"""

from __future__ import annotations

import json
import platform
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import numpy.typing as npt
import pyarrow.parquet as pq

from sentinel.config import REPO_ROOT, RetrainConfig, RetrainPolicyName, TrainConfig
from sentinel.data.schema import LABEL_COLUMN
from sentinel.data.splits import feature_columns, load_manifest, read_matrix, week_end
from sentinel.logging import get_logger
from sentinel.models.calibration import IsotonicCalibrator
from sentinel.models.evaluate import rates_at_threshold, threshold_at_fpr
from sentinel.models.train import (
    BUNDLE_SCHEMA_VERSION,
    LGBM_CAL_FILE,
    LGBM_FILE,
    METRICS_FILE,
    RUN_INFO_FILE,
    lightgbm_categoricals,
)
from sentinel.provenance import git_commit

log = get_logger(__name__)


@dataclass(frozen=True)
class RetrainWindows:
    policy: RetrainPolicyName
    as_of_week: int
    as_of: datetime
    fit: list[int]
    calibration: list[int]

    @property
    def used(self) -> list[int]:
        return [*self.fit, *self.calibration]


def plan_retrain_windows(
    manifest: dict[str, Any],
    cfg: RetrainConfig,
    policy: RetrainPolicyName,
    as_of_week: int,
) -> RetrainWindows:
    weeks = sorted(int(w["week"]) for w in manifest["weeks"])
    if as_of_week not in weeks:
        raise ValueError(f"week {as_of_week} is not in this dataset")
    known = [w for w in weeks if w <= as_of_week]  # never anything after T
    if len(known) <= cfg.calibration_weeks:
        raise ValueError(f"T = week {as_of_week} leaves no weeks to fit on")
    calibration = known[-cfg.calibration_weeks :]
    fit = known[: -cfg.calibration_weeks]
    if policy == "last_n_weeks":
        fit = fit[-cfg.policies.last_n_weeks["n_weeks"] :]
    return RetrainWindows(policy, as_of_week, week_end(manifest, as_of_week), fit, calibration)


def recency_weights(
    weeks: npt.NDArray[np.integer], as_of_week: int, half_life_weeks: float
) -> npt.NDArray[np.float64]:
    """Weight 1 at T, halving every `half_life_weeks` of age."""
    age = as_of_week - np.asarray(weeks, dtype=np.float64)
    if (age < 0).any():
        raise ValueError("a row is newer than T")
    out: npt.NDArray[np.float64] = 0.5 ** (age / half_life_weeks)
    return out


def retrain_candidate(
    file_type: str,
    splits_root: Path,
    never_break_path: Path,
    out_dir: Path,
    train_cfg: TrainConfig,
    cfg: RetrainConfig,
    *,
    policy: RetrainPolicyName | None = None,
    as_of_week: int | None = None,
    lgbm_threads: int = 0,
) -> dict[str, Any]:
    t_start = time.time()
    pol: RetrainPolicyName = policy or cfg.policy
    manifest = load_manifest(splits_root, file_type)
    features = feature_columns(manifest)
    win = plan_retrain_windows(manifest, cfg, pol, as_of_week or cfg.as_of_week)
    if max(win.used) > win.as_of_week:  # pragma: no cover - guaranteed by plan_retrain_windows
        raise AssertionError("retraining would read a week after T")
    blog = log.bind(file_type=file_type, policy=pol, as_of_week=win.as_of_week)
    blog.info("retrain_start", fit=f"{win.fit[0]}-{win.fit[-1]}", calibration=win.calibration)

    fit = read_matrix(splits_root, file_type, win.fit, features, as_of=win.as_of)
    if len(fit.y) < cfg.min_fit_rows:
        raise ValueError(f"only {len(fit.y)} labelled rows to fit on (< {cfg.min_fit_rows})")
    weights = (
        recency_weights(fit.week, win.as_of_week, cfg.policies.recency_weighted["half_life_weeks"])
        if pol == "recency_weighted"
        else None
    )
    fit_counts = {
        "rows": len(fit.y),
        "malicious": int(fit.y.sum()),
        "unlabelled_dropped": fit.dropped_unlabelled,
    }
    params = {**train_cfg.lightgbm.params, "seed": train_cfg.seed, "num_threads": lgbm_threads}
    cats = lightgbm_categoricals(features, train_cfg)
    dataset = lgb.Dataset(
        fit.x,
        label=fit.y,
        weight=weights,
        feature_name=features,
        categorical_feature=cats,
        free_raw_data=True,
        params=params,
    )
    dataset.construct()
    del fit
    t0 = time.time()
    booster = lgb.train(params, dataset)
    lgbm_seconds = time.time() - t0
    del dataset

    cal = read_matrix(splits_root, file_type, win.calibration, features, as_of=win.as_of)
    raw_cal = np.asarray(booster.predict(cal.x), dtype=np.float64)
    calibrator = IsotonicCalibrator.fit(raw_cal, cal.y)
    thr = threshold_at_fpr(cal.y, raw_cal, train_cfg.target_fpr)
    nb = pq.read_table(never_break_path, columns=[LABEL_COLUMN, *features])
    nb_x = np.column_stack([nb.column(f).to_numpy() for f in features]).astype(np.float32)
    raw_nb = np.asarray(booster.predict(nb_x), dtype=np.float64)

    out_dir.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(out_dir / LGBM_FILE))
    calibrator.save(out_dir / LGBM_CAL_FILE)
    metrics: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "kind": "candidate",
        "file_type": file_type,
        "target_fpr": train_cfg.target_fpr,
        "primary_model": "lightgbm",
        "models": {
            "lightgbm": {
                "model": "lightgbm",
                "threshold_raw": thr,
                "threshold_probability": float(calibrator.predict(np.array([thr]))[0]),
                "calibration": rates_at_threshold(
                    cal.y, raw_cal, thr, train_cfg.confidence
                ).as_dict(),
                "never_break_flags": int((raw_nb > thr).sum()),
                "never_break_rows": int(raw_nb.size),
            }
        },
    }
    run_info: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "kind": "candidate",
        "file_type": file_type,
        "seed": train_cfg.seed,
        "feature_schema_version": manifest["feature_schema_version"],
        "data_fingerprint": manifest["data_fingerprint"],
        "git_commit": git_commit(REPO_ROOT),
        "policy": pol,
        "policy_params": getattr(cfg.policies, pol),
        "retrain_config": cfg.model_dump(mode="json"),
        "lightgbm_params": params,
        "categorical_features": cats,
        "features": features,
        "windows": {
            "as_of_week": win.as_of_week,
            "as_of": win.as_of.isoformat(),
            "fit": win.fit,
            "calibration": win.calibration,
        },
        "counts": {
            "fit": fit_counts,
            "calibration": {
                "rows": len(cal.y),
                "benign": int((cal.y == 0).sum()),
                "unlabelled_dropped": cal.dropped_unlabelled,
            },
        },
        "seconds": {"lightgbm": round(lgbm_seconds, 1), "total": round(time.time() - t_start, 1)},
        "versions": {"python": platform.python_version(), "lightgbm": lgb.__version__},
    }
    (out_dir / METRICS_FILE).write_text(json.dumps(metrics, indent=1) + "\n", encoding="utf-8")
    (out_dir / RUN_INFO_FILE).write_text(json.dumps(run_info, indent=1) + "\n", encoding="utf-8")
    blog.info(
        "retrain_done",
        fit_rows=fit_counts["rows"],
        threshold=thr,
        never_break_flags=metrics["models"]["lightgbm"]["never_break_flags"],
        seconds=run_info["seconds"]["total"],
    )
    return metrics
