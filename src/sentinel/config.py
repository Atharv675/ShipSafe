"""Typed configuration: environment (.env) settings plus YAML files in configs/."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"


class Settings(BaseSettings):
    """Environment-level settings (read from env vars / .env)."""

    model_config = SettingsConfigDict(env_prefix="SENTINEL_", env_file=".env", extra="ignore")

    data_dir: Path = Field(default=REPO_ROOT / "data")
    log_level: str = "INFO"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def clean_dir(self) -> Path:
        return self.data_dir / "clean"

    @property
    def splits_dir(self) -> Path:
        return self.data_dir / "splits"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExpectedCounts(_Frozen):
    weekly: int
    train: int
    test: int


class ExpectedFigures(_Frozen):
    train_weeks: int
    test_weeks: int
    per_file_type: dict[str, ExpectedCounts]


class DatasetSpec(_Frozen):
    name: str
    hf_repo_id: str
    hf_repo_type: str
    zip_name_template: str
    member_regex: str
    file_types: list[str]
    splits: list[str]
    metadata_columns: list[str]
    label_column: str
    time_column: str
    feature_blocks: list[str]

    def zip_name(self, file_type: str, split: str) -> str:
        return self.zip_name_template.format(file_type=file_type, split=split)


class DataConfig(_Frozen):
    dataset: DatasetSpec
    expected: ExpectedFigures
    min_benign_test_files: int


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return loaded


def load_data_config(path: Path | None = None) -> DataConfig:
    return DataConfig.model_validate(load_yaml(path or CONFIG_DIR / "data.yaml"))
