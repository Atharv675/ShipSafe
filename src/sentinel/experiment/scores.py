"""The experiment's input: every model's scores on the same files, merged from Kaggle exports.

Several Kaggle runs (split by retraining policy) are merged here. Each run must have scored
exactly the same rows (same file ids, weeks, labels and label times, same data fingerprint),
and a model scored by two runs (v1 always is) must have identical scores, or the merge stops.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow.parquet as pq

from sentinel.data.schema import ID_COLUMN, LABEL_AVAILABLE_COLUMN, LABEL_COLUMN, WEEK_COLUMN
from sentinel.data.splits import week_end
from sentinel.experiment.export import (
    HIST_FILE,
    INFO_FILE,
    MODELS_FILE,
    NEVER_BREAK_FILE,
    SCORES_FILE,
    V1_ID,
    prob_col,
    raw_col,
)

F64 = npt.NDArray[np.float64]


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    policy: str
    as_of_week: int
    threshold: float  # shipped threshold on the RAW score (flag if raw > threshold)
    fit_weeks: tuple[int, ...]
    calibration_weeks: tuple[int, ...]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ModelInfo:
        return cls(
            str(d["model_id"]),
            str(d["policy"]),
            int(d["as_of_week"]),
            float(d["threshold_raw"]),
            tuple(int(w) for w in d["fit_weeks"]),
            tuple(int(w) for w in d["calibration_weeks"]),
        )


@dataclass(frozen=True)
class ScoreTable:
    file_ids: npt.NDArray[np.str_]
    week: npt.NDArray[np.int64]
    y: npt.NDArray[np.int8]
    label_at: npt.NDArray[np.int64]  # epoch seconds when the label becomes known
    raw: dict[str, F64]
    prob: dict[str, F64]
    never_break: dict[str, F64]  # raw scores on the never-break set
    models: dict[str, ModelInfo]
    week_end: dict[int, int]  # epoch seconds of each week's last second
    histograms: dict[str, Any] | None
    info: dict[str, Any]

    def __post_init__(self) -> None:
        n = len(self.file_ids)
        if any(a.shape != (n,) for a in (self.week, self.y, self.label_at)):
            raise ValueError("per-file arrays must have one entry per file")
        for mid in self.models:
            if self.raw[mid].shape != (n,) or self.prob[mid].shape != (n,):
                raise ValueError(f"{mid}: scores must have one entry per file")
        if V1_ID not in self.models:
            raise ValueError("v1 (the starting production model) is missing")

    def flags_never_break(self, model_id: str) -> int:
        return int((self.never_break[model_id] > self.models[model_id].threshold).sum())


def load_score_table(export_dirs: list[Path], manifest: dict[str, Any]) -> ScoreTable:
    """Merge Kaggle export directories (already verified by `import_export_zip`).
    `manifest` is the committed Stage 1 manifest (docs/cleaning/<type>.json): week ends."""
    if not export_dirs:
        raise ValueError("no experiment exports")
    base: dict[str, Any] | None = None
    raw: dict[str, F64] = {}
    prob: dict[str, F64] = {}
    nb: dict[str, F64] = {}
    models: dict[str, ModelInfo] = {}
    hist: dict[str, Any] | None = None
    infos = []
    for d in export_dirs:
        info = json.loads((d / INFO_FILE).read_text(encoding="utf-8"))
        if info["data_fingerprint"] != manifest["data_fingerprint"]:
            raise ValueError(f"{d.name}: data fingerprint differs from the Stage 1 manifest")
        infos.append(info)
        t = pq.read_table(d / SCORES_FILE)
        meta = {
            "file_ids": np.asarray(t.column(ID_COLUMN).to_pylist(), dtype=np.str_),
            "week": t.column(WEEK_COLUMN).to_numpy().astype(np.int64),
            "y": t.column(LABEL_COLUMN).to_numpy().astype(np.int8),
            "label_at": t.column(LABEL_AVAILABLE_COLUMN).to_numpy().astype(np.int64),
        }
        if base is None:
            base = meta
        elif any(not np.array_equal(base[k], meta[k]) for k in base):
            raise ValueError(f"{d.name}: scored rows differ from the first export")
        nbt = pq.read_table(d / NEVER_BREAK_FILE)
        for mid, mi in json.loads((d / MODELS_FILE).read_text(encoding="utf-8")).items():
            r = t.column(raw_col(mid)).to_numpy().astype(np.float64)
            if mid in raw:
                if not np.array_equal(raw[mid], r):
                    raise ValueError(f"{mid}: different scores in two exports")
                continue
            raw[mid] = r
            prob[mid] = t.column(prob_col(mid)).to_numpy().astype(np.float64)
            nb[mid] = nbt.column(raw_col(mid)).to_numpy().astype(np.float64)
            models[mid] = ModelInfo.from_dict(mi)
        if hist is None and (d / HIST_FILE).exists():
            hist = json.loads((d / HIST_FILE).read_text(encoding="utf-8"))
    assert base is not None
    ends = {
        int(w["week"]): int(week_end(manifest, int(w["week"])).timestamp())
        for w in manifest["weeks"]
    }
    return ScoreTable(
        base["file_ids"],
        base["week"],
        base["y"],
        base["label_at"],
        raw,
        prob,
        nb,
        models,
        ends,
        hist,
        {"exports": infos},
    )
