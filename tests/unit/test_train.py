"""Stage 2 end to end on synthetic splits built by the real Stage 1 pipeline (fake vectoriser):
train -> bundle -> MLflow registry (alias production) -> load by alias -> predict."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest

from sentinel.config import (
    TrainConfig,
    TrainWeeks,
    load_clean_config,
    load_data_config,
    load_train_config,
)
from sentinel.data.pipeline import Layout, clean_file_type
from sentinel.data.splits import (
    compare_manifests,
    feature_columns,
    load_manifest,
    read_matrix,
    read_weeks,
)
from sentinel.models.baseline_report import build_baseline, render_baseline
from sentinel.models.calibration import IsotonicCalibrator
from sentinel.models.logreg import LogRegModel
from sentinel.models.train import (
    LGBM_FILE,
    METRICS_FILE,
    RUN_INFO_FILE,
    lightgbm_categoricals,
    original_index,
    plan_windows,
    train_baseline,
)

FT = "Win64"
DIM = 10
ROWS_PER_CLASS = 120
TRAIN_STARTS = ["2024-08-04", "2024-08-11", "2024-08-18", "2024-08-25", "2024-09-01"]
TEST_STARTS = ["2024-09-22", "2024-09-29"]


def fake_vectorize(rec: dict[str, Any]) -> npt.NDArray[np.float32]:
    return np.asarray(rec["vec"], dtype=np.float32)


def _week_records(start: str, split: str, seed: int) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    t0 = datetime.fromisoformat(start).replace(tzinfo=UTC)
    recs = []
    for label in (0, 1):
        for i in range(ROWS_PER_CLASS):
            vec = rng.normal(0, 1, DIM)
            vec[[0, 1, 7, 8]] += 1.6 * label  # signal
            vec[2:7] = rng.integers(0, 4, 5)  # categorical columns (thrember indices 2..6)
            t = t0 + timedelta(seconds=int(rng.integers(0, 6 * 86_400)))
            recs.append(
                {
                    "sha256": f"{split}{start}{label}{i}",
                    "label": label,
                    "first_submission_date": int(t.timestamp()),
                    "vec": vec.tolist(),
                }
            )
    return recs


def _write_zips(raw: Path) -> None:
    raw.mkdir(parents=True, exist_ok=True)
    for split, starts, seed0 in (("train", TRAIN_STARTS, 100), ("test", TEST_STARTS, 200)):
        with zipfile.ZipFile(raw / f"{FT}_{split}.zip", "w") as zf:
            for k, start in enumerate(starts):
                end = (datetime.fromisoformat(start) + timedelta(days=6)).date().isoformat()
                lines = (
                    "\n".join(json.dumps(r) for r in _week_records(start, split, seed0 + k)) + "\n"
                )
                zf.writestr(
                    zipfile.ZipInfo(f"{start}_{end}_{FT}_{split}.jsonl", (2024, 1, 1, 0, 0, 0)),
                    lines,
                )


def _cfg(**weeks: int) -> TrainConfig:
    base = load_train_config()
    lgbm = base.lightgbm.model_copy(
        update={
            "params": {
                **base.lightgbm.params,
                "num_iterations": 25,
                "num_leaves": 7,
                "min_data_in_leaf": 5,
                "learning_rate": 0.1,
            }
        }
    )
    w = {"train_end": 4, "calibration_weeks": 1, "eval_week": 5, "as_of_week": 5, **weeks}
    return base.model_copy(
        update={
            "target_fpr": 0.02,
            "weeks": TrainWeeks(**w),
            "lightgbm": lgbm,
            "logreg": base.logreg.model_copy(update={"max_rows": 400}),
        }
    )


@pytest.fixture(scope="module")
def splits(tmp_path_factory: pytest.TempPathFactory) -> Layout:
    root = tmp_path_factory.mktemp("stage2")
    _write_zips(root / "raw")
    layout = Layout(root / "splits", root / "clean", FT)
    clean_cfg = load_clean_config().model_copy(
        update={"never_break": load_clean_config().never_break.model_copy(update={"size": 20})}
    )
    clean_file_type(
        FT, root / "raw", layout, load_data_config(), clean_cfg, vectorize=fake_vectorize, dim=DIM
    )
    return layout


@pytest.fixture(scope="module")
def trained(
    splits: Layout, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, dict[str, Any]]:
    out = tmp_path_factory.mktemp("bundle")
    result = train_baseline(FT, splits.splits_root, splits.never_break, out, _cfg(), lgbm_threads=1)
    return out, result


def test_windows_are_train_weeks_only(splits: Layout) -> None:
    m = load_manifest(splits.splits_root, FT)
    w = plan_windows(m, _cfg())
    assert (w.fit, w.calibration, w.eval_week, w.test) == ([1, 2, 3], [4], 5, [6, 7])
    with pytest.raises(ValueError, match="TRAIN weeks"):
        plan_windows(m, _cfg(eval_week=6, as_of_week=6))  # 6 is a test week
    with pytest.raises(ValueError, match="test week"):
        plan_windows(m, _cfg(as_of_week=6))
    with pytest.raises(ValueError):
        TrainWeeks(train_end=4, calibration_weeks=4, eval_week=5, as_of_week=5)
    with pytest.raises(ValueError):
        TrainWeeks(train_end=4, calibration_weeks=1, eval_week=4, as_of_week=5)
    with pytest.raises(ValueError):
        TrainWeeks(train_end=4, calibration_weeks=1, eval_week=5, as_of_week=4)


def test_bundle_and_metrics(trained: tuple[Path, dict[str, Any]]) -> None:
    out, result = trained
    for f in (
        LGBM_FILE,
        METRICS_FILE,
        RUN_INFO_FILE,
        "lightgbm_calibration.npz",
        "logreg.npz",
        "logreg_calibration.npz",
    ):
        assert (out / f).is_file()
    lg, lr = result["models"]["lightgbm"], result["models"]["logreg"]
    for m in (lg, lr):
        assert m["calibration"]["fpr"] <= result["target_fpr"]  # threshold set on calibration weeks
        assert m["eval_week"]["week"] == 5
        assert m["eval_week"]["recall"] > 0.5 and m["eval_week"]["auc"] > 0.9
        assert [r["week"] for r in m["test_weeks_report_only"]["per_week"]] == [6, 7]
        assert m["never_break_rows"] == 20
        assert 0 <= m["eval_week"]["ece"] <= 1
    info = json.loads((out / RUN_INFO_FILE).read_text())
    assert info["windows"]["fit"] == [1, 2, 3] and info["seed"] == _cfg().seed
    assert info["categorical_features"] == ["f_0002", "f_0003", "f_0004", "f_0005", "f_0006"]
    assert info["feature_schema_version"] and len(info["data_fingerprint"]) == 64


def test_late_labels_are_left_out_of_fit_and_calibration(
    trained: tuple[Path, dict[str, Any]],
) -> None:
    out, _ = trained
    c = json.loads((out / RUN_INFO_FILE).read_text())["counts"]
    # as_of = end of week 5: week 4 rows need 7-28 days, so most are not labelled yet
    assert c["calibration"]["unlabelled_dropped"] > 0
    assert c["calibration"]["rows"] > 0  # short delays are already labelled
    assert c["calibration"]["rows"] + c["calibration"]["unlabelled_dropped"] == 2 * ROWS_PER_CLASS
    # week 1 is > 28 days before as_of: every label is known (regression: Parquet stores
    # timestamp[s] as ms; a unit mix-up once made every row look unlabelled)
    assert c["fit"]["rows"] + c["fit"]["unlabelled_dropped"] == 3 * 2 * ROWS_PER_CLASS
    assert c["fit"]["rows"] >= 2 * 2 * ROWS_PER_CLASS
    assert c["eval"]["rows"] == 2 * ROWS_PER_CLASS  # evaluation uses ground truth


def test_training_is_deterministic(
    splits: Layout, trained: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    out1, r1 = trained
    r2 = train_baseline(
        FT, splits.splits_root, splits.never_break, tmp_path, _cfg(), lgbm_threads=1
    )
    assert (out1 / LGBM_FILE).read_bytes() == (tmp_path / LGBM_FILE).read_bytes()
    assert r1 == r2


def test_read_matrix_matches_dataframe_reader(splits: Layout) -> None:
    m = load_manifest(splits.splits_root, FT)
    feats = feature_columns(m)
    mat = read_matrix(splits.splits_root, FT, [2, 1], feats)
    df = read_weeks(splits.splits_root, FT, [1, 2])
    assert np.array_equal(mat.x, df[feats].to_numpy(np.float32))
    assert mat.ids == list(df["sha256"]) and np.array_equal(mat.week, df["week"].to_numpy())
    sub = mat.take(np.array([0, 3]))
    assert sub.ids == [mat.ids[0], mat.ids[3]]
    with pytest.raises(ValueError):
        read_matrix(splits.splits_root, FT, [], feats)


def test_compare_manifests_ignores_bytes_but_not_content(splits: Layout) -> None:
    m = load_manifest(splits.splits_root, FT)
    rebuilt = json.loads(json.dumps(m))
    for w in rebuilt["weeks"]:
        w["sha256"] = "0" * 64  # e.g. a newer pyarrow writes different bytes
    rebuilt["generated_at"], rebuilt["git_commit"] = "later", "other"
    assert compare_manifests(m, rebuilt) == []
    rebuilt["weeks"][0]["rows"] += 1
    rebuilt["feature_schema_version"] = "x"
    rebuilt["never_break"]["rows"] = 0
    diffs = compare_manifests(m, rebuilt)
    assert any("week 1" in d for d in diffs) and "feature_schema_version differs" in diffs
    assert "never_break rows differ" in diffs
    assert "number of weeks differs" in compare_manifests(m, {**m, "weeks": m["weeks"][:-1]})


def test_components_round_trip(tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    raw, y = rng.uniform(0, 1, 500), rng.integers(0, 2, 500)
    cal = IsotonicCalibrator.fit(raw, y)
    cal.save(tmp_path / "c.npz")
    assert np.array_equal(
        IsotonicCalibrator.load(tmp_path / "c.npz").predict(raw), cal.predict(raw)
    )
    x = rng.normal(0, 1, (300, 4)).astype(np.float32)
    x[:, 3] = 0  # constant column
    lab = (x[:, 0] > 0).astype(int)
    lr = LogRegModel.fit(x, lab, load_train_config().logreg, seed=1)
    lr.save(tmp_path / "lr.npz")
    back = LogRegModel.load(tmp_path / "lr.npz")
    assert np.allclose(back.predict(x), lr.predict(x)) and back.converged
    assert ((lr.predict(x) > 0.5) == lab.astype(bool)).mean() > 0.9


def test_categorical_mapping() -> None:
    cfg = load_train_config()
    assert original_index("f_0701") == 701
    assert lightgbm_categoricals(["f_0001", "f_0002", "f_0701", "f_0800"], cfg) == [
        "f_0002",
        "f_0701",
    ]
    with pytest.raises(ValueError):
        original_index("label")


def test_register_load_by_alias_and_predict(
    trained: tuple[Path, dict[str, Any]], splits: Layout, tmp_path: Path
) -> None:
    from sentinel.models.registry import load_production, register_bundle

    out, result = trained
    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    cfg = _cfg()
    reg = register_bundle(out, uri, tmp_path / "artifacts", cfg)
    assert (reg["model_name"], reg["version"], reg["alias"]) == (
        "sentinel-win64",
        "1",
        "production",
    )
    model = load_production(uri, cfg, FT)
    df = read_weeks(splits.splits_root, FT, [5])
    pred = model.predict(df)
    assert list(pred.columns) == ["raw_score", "probability", "verdict"]
    thr = result["models"]["lightgbm"]["threshold_raw"]
    assert (pred["verdict"] == (pred["raw_score"] > thr)).all()
    flagged_mal = int((pred["verdict"] & (df["label"] == 1)).sum())
    assert flagged_mal == result["models"]["lightgbm"]["eval_week"]["tp"]
    with pytest.raises(ValueError, match="missing"):
        model.predict(df.drop(columns=["f_0000"]))
    from mlflow import MlflowClient

    tags = MlflowClient(tracking_uri=uri).get_model_version("sentinel-win64", "1").tags
    assert (
        tags["feature_schema_version"]
        == json.loads((out / RUN_INFO_FILE).read_text())["feature_schema_version"]
    )


def test_baseline_report(trained: tuple[Path, dict[str, Any]], tmp_path: Path) -> None:
    out, _ = trained
    d = tmp_path / "models" / FT
    d.mkdir(parents=True)
    for f in (METRICS_FILE, RUN_INFO_FILE):
        (d / f).write_bytes((out / f).read_bytes())
    report = tmp_path / "BASELINE.md"
    assert build_baseline(tmp_path / "models", report) == [FT]
    md = report.read_text()
    assert "| LightGBM (v1) |" in md and "| Logistic regression |" in md
    assert "Recall difference (LightGBM minus LR)" in md and "### Test weeks 6-7 (report" in md
    assert "No trained models yet." in render_baseline({})
    assert build_baseline(tmp_path / "nope", report) == []


def test_safe_extract_refuses_unexpected_files(
    trained: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    from scripts.register_model import safe_extract

    out, _ = trained
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as zf:
        for f in (METRICS_FILE, RUN_INFO_FILE, LGBM_FILE):
            zf.write(out / f, f"run/{f}")
    assert {p.name for p in safe_extract(good, tmp_path / "x").iterdir()} == {
        METRICS_FILE,
        RUN_INFO_FILE,
        LGBM_FILE,
    }
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("evil.py", "print('x')")
    with pytest.raises(SystemExit):
        safe_extract(bad, tmp_path / "y")
