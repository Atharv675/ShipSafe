"""Typed configuration: environment (.env) settings plus YAML files in configs/."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"


class Settings(BaseSettings):
    """Environment-level settings (read from env vars / .env)."""

    model_config = SettingsConfigDict(env_prefix="SENTINEL_", env_file=".env", extra="ignore")

    data_dir: Path = Field(default=REPO_ROOT / "data")
    log_level: str = "INFO"
    mlflow_uri: str | None = None  # default: SQLite registry under data_dir/mlflow
    pg_dsn: str | None = None  # PostgreSQL DSN for serving (secret: .env / compose only)

    @property
    def mlflow_dir(self) -> Path:
        return self.data_dir / "mlflow"

    @property
    def tracking_uri(self) -> str:
        return self.mlflow_uri or f"sqlite:///{(self.mlflow_dir / 'mlflow.db').as_posix()}"

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
    id_column: str
    dup_ignored_columns: list[str]
    feature_blocks: list[str]

    def zip_name(self, file_type: str, split: str) -> str:
        return self.zip_name_template.format(file_type=file_type, split=split)


class PlanAssumption(_Frozen):
    """The dataset subset docs/BUILD_PLAN.md assumed; compared against measurements."""

    file_type: str
    train: int
    test: int
    train_weeks: int
    test_weeks: int


class DataConfig(_Frozen):
    dataset: DatasetSpec
    expected: ExpectedFigures
    min_benign_test_files: int
    plan_assumption: PlanAssumption


class NeverBreakConfig(_Frozen):
    size: int = Field(gt=0)


class LateLabelConfig(_Frozen):
    distribution: Literal["uniform"]
    min_days: float = Field(ge=0)
    max_days: float = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> LateLabelConfig:
        if self.max_days < self.min_days:
            raise ValueError("late_labels.max_days must be >= min_days")
        return self


class ParquetConfig(_Frozen):
    compression: str
    compression_level: int
    row_group_size: int = Field(gt=0)


class CleanConfig(_Frozen):
    seed: int
    file_types: list[str]
    label_values: list[int]
    benign_label: int
    feature_prefix: str
    feature_index_digits: int = Field(gt=0)
    row_hash_digest_bytes: int = Field(ge=8, le=64)
    non_finite_fill: float
    never_break: NeverBreakConfig
    late_labels: LateLabelConfig
    parquet: ParquetConfig
    keep_intermediate: bool

    def feature_name(self, index: int) -> str:
        return f"{self.feature_prefix}{index:0{self.feature_index_digits}d}"


class TrainWeeks(_Frozen):
    train_end: int = Field(ge=2)
    calibration_weeks: int = Field(ge=1)
    eval_week: int
    as_of_week: int

    @model_validator(mode="after")
    def _ordered(self) -> TrainWeeks:
        if self.calibration_weeks >= self.train_end:
            raise ValueError("calibration_weeks must leave at least one fit week")
        if self.eval_week <= self.train_end:
            raise ValueError("eval_week must come after train_end")
        if self.as_of_week < self.eval_week:
            raise ValueError("as_of_week must not be before eval_week")
        return self

    @property
    def fit(self) -> list[int]:
        return list(range(1, self.train_end - self.calibration_weeks + 1))

    @property
    def calibration(self) -> list[int]:
        return list(range(self.train_end - self.calibration_weeks + 1, self.train_end + 1))


class LightGBMConfig(_Frozen):
    params: dict[str, Any]
    categorical_feature_indices: list[int]


class LogRegConfig(_Frozen):
    max_rows: int = Field(gt=0)
    signed_log1p: bool
    clip: float = Field(gt=0)
    C: float = Field(gt=0)
    max_iter: int = Field(gt=0)


class CalibrationConfig(_Frozen):
    reliability_bins: int = Field(gt=1)


class MlflowConfig(_Frozen):
    experiment: str
    model_name_template: str
    production_alias: str

    def model_name(self, file_type: str) -> str:
        return self.model_name_template.format(file_type=file_type.lower())


class TrainConfig(_Frozen):
    seed: int
    target_fpr: float = Field(gt=0, lt=1)
    weeks: TrainWeeks
    lightgbm: LightGBMConfig
    logreg: LogRegConfig
    calibration: CalibrationConfig
    confidence: float = Field(gt=0, lt=1)
    mlflow: MlflowConfig


class LineageConfig(_Frozen):
    batch_size: int = Field(gt=0)
    flush_seconds: float = Field(gt=0)
    queue_max: int = Field(gt=0)
    max_retries: int = Field(ge=0)


class OverridesConfig(_Frozen):
    refresh_seconds: float = Field(gt=0)


class ServingConfig(_Frozen):
    service_name: str
    file_types: list[str] = Field(min_length=1)
    model_alias: str
    poll_seconds: float = Field(gt=0)
    predict_threads: int = Field(ge=1)
    max_id_length: int = Field(gt=0)
    latency_samples: int = Field(gt=0)
    lineage: LineageConfig
    overrides: OverridesConfig


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return loaded


def load_data_config(path: Path | None = None) -> DataConfig:
    return DataConfig.model_validate(load_yaml(path or CONFIG_DIR / "data.yaml"))


def load_clean_config(path: Path | None = None) -> CleanConfig:
    return CleanConfig.model_validate(load_yaml(path or CONFIG_DIR / "clean.yaml"))


def load_train_config(path: Path | None = None) -> TrainConfig:
    return TrainConfig.model_validate(load_yaml(path or CONFIG_DIR / "train.yaml"))


def load_serving_config(path: Path | None = None) -> ServingConfig:
    return ServingConfig.model_validate(load_yaml(path or CONFIG_DIR / "serving.yaml"))
