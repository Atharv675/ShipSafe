import importlib
from pathlib import Path

import pytest

import sentinel
from sentinel.config import Settings, load_data_config

SUBPACKAGES = ["data", "features", "models", "gate", "serving", "replay", "rollout", "monitoring"]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackages_import(name: str) -> None:
    importlib.import_module(f"sentinel.{name}")


def test_version() -> None:
    assert sentinel.__version__ == "0.1.0"


def test_data_config_is_consistent() -> None:
    cfg = load_data_config()
    assert cfg.dataset.hf_repo_id == "joyce8/EMBER2024"
    assert cfg.dataset.zip_name("PDF", "test") == "PDF_test.zip"
    assert set(cfg.expected.per_file_type) == set(cfg.dataset.file_types)
    for counts in cfg.expected.per_file_type.values():
        assert counts.train == counts.weekly * cfg.expected.train_weeks
        assert counts.test == counts.weekly * cfg.expected.test_weeks


def test_settings_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path))
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.data_dir == tmp_path
    assert s.raw_dir == tmp_path / "raw"
    assert s.clean_dir == tmp_path / "clean"
    assert s.splits_dir == tmp_path / "splits"


def test_logging_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    from sentinel.logging import configure_logging, get_logger

    configure_logging("INFO")
    get_logger("t").info("hello", rows=3)
    err = capsys.readouterr().err
    assert '"event": "hello"' in err and '"rows": 3' in err
