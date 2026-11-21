"""Stage 5 release gate: statistics, checks, decision, evidence bundle (no stack needed)."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scipy.stats import binomtest

from sentinel.config import GatePolicy, load_gate_policy
from sentinel.gate.checks import GateInput, ModelScores, decide, run_checks
from sentinel.gate.evidence import (
    bundle_sha256,
    canonical_json,
    render_markdown,
    verify_bundle,
)
from sentinel.gate.run import (
    WeekLabels,
    assemble_input,
    build_bundle,
    choose_weeks,
    decision_id_for,
    labels_cutoff,
    models_as_of_week,
    read_week_labels,
    verify_reports,
    write_reports,
)
from sentinel.models.calibration import IsotonicCalibrator
from sentinel.models.evaluate import mcnemar_exact, paired_table
from sentinel.replay.events import make_event_id
from tests.serving_support import loaded

WEEKS = (53, 54, 55)
PER_CLASS = 2000  # per week: 2,000 benign -> 2 false alarms allowed at 0.1% FPR
THRESHOLD = 0.75  # shipped threshold of both synthetic models


@pytest.fixture(scope="module")
def policy() -> GatePolicy:
    return load_gate_policy()


# ----------------------------------------------------------------------------- statistics


@pytest.mark.parametrize(
    ("b", "c"),
    [(0, 0), (0, 1), (1, 0), (3, 3), (0, 12), (5, 20), (40, 25), (100, 130), (450, 520)],
)
def test_mcnemar_exact_matches_scipy_binomtest(b: int, c: int) -> None:
    expected = 1.0 if b + c == 0 else binomtest(min(b, c), b + c, 0.5).pvalue
    assert math.isclose(mcnemar_exact(b, c), expected, rel_tol=1e-9, abs_tol=1e-300)


def test_mcnemar_is_symmetric_and_validates() -> None:
    assert mcnemar_exact(7, 19) == mcnemar_exact(19, 7)
    with pytest.raises(ValueError):
        mcnemar_exact(-1, 2)


def test_paired_table_counts_correctness_per_file() -> None:
    y = [1, 1, 1, 0, 0, 0]
    a = [1, 0, 1, 0, 1, 1]  # right, wrong, right, right, wrong, wrong
    b = [1, 1, 0, 0, 0, 1]  # right, right, wrong, right, right, wrong
    t = paired_table(y, a, b)
    assert (t.both_right, t.only_a_right, t.only_b_right, t.both_wrong) == (2, 1, 2, 1)
    assert t.discordant == 3
    with pytest.raises(ValueError):
        paired_table([1, 0], [1], [1, 0])


# ----------------------------------------------------------------------------- synthetic gate


def _base(seed: int = 0) -> tuple[npt.NDArray[np.int8], npt.NDArray[np.int64], npt.NDArray[Any]]:
    rng = np.random.default_rng(seed)
    y = np.tile(np.r_[np.ones(PER_CLASS), np.zeros(PER_CLASS)], len(WEEKS)).astype(np.int8)
    week = np.repeat(np.array(WEEKS, dtype=np.int64), 2 * PER_CLASS)
    s = np.where(y == 1, rng.uniform(0.3, 1.0, y.size), rng.uniform(0.0, 0.76, y.size))
    return y, week, s


def _gate(
    y: npt.NDArray[np.int8],
    week: npt.NDArray[np.int64],
    prod: npt.NDArray[Any],
    cand: npt.NDArray[Any],
    cand_nb: npt.NDArray[Any] | None = None,
    cand_prob: npt.NDArray[Any] | None = None,
    expected: int | None = None,
) -> GateInput:
    nb = np.full(500, 0.05)
    return GateInput(
        file_ids=[f"f{i}" for i in range(y.size)],
        y=y,
        week=week,
        production=ModelScores("m/1", THRESHOLD, prod, prod, nb),
        candidate=ModelScores(
            "m/2",
            THRESHOLD,
            cand,
            cand if cand_prob is None else cand_prob,
            nb if cand_nb is None else cand_nb,
        ),
        expected_events=y.size if expected is None else expected,
        paired_events=y.size,
        unlabelled_events=0,
    )


def _better() -> GateInput:
    y, week, s = _base()
    cand = np.where(y == 1, np.minimum(s + 0.15, 1.0), s)  # catches more, same benign scores
    return _gate(y, week, s, cand)


def _statuses(g: GateInput, policy: GatePolicy) -> dict[str, str]:
    return {r.name: r.status for r in run_checks(g, policy)}


def test_clearly_better_candidate_is_promoted(policy: GatePolicy) -> None:
    results = run_checks(_better(), policy)
    assert all(r.status == "pass" for r in results), [r.as_dict() for r in results]
    decision, reason = decide(results)
    assert decision == "PROMOTE"
    assert reason.startswith("Promoted")
    dis = next(r for r in results if r.name == "disagreement").numbers
    assert dis["malicious"]["candidate_significantly_better"]
    assert dis["malicious"]["only_production_right"] == 0
    assert dis["benign"]["discordant"] == 0
    assert len(dis["flipped_files"]) == dis["discordant_total"] > policy.min_disagreements


def test_ten_times_false_alarms_is_rejected(policy: GatePolicy) -> None:
    y, week, s = _base()
    cand = np.where(y == 0, s + 0.09, np.minimum(s + 0.15, 1.0))  # better on malware, noisy
    g = _gate(y, week, s, cand)
    results = run_checks(g, policy)
    fa = next(r for r in results if r.name == "false_alarms")
    assert fa.status == "fail"
    assert fa.numbers["candidate"]["fp"] >= 8 * fa.numbers["production"]["fp"]
    decision, reason = decide(results)
    assert decision == "REJECT"
    assert "[false_alarms]" in reason
    assert next(r for r in results if r.name == "disagreement").numbers["benign"][
        "candidate_significantly_worse"
    ]


def test_identical_candidate_is_insufficient_evidence(policy: GatePolicy) -> None:
    y, week, s = _base()
    results = run_checks(_gate(y, week, s, s.copy()), policy)
    assert _statuses(_gate(y, week, s, s.copy()), policy)["disagreement"] == "insufficient"
    decision, reason = decide(results)
    assert decision == "INSUFFICIENT_EVIDENCE"
    assert "too few to tell the models apart" in reason


def test_few_balanced_disagreements_are_insufficient_not_rejected(policy: GatePolicy) -> None:
    y, week, s = _base()
    cand = s.copy()
    mal_caught = np.flatnonzero((y == 1) & (s <= THRESHOLD))[:6]
    mal_missed = np.flatnonzero((y == 1) & (s > THRESHOLD))[:6]
    cand[mal_caught] = 0.99  # candidate now catches 6 production misses
    cand[mal_missed] = 0.10  # and misses 6 production catches
    g = _gate(y, week, s, cand)
    st = _statuses(g, policy)
    assert st["disagreement"] == "insufficient"
    assert decide(run_checks(g, policy))[0] == "INSUFFICIENT_EVIDENCE"


def test_never_break_hit_is_rejected(policy: GatePolicy) -> None:
    g = _better()
    nb = np.full(500, 0.05)
    nb[17] = 0.99
    g = replace(g, candidate=replace(g.candidate, never_break_scores=nb))
    results = run_checks(g, policy)
    assert _statuses(g, policy)["never_break"] == "fail"
    decision, reason = decide(results)
    assert decision == "REJECT"
    assert "[never_break] The candidate flags 1 of 500" in reason


def test_one_bad_week_is_rejected(policy: GatePolicy) -> None:
    y, week, s = _base()
    cand = np.where(y == 1, np.minimum(s + 0.15, 1.0), s)
    bad = (week == 54) & (y == 1)
    cand[bad] = s[bad] * 0.7  # week 54 collapses
    g = _gate(y, week, s, cand)
    wr = next(r for r in run_checks(g, policy) if r.name == "weekly_regression")
    assert wr.status == "fail"
    assert "54" in wr.summary
    assert decide(run_checks(g, policy))[0] == "REJECT"


def test_low_coverage_is_insufficient(policy: GatePolicy) -> None:
    y, week, s = _base()
    cand = np.where(y == 1, np.minimum(s + 0.15, 1.0), s)
    g = _gate(y, week, s, cand, expected=2 * y.size)
    assert _statuses(g, policy)["coverage"] == "insufficient"
    assert decide(run_checks(g, policy))[0] == "INSUFFICIENT_EVIDENCE"


def test_too_few_benign_for_operating_point_is_insufficient(policy: GatePolicy) -> None:
    y = np.r_[np.ones(500), np.zeros(500)].astype(np.int8)  # 500 benign: 0.5 alarms at 0.1%
    s = np.where(y == 1, 0.9, 0.1)
    g = _gate(y, np.full(y.size, 53, dtype=np.int64), s, s.copy())
    st = _statuses(g, policy)
    assert st["recall_at_fpr"] == st["false_alarms"] == st["weekly_regression"] == "insufficient"


def test_soft_calibration_failure_does_not_block(policy: GatePolicy) -> None:
    g = _better()
    bad_prob = np.where(g.y == 1, 0.05, 0.95)  # perfectly wrong probabilities
    g = replace(g, candidate=replace(g.candidate, probabilities=bad_prob))
    results = run_checks(g, policy)
    cal = next(r for r in results if r.name == "calibration")
    assert cal.status == "fail"
    assert not cal.hard
    decision, reason = decide(results)
    assert decision == "PROMOTE"
    assert "Note (does not block): [calibration]" in reason


def test_calibration_can_be_made_hard(policy: GatePolicy) -> None:
    hard = policy.model_copy(update={"soft_checks": []})
    g = _better()
    g = replace(g, candidate=replace(g.candidate, probabilities=np.where(g.y == 1, 0.05, 0.95)))
    assert decide(run_checks(g, hard))[0] == "REJECT"


def test_gate_input_rejects_misaligned_arrays() -> None:
    y, week, s = _base()
    with pytest.raises(ValueError):
        _gate(y, week[:-1], s, s)


# ----------------------------------------------------------------------------- evidence


def _bundle(policy: GatePolicy) -> dict[str, Any]:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    manifest = {
        "data_fingerprint": "fp",
        "feature_schema_version": "schema",
        "weeks": [{"week": w, "sha256": f"sha{w}"} for w in (52, *WEEKS)],
        "never_break": {"rows": 500, "sha256": "nb"},
    }
    return build_bundle(
        decision_id=decision_id_for("Win64", "1", "2", now),
        created_at=now,
        file_type="Win64",
        policy=policy,
        results=run_checks(_better(), policy),
        production={"label": "m/1", "version": "1", "threshold": 0.75, "as_of_week": 52},
        candidate={"label": "m/2", "version": "2", "threshold": 0.75, "as_of_week": 52},
        weeks=list(WEEKS),
        labels_as_of=now,
        manifest=manifest,
        git_commit="abc",
    )


def test_bundle_has_every_required_field(policy: GatePolicy) -> None:
    b = _bundle(policy)
    assert b["decision_id"] == "Win64-v2-vs-v1-20261120T120000Z"
    assert b["decision"] == "PROMOTE" and b["reason"]
    assert b["policy"]["policy_version"] == policy.policy_version
    assert b["models"]["production"]["version"] == "1"
    assert b["models"]["candidate"]["version"] == "2"
    assert b["data"]["data_fingerprint"] == "fp"
    assert b["data"]["week_files_sha256"] == {"53": "sha53", "54": "sha54", "55": "sha55"}
    assert b["git_commit"] == "abc"
    dis = next(c for c in b["checks"] if c["name"] == "disagreement")
    assert dis["numbers"]["flipped_files"][0]["file_id"].startswith("f")
    for c in b["checks"]:
        assert {"name", "status", "hard", "summary", "numbers"} <= set(c)


def test_canonical_json_is_stable_and_nan_free(policy: GatePolicy) -> None:
    b = _bundle(policy)
    text = canonical_json(b)
    assert text == canonical_json(json.loads(text))  # round trip -> same bytes
    assert canonical_json({"b": float("nan"), "a": np.float64(1.5), "c": (1, 2)}) == (
        '{"a":1.5,"b":null,"c":[1,2]}'
    )


def test_editing_a_stored_bundle_is_detected(policy: GatePolicy, tmp_path: Path) -> None:
    b = _bundle(policy)
    text = canonical_json(b)
    sha = bundle_sha256(text)
    assert verify_bundle(text, sha)
    tampered = json.loads(text)
    tampered["decision"] = "PROMOTE" if b["decision"] != "PROMOTE" else "REJECT"
    assert not verify_bundle(canonical_json(tampered), sha)
    assert not verify_bundle(text.replace('"m/2"', '"m/3"'), sha)

    did = b["decision_id"]
    write_reports(tmp_path, did, text, render_markdown(b, sha))
    assert verify_reports([(did, sha, text)], tmp_path) == []
    f = tmp_path / f"{did}.json"
    f.write_bytes(f.read_bytes().replace(b'"PROMOTE"', b'"REJECT"', 1))
    assert verify_reports([(did, sha, text)], tmp_path) == [
        f"{did}: {did}.json was edited (SHA-256 differs from gate_decisions)"
    ]
    assert (
        "does not match its stored SHA-256" in verify_reports([(did, "0" * 64, text)], tmp_path)[0]
    )
    f.unlink()
    assert "missing" in verify_reports([(did, sha, text)], tmp_path)[0]


def test_markdown_report_is_readable(policy: GatePolicy) -> None:
    b = _bundle(policy)
    md = render_markdown(b, "f" * 64)
    assert f"**Decision: {b['decision']}**" in md
    assert "| disagreement | yes | **pass** |" in md
    assert "Recall per week" in md and "| 54 |" in md
    assert "McNemar" in md


# ----------------------------------------------------------------------------- run helpers


def test_choose_weeks_never_evaluates_on_training_weeks() -> None:
    assert choose_weeks(None, [55, 53, 54, 52, 53], after_week=52) == [53, 54, 55]
    assert choose_weeks([54, 53], [53, 54, 55], after_week=52) == [53, 54]
    with pytest.raises(ValueError, match="may have learned"):
        choose_weeks([52, 53], [52, 53], after_week=52)
    with pytest.raises(ValueError, match="no shadow-scored weeks"):
        choose_weeks(None, [50, 52], after_week=52)


def test_models_as_of_week_uses_tag_else_training_config() -> None:
    assert models_as_of_week({"as_of_week": "56"}, 52) == 56
    assert models_as_of_week({}, 52) == 52


def test_labels_cutoff_waits_after_last_week() -> None:
    manifest = {"weeks": [{"week": 55, "end": "2024-10-12"}]}
    assert labels_cutoff(manifest, [53, 55], 28) == datetime(
        2024, 10, 12, 23, 59, 59, tzinfo=UTC
    ) + timedelta(days=28)


def _week_file(root: Path, week: int, ids: list[str], labels: list[int], avail: list[int]) -> None:
    d = root / "Win64" / f"week={week:02d}"
    d.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "sha256": ids,
                "week": pa.array([week] * len(ids), pa.int16()),
                "label": pa.array(labels, pa.int8()),
                "label_available_at": pa.array(avail, pa.timestamp("s", tz="UTC")),
            }
        ),
        d / "part.parquet",
    )


def test_read_week_labels(tmp_path: Path) -> None:
    _week_file(tmp_path, 53, ["a", "b"], [1, 0], [100, 200])
    _week_file(tmp_path, 54, ["c"], [1], [300])
    wl = read_week_labels(tmp_path, "Win64", [54, 53])
    assert wl.total_events == 3
    eid = make_event_id("Win64", "b")
    assert (wl.label[eid], wl.available_at[eid], wl.file_id[eid]) == (0, 200, "b")


def _labels() -> WeekLabels:
    ids = ["a", "b", "c", "d"]
    eids = [make_event_id("Win64", i) for i in ids]
    return WeekLabels(
        label=dict(zip(eids, [1, 0, 1, 0], strict=True)),
        available_at=dict(zip(eids, [100, 100, 100, 999], strict=True)),
        file_id=dict(zip(eids, ids, strict=True)),
        total_events=5,
    )


def _models() -> tuple[Any, Any]:
    raw = np.linspace(0, 1, 50)
    cal = IsotonicCalibrator.fit(raw, (raw > 0.5).astype(int))
    p = replace(loaded("Win64", "1", threshold=0.4, seed=1), calibrator=cal)
    c = replace(loaded("Win64", "2", threshold=0.6, seed=2), calibrator=cal)
    return p, c


def test_assemble_input_pairs_scores_with_labels_known_in_time() -> None:
    p, c = _models()
    rows = [
        (make_event_id("Win64", i), 53, sp, 0.4, sc, 0.6)
        for i, sp, sc in [("a", 0.9, 0.8), ("b", 0.1, 0.2), ("c", 0.5, 0.7), ("d", 0.3, 0.1)]
    ]
    g = assemble_input(
        rows,
        _labels(),
        datetime.fromtimestamp(500, UTC),
        p,
        c,
        lambda m: np.zeros((3, m.n_features), dtype=np.float32),
    )
    assert g.file_ids == ["a", "b", "c"]  # d's label only arrives at t=999
    assert g.unlabelled_events == 1 and g.paired_events == 4 and g.expected_events == 5
    assert g.y.tolist() == [1, 0, 1]
    assert g.candidate.scores.tolist() == [0.8, 0.2, 0.7]
    assert g.production.never_break_scores.shape == (3,)
    assert ((g.candidate.probabilities >= 0) & (g.candidate.probabilities <= 1)).all()


def test_assemble_input_refuses_unknown_events_and_threshold_mismatch() -> None:
    p, c = _models()

    def nb(m: Any) -> npt.NDArray[np.float32]:
        return np.zeros((0, m.n_features), dtype=np.float32)

    cut = datetime.fromtimestamp(500, UTC)
    with pytest.raises(ValueError, match="not in the week files"):
        assemble_input([("Win64:zzz", 53, 0.1, 0.4, 0.1, 0.6)], _labels(), cut, p, c, nb)
    with pytest.raises(ValueError, match="stored thresholds"):
        assemble_input(
            [(make_event_id("Win64", "a"), 53, 0.1, 0.5, 0.1, 0.6)], _labels(), cut, p, c, nb
        )
    with pytest.raises(ValueError, match="no calibrator"):
        assemble_input(
            [(make_event_id("Win64", "a"), 53, 0.1, 0.4, 0.1, 0.6)],
            _labels(),
            cut,
            replace(p, calibrator=None),
            c,
            nb,
        )


def test_policy_file_is_valid(policy: GatePolicy) -> None:
    assert policy.operating_fpr == 0.001
    assert policy.min_disagreements == 30
    assert math.isclose(policy.alpha, 0.05)
    assert policy.soft_checks == ["calibration"]
    assert policy.file_types == ["Win64", "Win32"]


def test_policy_file_type_must_be_chosen_and_covered(policy: GatePolicy) -> None:
    assert policy.file_type("Win32") == "Win32"
    with pytest.raises(ValueError, match="not covered"):
        policy.file_type("PDF")
    with pytest.raises(ValueError, match="choose a file type"):
        policy.file_type(None)
    assert policy.model_copy(update={"file_types": ["Win64"]}).file_type(None) == "Win64"
    assert _bundle(policy)["file_type"] == "Win64"
