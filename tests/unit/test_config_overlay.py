"""Stage 10: the demo config overlay (configs/demo/) changes only the demo, never the real run."""

from __future__ import annotations

import pytest

from sentinel import config as c


def test_deep_merge_merges_mappings_and_replaces_lists() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 2}
    out = c.deep_merge(base, {"a": {"y": [9]}, "c": 3})
    assert out == {"a": {"x": 1, "y": [9]}, "b": 2, "c": 3}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 2}  # the base is not modified


def test_without_overlay_the_real_configs_load_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(c.OVERLAY_ENV, raising=False)
    assert c.load_serving_config().file_types == ["Win64", "Win32"]
    assert c.load_gate_policy().operating_fpr == 0.001
    assert c.load_train_config().lightgbm.params["num_iterations"] == 1000


def test_demo_overlay_changes_only_what_it_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(c.OVERLAY_ENV, "demo")
    gate = c.load_gate_policy()
    assert (gate.file_types, gate.operating_fpr, gate.policy_version) == (
        ["PDF"],
        0.01,
        "1.0.0-demo",
    )
    assert gate.never_break_hits_allowed == 0  # untouched keys keep the real values
    train = c.load_train_config()
    assert train.lightgbm.params["num_iterations"] == 150
    assert train.lightgbm.params["learning_rate"] == 0.05
    assert c.load_serving_config().file_types == ["PDF"]
    assert c.load_monitoring_config().dashboard.ml.mlflow_ui_url.endswith(":15000")
    assert c.load_rollout_config() == c.RolloutConfig.model_validate(
        c._read_mapping(c.CONFIG_DIR / "rollout.yaml")
    )  # no overlay file -> the base file


def test_overlay_name_must_be_a_plain_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(c.OVERLAY_ENV, "../etc")
    with pytest.raises(ValueError, match="folder name"):
        c.load_gate_policy()
