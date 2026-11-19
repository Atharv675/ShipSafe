"""Stats JSON round trip + merged report, so Kaggle/Colab results combine with local ones."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.config import load_data_config
from sentinel.data.ember import parse_member_name, summarize_records
from sentinel.data.report import FileTypeSummary, mismatch_flags, summarize_split
from sentinel.data.report_io import (
    build_report,
    read_summaries,
    summary_from_dict,
    summary_to_dict,
    write_summary,
)

CFG = load_data_config()
DS = CFG.dataset


def _summary(ft: str, benign: int, malicious: int) -> FileTypeSummary:
    weekly = []
    for split, start, end in [
        ("train", "2024-09-15", "2024-09-21"),
        ("test", "2024-09-22", "2024-09-28"),
    ]:
        week = parse_member_name(f"{start}_{end}_{ft}_{split}.jsonl", DS.member_regex)
        assert week is not None
        recs = [
            {
                "sha256": f"{ft}{split}{i}",
                "label": int(i >= benign),
                "first_submission_date": 1727049600 if split == "test" else 1726444800,
                "caps": [i],
            }
            for i in range(benign + malicious)
        ]
        recs.append({**recs[0], "caps": ["changed"]})  # a repeat differing only in Capa
        weekly.append(
            summarize_records(
                recs,
                week,
                label_column=DS.label_column,
                time_column=DS.time_column,
                metadata_columns=DS.metadata_columns,
                feature_blocks=DS.feature_blocks,
                id_column=DS.id_column,
                dup_ignored_columns=DS.dup_ignored_columns,
            )
        )
    s = FileTypeSummary(
        file_type=ft,
        splits={
            sp: summarize_split(ft, sp, [w for w in weekly if w.week.split == sp])
            for sp in ("train", "test")
        },
        weekly=weekly,
        feature_dim=2568,
        nonconstant_features_in_sample=1000,
        sample_rows=10,
    )
    s.flags = mismatch_flags(s, CFG)
    return s


def test_round_trip_preserves_everything_but_ids() -> None:
    s = _summary("Win64", benign=3, malicious=2)
    raw = summary_to_dict(s, provenance=["p1"], generated_at="t")
    text = json.dumps(raw)
    assert "Win64train0" not in text  # file ids never serialised
    back, prov, gen = summary_from_dict(json.loads(text))
    assert (prov, gen) == (["p1"], "t")
    assert back.flags == s.flags
    for sp in ("train", "test"):
        a, b = s.splits[sp], back.splits[sp]
        assert (b.rows, b.benign, b.malicious, b.unique_ids) == (
            a.rows,
            a.benign,
            a.malicious,
            a.unique_ids,
        )
        assert b.dup_differ_only_ignored == a.dup_differ_only_ignored == 1
        assert b.dup_differing_keys == a.dup_differing_keys
        assert b.min_time == a.min_time and b.min_time is not None
    assert [w.week for w in back.weekly] == [w.week for w in s.weekly]
    assert [w.benign for w in back.weekly] == [w.benign for w in s.weekly]
    assert all(not w.ids for w in back.weekly)


def test_schema_version_is_checked() -> None:
    raw = summary_to_dict(_summary("PDF", 1, 1), provenance=[], generated_at="t")
    raw["schema_version"] = 999
    with pytest.raises(ValueError, match="schema_version"):
        summary_from_dict(raw)


def test_plan_check_and_recommendation_when_plan_type_is_too_small() -> None:
    from sentinel.data.report import render_markdown

    cfg = CFG.model_copy(update={"min_benign_test_files": 5})
    pdf = _summary("PDF", 2, 2)
    md = render_markdown(
        {"Win32": _summary("Win32", 9, 1), "Win64": _summary("Win64", 6, 1), "PDF": pdf},
        cfg,
        generated_at="t",
        provenance=[],
    )
    assert "## Plan assumption check" in md
    assert "| test weeks | 12 | 1 | **MISMATCH** |" in md  # synthetic data has 1 test week
    assert "PDF has **2 benign test files**, which is **below** the 5 needed" in md
    assert "**Win64**: 6 benign test files" in md
    assert "Recommended: **Win64** (smallest type" in md and "or **Win32** (most" in md


def test_recommendation_combines_types_when_none_meets_threshold() -> None:
    from sentinel.data.report import render_markdown

    cfg = CFG.model_copy(update={"min_benign_test_files": 10})
    md = render_markdown(
        {"Win64": _summary("Win64", 6, 1), "PDF": _summary("PDF", 5, 1)},
        cfg,
        generated_at="t",
        provenance=[],
    )
    assert "No single type meets 10; combining Win64, PDF gives 11." in md


def test_build_report_merges_sources_in_config_order(tmp_path: Path) -> None:
    write_summary(
        tmp_path / "PDF.json",
        _summary("PDF", 4, 4),
        provenance=["local"],
        generated_at="2026-11-18",
    )
    write_summary(
        tmp_path / "Win32.json",
        _summary("Win32", 9, 9),
        provenance=["colab"],
        generated_at="2026-11-19",
    )
    out = tmp_path / "REPORT.md"
    assert build_report(tmp_path, out, CFG) == ["Win32", "PDF"]  # config order
    md = out.read_text(encoding="utf-8")
    assert "**Win32** (2026-11-19): colab" in md
    assert "**PDF** (2026-11-18): local" in md
    assert md.index("| Win32 | 9 |") < md.index("| PDF | 4 |")  # benign ranking
    assert set(read_summaries(tmp_path)) == {"PDF", "Win32"}
