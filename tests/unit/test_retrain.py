"""Stage 4 retraining at simulated time T: windows, policies, and NO label from after T."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import sentinel.models.retrain as retrain_mod
from sentinel.config import RetrainConfig, load_retrain_config
from sentinel.data.pipeline import Layout
from sentinel.data.schema import ID_COLUMN, LABEL_AVAILABLE_COLUMN
from sentinel.data.splits import Matrix, load_manifest, week_end
from sentinel.models.retrain import plan_retrain_windows, recency_weights, retrain_candidate
from sentinel.models.train import LGBM_CAL_FILE, LGBM_FILE, METRICS_FILE, RUN_INFO_FILE
from tests.unit.test_train import FT, _cfg, splits  # noqa: F401  (pytest fixture)

# synthetic dataset: train weeks 1-5, test weeks 6-7


def _rcfg(**over: Any) -> RetrainConfig:
    base: dict[str, Any] = {
        "as_of_week": 7,
        "calibration_weeks": 2,
        "min_fit_rows": 100,
        "policy": "all_history",
        "policies": {
            "all_history": {},
            "last_n_weeks": {"n_weeks": 2},
            "recency_weighted": {"half_life_weeks": 2.0},
        },
        "mlflow_alias": "candidate",
    }
    base.update(over)
    return RetrainConfig.model_validate(base)


def test_config_file_is_valid() -> None:
    cfg = load_retrain_config()
    assert cfg.mlflow_alias == "candidate"
    assert cfg.policy in {"all_history", "last_n_weeks", "recency_weighted"}


def test_windows_per_policy_never_pass_t(splits: Layout) -> None:  # noqa: F811
    m = load_manifest(splits.splits_root, FT)
    cfg = _rcfg()
    w = plan_retrain_windows(m, cfg, "all_history", 7)
    assert (w.fit, w.calibration) == ([1, 2, 3, 4, 5], [6, 7])  # a test week may be used
    assert w.as_of == week_end(m, 7)
    assert plan_retrain_windows(m, cfg, "last_n_weeks", 7).fit == [4, 5]
    assert plan_retrain_windows(m, cfg, "recency_weighted", 7).fit == [1, 2, 3, 4, 5]
    early = plan_retrain_windows(m, cfg, "all_history", 5)
    assert max(early.used) == 5
    with pytest.raises(ValueError, match="not in this dataset"):
        plan_retrain_windows(m, cfg, "all_history", 99)
    with pytest.raises(ValueError, match="no weeks to fit"):
        plan_retrain_windows(m, cfg, "all_history", 2)


def test_recency_weights() -> None:
    w = recency_weights(np.array([7, 5, 3, 1]), 7, 2.0)
    assert w.tolist() == [1.0, 0.5, 0.25, 0.125]
    with pytest.raises(ValueError, match="newer than T"):
        recency_weights(np.array([8]), 7, 2.0)


@pytest.mark.parametrize("policy", ["all_history", "last_n_weeks", "recency_weighted"])
def test_retraining_never_uses_a_label_available_after_t(
    splits: Layout,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    calls: list[tuple[list[int], datetime | None, Matrix]] = []
    real = retrain_mod.read_matrix

    def spy(root: Path, ft: str, weeks: Any, features: Any, as_of: Any = None) -> Matrix:
        m = real(root, ft, weeks, features, as_of=as_of)
        calls.append((sorted(weeks), as_of, m))
        return m

    monkeypatch.setattr(retrain_mod, "read_matrix", spy)
    t = 6
    out = tmp_path / "cand"
    result = retrain_candidate(
        FT,
        splits.splits_root,
        splits.never_break,
        out,
        _cfg(),
        _rcfg(as_of_week=t),
        policy=policy,  # type: ignore[arg-type]
        lgbm_threads=1,
    )
    as_of = week_end(load_manifest(splits.splits_root, FT), t)
    # every read is capped at T, both in weeks and in label time
    assert calls
    avail: dict[str, int] = {}
    for w in range(1, 8):
        tb = pq.read_table(splits.week_file(w), columns=[ID_COLUMN, LABEL_AVAILABLE_COLUMN])
        secs = tb.column(LABEL_AVAILABLE_COLUMN).cast(pa.timestamp("s", tz="UTC")).cast(pa.int64())
        avail.update(zip(tb.column(ID_COLUMN).to_pylist(), secs.to_pylist(), strict=True))
    for weeks, cut, m in calls:
        assert max(weeks) <= t
        assert cut == as_of
        assert m.ids, "a read returned no rows"
        assert max(avail[i] for i in m.ids) <= int(as_of.timestamp())
    assert any(m.dropped_unlabelled > 0 for _, _, m in calls)  # late labels really bite here
    assert 7 not in {w for weeks, _, _ in calls for w in weeks}

    info = json.loads((out / RUN_INFO_FILE).read_text())
    assert info["kind"] == "candidate"
    assert info["policy"] == policy
    assert info["windows"]["as_of_week"] == t
    for f in (LGBM_FILE, LGBM_CAL_FILE, METRICS_FILE):
        assert (out / f).exists()
    lg = result["models"]["lightgbm"]
    assert 0 <= lg["threshold_raw"] <= 1
    assert lg["never_break_rows"] == 20


def test_too_few_rows_is_refused(splits: Layout, tmp_path: Path) -> None:  # noqa: F811
    with pytest.raises(ValueError, match="labelled rows to fit on"):
        retrain_candidate(
            FT,
            splits.splits_root,
            splits.never_break,
            tmp_path,
            _cfg(),
            _rcfg(min_fit_rows=10**6),
            lgbm_threads=1,
        )
