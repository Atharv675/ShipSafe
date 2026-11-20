"""Golden guard: v1 recall at the fixed 0.1% FPR on week K+1 must not silently change."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from sentinel.config import load_train_config
from sentinel.models.evaluate import wilson_interval

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden" / "v1_metrics.json"
MODELS_DOCS = ROOT / "docs" / "models"


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


GOLD = _load(GOLDEN)
TYPES = sorted(GOLD["models"])


def test_golden_covers_every_registered_dataset() -> None:
    registered = sorted(p.name for p in MODELS_DOCS.iterdir() if (p / "metrics.json").exists())
    assert registered == TYPES


def test_golden_target_fpr_matches_config() -> None:
    cfg = load_train_config(ROOT / "configs" / "train.yaml")
    assert GOLD["target_fpr"] == cfg.target_fpr
    assert all(g["week"] == cfg.weeks.eval_week for g in GOLD["models"].values())


@pytest.mark.parametrize("file_type", TYPES)
def test_v1_recall_at_fixed_fpr_matches_golden(file_type: str) -> None:
    gold = GOLD["models"][file_type]
    metrics = _load(MODELS_DOCS / file_type / "metrics.json")
    run_info = _load(MODELS_DOCS / file_type / "run_info.json")
    assert metrics["primary_model"] == "lightgbm"
    assert metrics["target_fpr"] == GOLD["target_fpr"]
    ev = metrics["models"]["lightgbm"]["eval_week"]

    assert ev["week"] == gold["week"]
    assert (ev["tp"], ev["positives"], ev["fp"], ev["negatives"]) == (
        gold["tp"],
        gold["positives"],
        gold["fp"],
        gold["negatives"],
    )
    assert math.isclose(ev["threshold"], gold["threshold_raw"], rel_tol=1e-12)
    assert ev["recall"] >= gold["recall"] - GOLD["recall_tolerance"]
    assert math.isclose(ev["recall"], ev["tp"] / ev["positives"], rel_tol=1e-12)
    # the interval in the report comes from the one shared evaluate module
    confidence = load_train_config(ROOT / "configs" / "train.yaml").confidence
    lo, hi = wilson_interval(ev["tp"], ev["positives"], confidence)
    assert ev["recall_ci"] == pytest.approx([lo, hi], rel=1e-12)
    # lineage: same data and feature schema as when the golden numbers were measured
    assert run_info["data_fingerprint"] == gold["data_fingerprint"]
    assert run_info["feature_schema_version"] == gold["feature_schema_version"]
