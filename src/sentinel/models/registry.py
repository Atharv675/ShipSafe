"""MLflow: package a run bundle as a pyfunc model, log params/metrics/lineage, register it.

The registered model returns, per row: raw_score, probability (isotonic-calibrated) and
verdict (raw_score > fixed-FPR threshold). Serving (Stage 3) loads it by alias.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import mlflow
import mlflow.pyfunc
import numpy as np
import pandas as pd
from mlflow import MlflowClient
from mlflow.pyfunc.model import PythonModel

from sentinel.config import TrainConfig
from sentinel.logging import get_logger
from sentinel.models.calibration import IsotonicCalibrator
from sentinel.models.train import (
    LGBM_CAL_FILE,
    LGBM_FILE,
    METRICS_FILE,
    RUN_INFO_FILE,
)

META_FILE = "model_meta.json"
OUTPUT_COLUMNS = ("raw_score", "probability", "verdict")

log = get_logger(__name__)


class SentinelModel(PythonModel):
    """LightGBM + isotonic calibrator + fixed-FPR threshold, as one MLflow model."""

    def load_context(self, context: Any) -> None:
        self.booster = lgb.Booster(model_file=context.artifacts["lightgbm"])
        self.calibrator = IsotonicCalibrator.load(Path(context.artifacts["calibration"]))
        self.meta: dict[str, Any] = json.loads(
            Path(context.artifacts["meta"]).read_text(encoding="utf-8")
        )

    def predict(self, context: Any, model_input: pd.DataFrame, params: Any = None) -> pd.DataFrame:
        features: list[str] = self.meta["features"]
        missing = [f for f in features if f not in model_input.columns]
        if missing:
            raise ValueError(f"missing {len(missing)} feature columns, e.g. {missing[:3]}")
        x = model_input[features].to_numpy(dtype=np.float32)
        raw = np.asarray(self.booster.predict(x), dtype=np.float64)
        return pd.DataFrame(
            {
                "raw_score": raw,
                "probability": self.calibrator.predict(raw),
                "verdict": raw > float(self.meta["threshold_raw"]),
            }
        )


def _flat_metrics(model_metrics: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {"threshold_raw": float(model_metrics["threshold_raw"])}
    for section in ("calibration", "eval_week"):
        for k, v in model_metrics[section].items():
            if isinstance(v, int | float) and not isinstance(v, bool) and v == v:  # skip NaN
                out[f"{section}.{k}"] = float(v)
    pooled = model_metrics.get("test_weeks_report_only", {}).get("pooled", {})
    for k, v in pooled.items():
        if isinstance(v, int | float) and not isinstance(v, bool) and v == v:
            out[f"test_report_only.{k}"] = float(v)
    out["never_break_flags"] = float(model_metrics["never_break_flags"])
    return out


def _flat_params(run_info: dict[str, Any], model: str) -> dict[str, str]:
    w = run_info["windows"]
    p = {
        "file_type": run_info["file_type"],
        "model": model,
        "seed": run_info["seed"],
        "fit_weeks": f"{w['fit'][0]}-{w['fit'][-1]}",
        "calibration_weeks": f"{w['calibration'][0]}-{w['calibration'][-1]}",
        "eval_week": w["eval_week"],
        "as_of": w["as_of"],
        "target_fpr": run_info["train_config"]["target_fpr"],
        "n_features": len(run_info["features"]),
    }
    if model == "lightgbm":
        p.update({f"lgbm.{k}": v for k, v in run_info["lightgbm_params"].items()})
    else:
        p.update({f"logreg.{k}": v for k, v in run_info["train_config"]["logreg"].items()})
    return {k: str(v) for k, v in p.items()}


def register_bundle(
    bundle_dir: Path,
    tracking_uri: str,
    artifact_root: Path,
    cfg: TrainConfig,
) -> dict[str, Any]:
    """Log both models' runs; register LightGBM as the dataset's v1 and alias it `production`."""
    metrics = json.loads((bundle_dir / METRICS_FILE).read_text(encoding="utf-8"))
    run_info = json.loads((bundle_dir / RUN_INFO_FILE).read_text(encoding="utf-8"))
    file_type = run_info["file_type"]
    name = cfg.mlflow.model_name(file_type)
    mlflow.set_tracking_uri(tracking_uri)
    exp = mlflow.get_experiment_by_name(cfg.mlflow.experiment)
    exp_id = (
        exp.experiment_id
        if exp
        else mlflow.create_experiment(
            cfg.mlflow.experiment, artifact_location=artifact_root.as_uri()
        )
    )
    tags = {
        "file_type": file_type,
        "feature_schema_version": run_info["feature_schema_version"],
        "data_fingerprint": run_info["data_fingerprint"],
        "git_commit": run_info["git_commit"],
        "seed": str(run_info["seed"]),
    }
    result: dict[str, Any] = {"model_name": name}
    for model in ("logreg", "lightgbm"):
        with mlflow.start_run(experiment_id=exp_id, run_name=f"{file_type}-{model}") as run:
            mlflow.log_params(_flat_params(run_info, model))
            mlflow.log_metrics(_flat_metrics(metrics["models"][model]))
            mlflow.set_tags({**tags, "model": model})
            mlflow.log_artifacts(str(bundle_dir), artifact_path="bundle")
            result[f"{model}_run_id"] = run.info.run_id
            if model != "lightgbm":
                continue
            meta = {
                "file_type": file_type,
                "features": run_info["features"],
                "feature_schema_version": run_info["feature_schema_version"],
                "threshold_raw": metrics["models"]["lightgbm"]["threshold_raw"],
                "threshold_probability": metrics["models"]["lightgbm"]["threshold_probability"],
                "target_fpr": metrics["target_fpr"],
                "data_fingerprint": run_info["data_fingerprint"],
            }
            meta_path = bundle_dir / META_FILE
            meta_path.write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
            info = mlflow.pyfunc.log_model(
                name="model",
                python_model=SentinelModel(),
                artifacts={
                    "lightgbm": str(bundle_dir / LGBM_FILE),
                    "calibration": str(bundle_dir / LGBM_CAL_FILE),
                    "meta": str(meta_path),
                },
                registered_model_name=name,
                pip_requirements=[
                    f"lightgbm=={lgb.__version__}",
                    f"numpy=={np.__version__}",
                    f"pandas=={pd.__version__}",
                    f"mlflow=={mlflow.__version__}",
                ],
            )
            version = str(info.registered_model_version)
    client = MlflowClient(tracking_uri=tracking_uri)
    for k, v in tags.items():
        client.set_model_version_tag(name, version, k, v)
    client.set_registered_model_alias(name, cfg.mlflow.production_alias, version)
    result.update({"version": version, "alias": cfg.mlflow.production_alias})
    log.info("registered", **result)
    return result


def register_candidate(
    bundle_dir: Path,
    tracking_uri: str,
    artifact_root: Path,
    cfg: TrainConfig,
    alias: str,
) -> dict[str, Any]:
    """Register a Stage 4 retrained bundle as a NEW version of sentinel-<type> under `alias`.

    The production alias is never touched here (promotion is the gate's job, Stage 5)."""
    metrics = json.loads((bundle_dir / METRICS_FILE).read_text(encoding="utf-8"))
    run_info = json.loads((bundle_dir / RUN_INFO_FILE).read_text(encoding="utf-8"))
    if run_info.get("kind") != "candidate":
        raise ValueError("not a candidate bundle (run_info.kind != 'candidate')")
    if alias == cfg.mlflow.production_alias:
        raise ValueError("a candidate must not be registered under the production alias")
    file_type = run_info["file_type"]
    name = cfg.mlflow.model_name(file_type)
    lg = metrics["models"]["lightgbm"]
    w = run_info["windows"]
    mlflow.set_tracking_uri(tracking_uri)
    exp = mlflow.get_experiment_by_name(cfg.mlflow.experiment)
    exp_id = (
        exp.experiment_id
        if exp
        else mlflow.create_experiment(
            cfg.mlflow.experiment, artifact_location=artifact_root.as_uri()
        )
    )
    tags = {
        "file_type": file_type,
        "kind": "candidate",
        "policy": run_info["policy"],
        "as_of_week": str(w["as_of_week"]),
        "feature_schema_version": run_info["feature_schema_version"],
        "data_fingerprint": run_info["data_fingerprint"],
        "git_commit": run_info["git_commit"],
        "seed": str(run_info["seed"]),
    }
    params = {
        "file_type": file_type,
        "policy": run_info["policy"],
        "policy_params": json.dumps(run_info["policy_params"], sort_keys=True),
        "as_of_week": str(w["as_of_week"]),
        "fit_weeks": f"{w['fit'][0]}-{w['fit'][-1]}",
        "calibration_weeks": f"{w['calibration'][0]}-{w['calibration'][-1]}",
        "target_fpr": str(metrics["target_fpr"]),
        **{f"lgbm.{k}": str(v) for k, v in run_info["lightgbm_params"].items()},
    }
    flat = {"threshold_raw": float(lg["threshold_raw"])}
    flat |= {
        f"calibration.{k}": float(v)
        for k, v in lg["calibration"].items()
        if isinstance(v, int | float) and not isinstance(v, bool) and v == v
    }
    flat["never_break_flags"] = float(lg["never_break_flags"])
    with mlflow.start_run(
        experiment_id=exp_id, run_name=f"{file_type}-candidate-T{w['as_of_week']}-{tags['policy']}"
    ) as run:
        mlflow.log_params(params)
        mlflow.log_metrics(flat)
        mlflow.set_tags(tags)
        mlflow.log_artifacts(str(bundle_dir), artifact_path="bundle")
        meta = {
            "file_type": file_type,
            "features": run_info["features"],
            "feature_schema_version": run_info["feature_schema_version"],
            "threshold_raw": lg["threshold_raw"],
            "threshold_probability": lg["threshold_probability"],
            "target_fpr": metrics["target_fpr"],
            "data_fingerprint": run_info["data_fingerprint"],
        }
        meta_path = bundle_dir / META_FILE
        meta_path.write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
        info = mlflow.pyfunc.log_model(
            name="model",
            python_model=SentinelModel(),
            artifacts={
                "lightgbm": str(bundle_dir / LGBM_FILE),
                "calibration": str(bundle_dir / LGBM_CAL_FILE),
                "meta": str(meta_path),
            },
            registered_model_name=name,
            pip_requirements=[
                f"lightgbm=={lgb.__version__}",
                f"numpy=={np.__version__}",
                f"pandas=={pd.__version__}",
                f"mlflow=={mlflow.__version__}",
            ],
        )
        run_id = run.info.run_id
    version = str(info.registered_model_version)
    client = MlflowClient(tracking_uri=tracking_uri)
    for k, v in tags.items():
        client.set_model_version_tag(name, version, k, v)
    client.set_registered_model_alias(name, alias, version)
    result = {"model_name": name, "version": version, "alias": alias, "run_id": run_id}
    log.info("candidate_registered", **result)
    return result


def load_production(tracking_uri: str, cfg: TrainConfig, file_type: str) -> Any:
    """Load the dataset's production model by alias (what serving does in Stage 3)."""
    mlflow.set_tracking_uri(tracking_uri)
    return mlflow.pyfunc.load_model(
        f"models:/{cfg.mlflow.model_name(file_type)}@{cfg.mlflow.production_alias}"
    )
