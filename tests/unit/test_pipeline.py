"""End-to-end Stage 1 pipeline on synthetic EMBER-shaped zips (fake 6-dim vectoriser).

Includes the plan's required guards: no test-week row hash may appear in train weeks, and
re-running gives byte-identical outputs.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import pandera.errors
import pyarrow.parquet as pq
import pytest

from sentinel.config import NeverBreakConfig, load_clean_config, load_data_config
from sentinel.data.clean import row_hashes
from sentinel.data.clean_report import build_cleaning_report, render_cleaning_report
from sentinel.data.pipeline import Layout, clean_file_type, ordered_weeks
from sentinel.data.schema import LABEL_AVAILABLE_COLUMN, META_COLUMNS, split_schema
from sentinel.data.splits import labelled_as_of, load_manifest, read_weeks, weeks_in_split

FT = "Win64"
DIM = 6
DATA_CFG = load_data_config()
CFG = load_clean_config().model_copy(update={"never_break": NeverBreakConfig(size=3)})
DAY = 86_400


def fake_vectorize(rec: dict[str, Any]) -> npt.NDArray[np.float32]:
    return np.array([np.nan if v is None else v for v in rec["vec"]], dtype=np.float32)


def _ts(day: str, hour: int = 12) -> int:
    return int(datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC).timestamp())


def _rec(sha: str, label: Any, t: Any, vec: list[float | None]) -> dict[str, Any]:
    return {"sha256": sha, "label": label, "first_submission_date": t, "vec": vec, "caps": []}


TRAIN_WEEKS = [("2024-09-01", "2024-09-07"), ("2024-09-08", "2024-09-14")]
TEST_WEEKS = [("2024-09-22", "2024-09-28"), ("2024-09-29", "2024-10-05")]


def _records() -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Column 5 is constant (0) in train but not in test: must be dropped everywhere."""
    w1, w2 = TRAIN_WEEKS[0][0], TRAIN_WEEKS[1][0]
    t1, t2 = TEST_WEEKS[0][0], TEST_WEEKS[1][0]
    train1 = [_rec(f"b{i}", 0, _ts(w1), [i, 1, 0, 0, 0, 0]) for i in range(4)] + [
        _rec(f"m{i}", 1, _ts(w1), [i, 2, 1, 0, 0, 0]) for i in range(3)
    ]
    train1 += [train1[0]]  # raw repeat (same sha) -> dropped
    train1 += [_rec("bad_label", None, _ts(w1), [9, 9, 9, 9, 9, 0])]
    train1 += [_rec("bad_time", 0, None, [8, 8, 8, 8, 8, 0])]
    train1 += [_rec("nan", 0, _ts(w1), [None, 3, 3, 3, 3, 0])]  # NaN -> 0, kept
    train2 = [_rec(f"c{i}", i % 2, _ts(w2), [i, 5, 5, 5, i, 0]) for i in range(5)]
    train2 += [_rec("dupvec", 1, _ts(w2), [0, 1, 0, 0, 0, 0])]  # same vector as b0 -> dropped
    test1 = [_rec(f"t{i}", i % 2, _ts(t1), [i, 7, 7, 7, 7, 1]) for i in range(4)]
    test1 += [_rec("b1", 0, _ts(t1), [1, 1, 0, 0, 0, 0])]  # id in train -> leakage
    test1 += [_rec("leakvec", 1, _ts(t1), [0, 2, 1, 0, 0, 0])]  # vector of m0 -> leakage
    test1 += [_rec("early", 0, _ts(t1) - 2 * DAY, [42, 7, 7, 7, 7, 1])]  # dated before week
    test2 = [_rec(f"u{i}", i % 2, _ts(t2), [i, 8, 8, 8, 8, 1]) for i in range(3)]
    test2 += [_rec("t0", 0, _ts(t2), [0, 7, 7, 7, 7, 1])]  # cross-week repeat in test
    return {
        ("train", w1): train1,
        ("train", w2): train2,
        ("test", t1): test1,
        ("test", t2): test2,
    }


def _write_zips(raw: Path) -> None:
    raw.mkdir(parents=True, exist_ok=True)
    recs = _records()
    for split, weeks in (("train", TRAIN_WEEKS), ("test", TEST_WEEKS)):
        with zipfile.ZipFile(raw / f"{FT}_{split}.zip", "w") as zf:
            for start, end in weeks:
                lines = "\n".join(json.dumps(r) for r in recs[(split, start)]) + "\n"
                # fixed entry timestamp: identical input bytes => identical data fingerprint
                info = zipfile.ZipInfo(
                    f"{split}/{start}_{end}_{FT}_{split}.jsonl", date_time=(2024, 1, 1, 0, 0, 0)
                )
                zf.writestr(info, lines)


def _clean(root: Path, workers: int = 1) -> tuple[Layout, dict[str, Any]]:
    raw = root / "raw"
    if not raw.exists():
        _write_zips(raw)
    layout = Layout(root / "splits", root / "clean", FT)
    m = clean_file_type(
        FT, raw, layout, DATA_CFG, CFG, workers=workers, vectorize=fake_vectorize, dim=DIM
    )
    return layout, m


@pytest.fixture(scope="module")
def cleaned(tmp_path_factory: pytest.TempPathFactory) -> tuple[Layout, dict[str, Any]]:
    return _clean(tmp_path_factory.mktemp("run1"))


def _steps(m: dict[str, Any], split: str) -> dict[str, int]:
    return {s["step"]: s["removed"] for s in m["steps"] if s["split"] == split}


def test_manifest_counts_every_rule(cleaned: tuple[Layout, dict[str, Any]]) -> None:
    _, m = cleaned
    tr, te = _steps(m, "train"), _steps(m, "test")
    assert tr["drop_repeated_id_within_week"] == 1
    assert tr["drop_bad_label"] == 1
    assert tr["drop_bad_timestamp"] == 1
    assert tr["drop_duplicate_vector"] == 1
    assert te["drop_test_id_in_train"] == 1
    assert te["drop_test_vector_in_train"] == 1
    assert te["drop_cross_week_repeat"] == 1
    fill = next(s for s in m["steps"] if s["step"] == "fill_non_finite" and s["split"] == "train")
    assert fill["note"].startswith("1 values")
    outside = next(
        s
        for s in m["steps"]
        if s["step"] == "keep_outside_week_timestamps" and s["split"] == "test"
    )
    assert outside["note"].startswith("1 rows")
    assert m["totals"]["train"]["rows"] == 4 + 3 + 1 + 5  # b*, m*, nan, c*
    assert m["totals"]["test"]["rows"] == 4 + 1 + 3  # t*, early, u*
    for s in m["steps"]:
        assert s["before"] - s["after"] == s["removed"] >= 0


def test_constant_columns_dropped_using_train_only(cleaned: tuple[Layout, dict[str, Any]]) -> None:
    layout, m = cleaned
    assert m["dropped_constant_columns"] == [5]  # varies in test, constant in train
    assert m["feature_columns"] == DIM - 1
    df = read_weeks(layout.splits_root, FT, [1])
    assert [c for c in df.columns if c not in META_COLUMNS] == [f"f_000{i}" for i in range(5)]


def test_no_test_week_row_appears_in_train_weeks(cleaned: tuple[Layout, dict[str, Any]]) -> None:
    layout, m = cleaned
    tr = read_weeks(layout.splits_root, FT, weeks_in_split(m, "train"))
    te = read_weeks(layout.splits_root, FT, weeks_in_split(m, "test"))
    feats = [c for c in tr.columns if c not in META_COLUMNS]
    tr_h = set(row_hashes(tr[feats].to_numpy(np.float32), 16))
    te_h = set(row_hashes(te[feats].to_numpy(np.float32), 16))
    assert not tr_h & te_h, "test-week feature rows found in train weeks (leakage)"
    assert not set(tr["sha256"]) & set(te["sha256"])
    assert tr["sha256"].is_unique and te["sha256"].is_unique


def test_rerun_is_byte_identical_and_worker_independent(
    cleaned: tuple[Layout, dict[str, Any]], tmp_path: Path
) -> None:
    _, m1 = cleaned
    _, m2 = _clean(tmp_path, workers=2)
    assert m1["feature_schema_version"] == m2["feature_schema_version"]
    assert [w["sha256"] for w in m1["weeks"]] == [w["sha256"] for w in m2["weeks"]]
    assert m1["never_break"]["sha256"] == m2["never_break"]["sha256"]
    assert m1["data_fingerprint"] == m2["data_fingerprint"]


def test_outputs_follow_the_schema_and_time_order(cleaned: tuple[Layout, dict[str, Any]]) -> None:
    layout, m = cleaned
    names = [c["name"] for c in m["columns"] if c["name"] not in META_COLUMNS]
    schema = split_schema(names, CFG)
    assert [w["week"] for w in m["weeks"]] == [1, 2, 3, 4]
    assert [w["split"] for w in m["weeks"]] == ["train", "train", "test", "test"]
    for w in m["weeks"]:
        df = pq.read_table(layout.splits_root / w["path"]).to_pandas()
        schema.validate(df)
        assert (df["week"] == w["week"]).all() and len(df) == w["rows"]
    assert load_manifest(layout.splits_root, FT)["seed"] == CFG.seed
    assert m["git_commit"] and len(m["data_fingerprint"]) == 64


def test_never_break_set_is_benign_train_only(cleaned: tuple[Layout, dict[str, Any]]) -> None:
    layout, m = cleaned
    nb = pq.read_table(layout.never_break).to_pandas()
    assert len(nb) == m["never_break"]["rows"] == 3
    assert (nb["label"] == CFG.benign_label).all()
    assert set(nb["week"]) <= set(weeks_in_split(m, "train"))


def test_late_labels_filter(cleaned: tuple[Layout, dict[str, Any]]) -> None:
    layout, _ = cleaned
    df = read_weeks(layout.splits_root, FT, [1, 2])
    late = df[LABEL_AVAILABLE_COLUMN].max()
    assert len(labelled_as_of(df, late.to_pydatetime())) == len(df)
    early = labelled_as_of(df, datetime(2024, 9, 1, tzinfo=UTC))
    assert early.empty
    with pytest.raises(ValueError):
        labelled_as_of(df, datetime(2024, 9, 1))  # naive datetime rejected


def test_schema_rejects_bad_rows(cleaned: tuple[Layout, dict[str, Any]]) -> None:
    layout, m = cleaned
    names = [c["name"] for c in m["columns"] if c["name"] not in META_COLUMNS]
    schema = split_schema(names, CFG)
    good = read_weeks(layout.splits_root, FT, [1])
    bad_label = good.assign(label=good["label"].replace({1: 7}).astype(np.int8))
    inf = good.copy()
    inf.loc[0, names[0]] = np.float32(np.inf)
    no_late = good.assign(**{LABEL_AVAILABLE_COLUMN: good["first_seen"]})
    extra = good.assign(extra=1)
    naive = good.assign(first_seen=good["first_seen"].dt.tz_localize(None))
    for df in (bad_label, inf, no_late, extra, naive):
        with pytest.raises((pandera.errors.SchemaError, pandera.errors.SchemaErrors)):
            schema.validate(df)


def test_cleaning_report_renders(cleaned: tuple[Layout, dict[str, Any]], tmp_path: Path) -> None:
    layout, _ = cleaned
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / f"{FT}.json").write_text(layout.manifest.read_text(encoding="utf-8"))
    out = tmp_path / "R.md"
    assert build_cleaning_report(mdir, out, ["Win32", FT]) == [FT]
    md = out.read_text(encoding="utf-8")
    assert "| drop_test_vector_in_train | test | rows |" in md
    assert "No manifests found" in render_cleaning_report([])


def test_missing_zip_and_overlapping_weeks_fail(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ordered_weeks(tmp_path, FT, DATA_CFG.dataset)
    _write_zips(tmp_path)
    with zipfile.ZipFile(tmp_path / f"{FT}_test.zip", "a") as zf:  # a test week inside train
        zf.writestr(f"test/2024-09-01_2024-09-07_{FT}_test.jsonl", "")
    with pytest.raises(ValueError, match="overlap"):
        ordered_weeks(tmp_path, FT, DATA_CFG.dataset)


def test_wrong_vector_shape_fails(tmp_path: Path) -> None:
    _write_zips(tmp_path / "raw")
    with pytest.raises(ValueError, match="vector shape"):
        clean_file_type(
            FT,
            tmp_path / "raw",
            Layout(tmp_path / "s", tmp_path / "c", FT),
            DATA_CFG,
            CFG,
            vectorize=fake_vectorize,
            dim=DIM + 1,
        )


def test_split_files_are_readable_as_dataframes(cleaned: tuple[Layout, dict[str, Any]]) -> None:
    layout, _ = cleaned
    df = read_weeks(layout.splits_root, FT, [3], columns=["sha256", "label"])
    assert list(df.columns) == ["sha256", "label"] and isinstance(df, pd.DataFrame)
    with pytest.raises(ValueError):
        read_weeks(layout.splits_root, FT, [])
