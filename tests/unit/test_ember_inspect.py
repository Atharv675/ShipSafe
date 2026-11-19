"""Unit tests for the EMBER2024 record readers and dataset report, on synthetic zips."""

from __future__ import annotations

import itertools
import json
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from sentinel.config import load_data_config
from sentinel.data.ember import (
    WeekFile,
    iter_member_records,
    list_week_files,
    parse_member_name,
    summarize_records,
    to_utc_datetime,
)
from sentinel.data.report import (
    FileTypeSummary,
    min_measurable_fpr,
    mismatch_flags,
    recommend_file_types,
    render_markdown,
    summarize_split,
    train_test_overlap,
)

CFG = load_data_config()
DS = CFG.dataset


_IDS = itertools.count()


def _rec(label: Any, ts: Any, **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {c: "x" for c in DS.metadata_columns}
    rec.update({b: {} for b in DS.feature_blocks})
    rec.update(label=label, first_submission_date=ts, file_type="pdf", sha256=f"h{next(_IDS)}")
    rec.update(extra)
    return rec


def _week(split: str = "test", start: str = "2024-09-22", end: str = "2024-09-28") -> WeekFile:
    name = f"{start}_{end}_PDF_{split}.jsonl"
    w = parse_member_name(name, DS.member_regex)
    assert w is not None
    return w


def _summ(records: list[dict[str, Any]], week: WeekFile) -> Any:
    return summarize_records(
        records,
        week,
        label_column=DS.label_column,
        time_column=DS.time_column,
        metadata_columns=DS.metadata_columns,
        feature_blocks=DS.feature_blocks,
        id_column=DS.id_column,
        dup_ignored_columns=DS.dup_ignored_columns,
    )


def test_duplicates_are_counted_once_and_classified() -> None:
    w = _week()
    a = _rec(0, "2024-09-23T00:00:00")
    b = _rec(1, "2024-09-23T00:00:00", caps=[])
    c = _rec(1, "2024-09-23T00:00:00")
    d = _rec(0, "2024-09-23T00:00:00")
    recs = [
        a,
        b,
        c,
        d,
        dict(a),  # byte-identical repeat
        {**b, "caps": [{"Capability": "x"}], "mbc": []},  # differs only in Capa keys
        {**c, "general": {"size": 1}},  # differs in a feature block
        {**d, "label": 1},  # label conflict
        _rec(0, "2024-09-23T00:00:00", sha256=None),  # no id: counted, never deduped
    ]
    s = _summ(recs, w)
    assert (s.raw_rows, s.rows, s.duplicate_rows, s.missing_id) == (9, 5, 4, 1)
    assert (s.benign, s.malicious) == (3, 2)  # first occurrence wins
    assert s.dup_identical == 1
    assert s.dup_differ_only_ignored == 1
    assert s.dup_differ_other == 2
    assert s.dup_label_conflicts == 1
    assert s.dup_differing_keys == {"caps": 1, "mbc": 1, "general": 1, "label": 1}
    assert s.missing_metadata["sha256"] == 1


def test_cross_week_repeats_leakage_and_outside_days() -> None:
    w1 = _week("train", "2024-09-08", "2024-09-14")
    w2 = _week("train", "2024-09-15", "2024-09-21")
    wt = _week("test", "2024-09-22", "2024-09-28")
    shared = _rec(1, "2024-09-10T00:00:00")
    s1 = _summ([shared, _rec(0, "2024-09-10T00:00:00")], w1)
    s2 = _summ([{**shared, "first_submission_date": "2024-09-23T00:00:00"}], w2)  # +2 days
    st = _summ([{**shared}, _rec(0, "2024-09-20T00:00:00")], wt)  # leak + (-2 days)
    train = summarize_split("PDF", "train", [s1, s2])
    test = summarize_split("PDF", "test", [st])
    assert train.cross_week_repeats == 1 and train.unique_ids == 2
    assert test.cross_week_repeats == 0
    assert train.outside_week_days == {"+2": 1}
    assert test.outside_week_days == {"-2": 1, "-12": 1}
    summ = FileTypeSummary("PDF", {"train": train, "test": test}, [s1, s2, st])
    summ.train_test_overlap = train_test_overlap(summ.weekly)
    assert summ.train_test_overlap == 1
    flags = mismatch_flags(summ, CFG)
    assert any(f.startswith("LEAKAGE: 1 sha256") for f in flags)
    assert any("1 files appear in more than one week" in f for f in flags)
    assert train_test_overlap([s1, s2]) == 0  # no test weeks -> no overlap


def test_duplicate_flag_in_report() -> None:
    w = _week()
    r = _rec(0, "2024-09-23T00:00:00")
    s = _summ([r, dict(r)], w)
    summ = FileTypeSummary("PDF", {"test": summarize_split("PDF", "test", [s])}, [s])
    flags = mismatch_flags(summ, CFG)
    assert any("2 rows but 1 unique sha256" in f for f in flags)


def test_parse_member_name_real_pattern() -> None:
    w = parse_member_name("2024-09-22_2024-09-28_Dot_Net_test.jsonl", DS.member_regex)
    assert w is not None
    assert (w.start, w.end, w.file_type, w.split) == (
        date(2024, 9, 22),
        date(2024, 9, 28),
        "Dot_Net",
        "test",
    )
    assert parse_member_name("README.md", DS.member_regex) is None
    assert parse_member_name("2024-09-22_2024-09-28_PDF_challenge.jsonl", DS.member_regex) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1727049600, datetime(2024, 9, 23, tzinfo=UTC)),
        (1727049600000, datetime(2024, 9, 23, tzinfo=UTC)),
        ("1727049600", datetime(2024, 9, 23, tzinfo=UTC)),
        ("2024-09-23T00:00:00Z", datetime(2024, 9, 23, tzinfo=UTC)),
        ("2024-09-23 00:00:00", datetime(2024, 9, 23, tzinfo=UTC)),
        (None, None),
        ("", None),
        (float("nan"), None),
        (True, None),
        ([1], None),
    ],
)
def test_to_utc_datetime(value: Any, expected: datetime | None) -> None:
    assert to_utc_datetime(value) == expected


def test_summarize_counts_labels_missing_and_dates() -> None:
    w = _week()
    recs = [
        _rec(0, "2024-09-22T10:00:00"),
        _rec(1, "2024-09-28T23:00:00"),
        _rec(1, "2024-09-25T00:00:00", family=None),
        _rec(None, "2024-10-30T00:00:00"),  # unlabeled AND outside week
        _rec(0, "garbage"),  # unparsable
        _rec(0, None, general=None),  # missing timestamp + missing feature block
    ]
    s = _summ(recs, w)
    assert s.rows == 6
    assert (s.benign, s.malicious, s.unlabeled) == (3, 2, 1)
    assert s.labels["null"] == 1
    assert s.missing_metadata["family"] == 1
    assert s.missing_metadata["first_submission_date"] == 1
    assert s.missing_blocks["general"] == 1
    assert s.unparsable_time == 2
    assert s.outside_week == 1
    assert s.min_time == datetime(2024, 9, 22, 10, tzinfo=UTC)
    assert s.max_time == datetime(2024, 10, 30, tzinfo=UTC)


def _write_zip(path: Path, members: dict[str, list[dict[str, Any]]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, recs in members.items():
            zf.writestr(name, "\n".join(json.dumps(r) for r in recs) + "\n")
        zf.writestr("notes.txt", "ignored")


def test_zip_roundtrip_sorted_weeks(tmp_path: Path) -> None:
    zp = tmp_path / "PDF_test.zip"
    _write_zip(
        zp,
        {
            "2024-09-29_2024-10-05_PDF_test.jsonl": [_rec(1, 1727654400)],
            "2024-09-22_2024-09-28_PDF_test.jsonl": [_rec(0, 1727049600), _rec(1, 1727049600)],
        },
    )
    weeks = list_week_files(zp, DS.member_regex)
    assert [w.start for w in weeks] == [date(2024, 9, 22), date(2024, 9, 29)]
    recs = list(iter_member_records(zp, weeks[0].name))
    assert [r["label"] for r in recs] == [0, 1]


def test_iter_rejects_non_object_lines(tmp_path: Path) -> None:
    zp = tmp_path / "bad.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("2024-09-22_2024-09-28_PDF_test.jsonl", "[1,2]\n")
    with pytest.raises(ValueError, match="JSON object"):
        list(iter_member_records(zp, "2024-09-22_2024-09-28_PDF_test.jsonl"))


def _summary(ft: str, test_benign: int, weeks_test: int = 12, rows_per_week: int = 1000) -> Any:
    weekly = []
    for i in range(weeks_test):
        start = date.fromordinal(date(2024, 9, 22).toordinal() + 7 * i)
        end = date.fromordinal(start.toordinal() + 6)
        w = _week("test", start.isoformat(), end.isoformat())
        ben = test_benign // weeks_test
        recs = [_rec(0 if j < ben else 1, f"{start}T12:00:00") for j in range(rows_per_week)]
        weekly.append(_summ(recs, w))
    return FileTypeSummary(
        file_type=ft, splits={"test": summarize_split(ft, "test", weekly)}, weekly=weekly
    )


def test_mismatch_flags_clean_and_dirty() -> None:
    clean = _summary("PDF", test_benign=6000)
    assert mismatch_flags(clean, CFG) == []
    dirty = _summary("PDF", test_benign=6000, weeks_test=11)
    flags = mismatch_flags(dirty, CFG)
    assert any("11 weeks measured vs 12" in f for f in flags)
    assert any("11,000 rows measured vs 12,000" in f for f in flags)


def test_recommendation_ranks_by_benign_and_threshold() -> None:
    sums = {"PDF": _summary("PDF", 6000), "Big": _summary("Big", 12000, rows_per_week=5000)}
    meeting, ranked = recommend_file_types(sums, min_benign=10000)
    assert ranked == ["Big", "PDF"]
    assert meeting == ["Big"]
    assert min_measurable_fpr(10000) == pytest.approx(1e-4)
    assert min_measurable_fpr(0) == float("inf")


def test_render_markdown_contains_key_sections() -> None:
    s = _summary("PDF", 6000)
    s.feature_dim, s.nonconstant_features_in_sample, s.sample_rows = 2568, 700, 100
    s.flags = ["example flag"]
    md = render_markdown({"PDF": s}, CFG, generated_at="now", provenance=["unit test"])
    assert "Benign files in the TEST weeks" in md
    assert "| PDF | 6,000 |" in md
    assert "**no**" in md  # 6,000 < 50,000
    assert "example flag" in md
    assert "2568" in md
