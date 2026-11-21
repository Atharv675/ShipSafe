"""Run the release gate for one candidate version against the current production model.

    python -m sentinel.gate.run --candidate 2 [--weeks 53 54 55]

1. Resolve production (alias) and the candidate (explicit version); refuse to evaluate on
   any week either model may have learned from (week <= its as_of_week).
2. Pair live (production) and shadow (candidate) scores on the SAME event ids; attach labels
   from the week files, keeping only labels known at decision time (late labels).
3. Run the checks (sentinel.gate.checks), decide, build the evidence bundle, store its
   SHA-256 + text in the append-only `gate_decisions` table, write JSON + Markdown reports.
4. On PROMOTE only: move the `candidate_approved` alias. Production is never touched.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq

from sentinel.config import REPO_ROOT, GatePolicy
from sentinel.data.schema import ID_COLUMN, LABEL_AVAILABLE_COLUMN, LABEL_COLUMN, WEEK_COLUMN
from sentinel.data.splits import week_end
from sentinel.gate.checks import CheckResult, GateInput, ModelScores, decide, run_checks
from sentinel.gate.evidence import (
    BUNDLE_SCHEMA_VERSION,
    bundle_sha256,
    canonical_json,
    render_markdown,
    verify_bundle,
)
from sentinel.logging import get_logger
from sentinel.replay.events import make_event_id
from sentinel.serving.models import LoadedModel

log = get_logger(__name__)

# (event_id, week, production score, production threshold, candidate score, candidate threshold)
PairedRow = tuple[str, int, float, float, float, float]

SELECT_PAIRED = """
SELECT p.event_id, s.week, p.score, p.threshold, s.score, s.threshold
FROM predictions p JOIN shadow_predictions s ON s.event_id = p.event_id
WHERE p.model_version = %s AND s.model_version = %s AND s.week = ANY(%s)
ORDER BY p.event_id
"""
SELECT_SHADOW_WEEKS = "SELECT DISTINCT week FROM shadow_predictions WHERE model_version = %s"
INSERT_DECISION = """
INSERT INTO gate_decisions (decision_id, file_type, policy_version, production_version,
    candidate_version, decision, reason, bundle_sha256, bundle_json)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


@dataclass(frozen=True)
class WeekLabels:
    """Ground truth of the evaluated weeks, keyed by event id."""

    label: dict[str, int]
    available_at: dict[str, int]  # epoch seconds
    file_id: dict[str, str]
    total_events: int


def read_week_labels(splits_root: Path, file_type: str, weeks: Sequence[int]) -> WeekLabels:
    label: dict[str, int] = {}
    avail: dict[str, int] = {}
    fid: dict[str, str] = {}
    total = 0
    for w in sorted(weeks):
        t = pq.read_table(
            splits_root / file_type / f"week={w:02d}" / "part.parquet",
            columns=[ID_COLUMN, WEEK_COLUMN, LABEL_COLUMN, LABEL_AVAILABLE_COLUMN],
        )
        ids = [str(v) for v in t.column(ID_COLUMN).to_pylist()]
        ys = t.column(LABEL_COLUMN).to_numpy()
        av = (
            t.column(LABEL_AVAILABLE_COLUMN)
            .cast(pa.timestamp("s", tz="UTC"))
            .cast(pa.int64())
            .to_numpy()
        )
        for i, sha in enumerate(ids):
            eid = make_event_id(file_type, sha)
            label[eid], avail[eid], fid[eid] = int(ys[i]), int(av[i]), sha
        total += t.num_rows
    return WeekLabels(label, avail, fid, total)


def models_as_of_week(tags: Mapping[str, str], default: int) -> int:
    """Last week a model may have learned from: its `as_of_week` tag (candidates), else the
    Stage 2 training config's as_of_week (v1 has no such tag)."""
    return int(tags.get("as_of_week", default))


def choose_weeks(
    requested: Sequence[int] | None, available: Sequence[int], after_week: int
) -> list[int]:
    """Weeks to evaluate: all strictly after both models' as_of weeks."""
    if requested:
        bad = sorted(w for w in requested if w <= after_week)
        if bad:
            raise ValueError(
                f"weeks {bad} are not after week {after_week}: a model may have learned from them"
            )
        return sorted(set(requested))
    weeks = sorted(w for w in set(available) if w > after_week)
    if not weeks:
        raise ValueError(f"no shadow-scored weeks after week {after_week}")
    return weeks


def assemble_input(
    rows: Sequence[PairedRow],
    labels: WeekLabels,
    labels_as_of: datetime,
    production: LoadedModel,
    candidate: LoadedModel,
    never_break: Callable[[LoadedModel], npt.NDArray[np.float32]],
) -> GateInput:
    """Join paired scores with labels known at decision time; check stored thresholds."""
    cut = int(labels_as_of.timestamp())
    keep = [r for r in rows if r[0] in labels.label and labels.available_at[r[0]] <= cut]
    unknown = [r[0] for r in rows if r[0] not in labels.label]
    if unknown:
        raise ValueError(f"{len(unknown)} scored events are not in the week files: {unknown[:3]}")
    for r in rows:
        if r[3] != production.threshold or r[5] != candidate.threshold:
            raise ValueError(
                f"event {r[0]}: stored thresholds ({r[3]}, {r[5]}) differ from the registered "
                f"models' ({production.threshold}, {candidate.threshold})"
            )

    def scored(m: LoadedModel, s: npt.NDArray[np.float64]) -> ModelScores:
        if m.calibrator is None:
            raise ValueError(f"{m.label} has no calibrator")
        nb = never_break(m)
        return ModelScores(
            label=m.label,
            threshold=m.threshold,
            scores=s,
            probabilities=np.asarray(m.calibrator.predict(s), dtype=np.float64),
            never_break_scores=m.score(nb) if nb.size else np.zeros(0),
        )

    return GateInput(
        file_ids=[labels.file_id[r[0]] for r in keep],
        y=np.array([labels.label[r[0]] for r in keep], dtype=np.int8),
        week=np.array([r[1] for r in keep], dtype=np.int64),
        production=scored(production, np.array([r[2] for r in keep], dtype=np.float64)),
        candidate=scored(candidate, np.array([r[4] for r in keep], dtype=np.float64)),
        expected_events=labels.total_events,
        paired_events=len(rows),
        unlabelled_events=len(rows) - len(keep),
    )


def build_bundle(
    *,
    decision_id: str,
    created_at: datetime,
    file_type: str,
    policy: GatePolicy,
    results: list[CheckResult],
    production: Mapping[str, Any],
    candidate: Mapping[str, Any],
    weeks: list[int],
    labels_as_of: datetime,
    manifest: Mapping[str, Any],
    git_commit: str,
) -> dict[str, Any]:
    decision, reason = decide(results)
    week_files = {
        str(w["week"]): w.get("sha256") for w in manifest["weeks"] if int(w["week"]) in weeks
    }
    return {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "decision_id": decision_id,
        "created_at": created_at.isoformat(),
        "file_type": policy.file_type(file_type),
        "decision": decision,
        "reason": reason,
        "policy": policy.model_dump(),
        "models": {"production": dict(production), "candidate": dict(candidate)},
        "evaluation": {"weeks": weeks, "labels_as_of": labels_as_of.isoformat()},
        "data": {
            "data_fingerprint": manifest["data_fingerprint"],
            "feature_schema_version": manifest["feature_schema_version"],
            "week_files_sha256": week_files,
            "never_break": manifest.get("never_break"),
        },
        "checks": [r.as_dict() for r in results],
        "git_commit": git_commit,
        "seed": None,  # the gate draws no random numbers; model seeds are under "models"
    }


def labels_cutoff(manifest: Mapping[str, Any], weeks: Sequence[int], wait_days: float) -> datetime:
    return week_end(dict(manifest), max(weeks)) + timedelta(days=wait_days)


def decision_id_for(file_type: str, prod: str, cand: str, now: datetime) -> str:
    return f"{file_type}-v{cand}-vs-v{prod}-{now.strftime('%Y%m%dT%H%M%SZ')}"


def main() -> None:  # pragma: no cover - exercised by `make gate` against the running stack
    import sys

    if sys.argv[1:2] == ["verify"]:
        verify_main()
        return

    import psycopg
    from mlflow import MlflowClient

    from sentinel.config import Settings, load_gate_policy, load_train_config
    from sentinel.data.splits import load_manifest
    from sentinel.logging import configure_logging
    from sentinel.provenance import git_commit
    from sentinel.serving.models import MlflowModelSource

    settings = Settings()
    configure_logging(settings.log_level)
    policy = load_gate_policy()
    train = load_train_config()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file-type", choices=policy.file_types, help="needed if several are covered")
    p.add_argument("--candidate", required=True, help="registered version number of the candidate")
    p.add_argument("--weeks", type=int, nargs="*", help="default: every shadow-scored week after T")
    p.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "gate")
    args = p.parse_args()

    try:
        ft = policy.file_type(args.file_type)
    except ValueError as exc:
        p.error(str(exc))
    name = train.mlflow.model_name(ft)
    client = MlflowClient(tracking_uri=settings.tracking_uri, registry_uri=settings.tracking_uri)
    prod_v = str(client.get_model_version_by_alias(name, train.mlflow.production_alias).version)
    cand_v = str(args.candidate)
    if cand_v == prod_v:
        raise SystemExit(f"{name}/{cand_v} is already production")
    source = MlflowModelSource(settings.tracking_uri, {name: ft}, predict_threads=0)
    prod, cand = source.load(name, prod_v), source.load(name, cand_v)
    tags = {v: dict(client.get_model_version(name, v).tags) for v in (prod_v, cand_v)}
    as_of = {v: models_as_of_week(tags[v], train.weeks.as_of_week) for v in (prod_v, cand_v)}

    manifest = load_manifest(settings.splits_dir, ft)
    nb_path = settings.clean_dir / ft / "never_break.parquet"

    def never_break(m: LoadedModel) -> npt.NDArray[np.float32]:
        t = pq.read_table(nb_path, columns=list(m.features))
        return np.column_stack([t.column(f).to_numpy() for f in m.features]).astype(np.float32)

    with psycopg.connect(os.environ["SENTINEL_GATE_DSN"], autocommit=True) as conn:
        available = [r[0] for r in conn.execute(SELECT_SHADOW_WEEKS, (cand.label,)).fetchall()]
        weeks = choose_weeks(args.weeks, available, max(as_of.values()))
        rows: list[PairedRow] = conn.execute(
            SELECT_PAIRED, (prod.label, cand.label, weeks)
        ).fetchall()
        cutoff = labels_cutoff(manifest, weeks, policy.label_wait_days)
        g = assemble_input(
            rows,
            read_week_labels(settings.splits_dir, ft, weeks),
            cutoff,
            prod,
            cand,
            never_break,
        )
        results = run_checks(g, policy)
        now = datetime.now(UTC).replace(microsecond=0)
        did = decision_id_for(ft, prod_v, cand_v, now)

        def describe(m: LoadedModel, v: str) -> dict[str, Any]:
            t = tags[v]
            return {
                "label": m.label,
                "version": v,
                "threshold": m.threshold,
                "as_of_week": as_of[v],
                "feature_schema_version": m.feature_schema_version,
                "policy": t.get("policy", "stage2_v1"),
                "data_fingerprint": t.get("data_fingerprint"),
                "git_commit": t.get("git_commit"),
                "seed": t.get("seed"),
            }

        bundle = build_bundle(
            decision_id=did,
            created_at=now,
            file_type=ft,
            policy=policy,
            results=results,
            production=describe(prod, prod_v),
            candidate=describe(cand, cand_v),
            weeks=weeks,
            labels_as_of=cutoff,
            manifest=manifest,
            git_commit=git_commit(REPO_ROOT),
        )
        text = canonical_json(bundle)
        sha = bundle_sha256(text)
        conn.execute(
            INSERT_DECISION,
            (
                did,
                ft,
                policy.policy_version,
                prod.label,
                cand.label,
                bundle["decision"],
                bundle["reason"],
                sha,
                text,
            ),
        )
    write_reports(args.out, did, text, render_markdown(bundle, sha))
    if bundle["decision"] == "PROMOTE":
        client.set_registered_model_alias(name, policy.approved_alias, cand_v)
    log.info(
        "gate_decision",
        decision_id=did,
        decision=bundle["decision"],
        reason=bundle["reason"],
        bundle_sha256=sha,
        approved_alias=policy.approved_alias if bundle["decision"] == "PROMOTE" else None,
        report=f"docs/gate/{did}.md",
    )


def write_reports(out: Path, decision_id: str, bundle_text: str, markdown: str) -> None:
    """The JSON file holds EXACTLY the hashed text (no trailing newline), so it verifies."""
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{decision_id}.json").write_bytes(bundle_text.encode("utf-8"))
    (out / f"{decision_id}.md").write_text(markdown, encoding="utf-8")


def verify_reports(stored: Sequence[tuple[str, str, str]], out: Path) -> list[str]:
    """Check (decision_id, bundle_sha256, bundle_json) rows from `gate_decisions`: the stored
    text must hash to the stored SHA-256, and the JSON report file (if present) must be
    byte-identical. Returns the problems found (empty = all good)."""
    problems: list[str] = []
    for did, sha, text in stored:
        if not verify_bundle(text, sha):
            problems.append(f"{did}: stored bundle does not match its stored SHA-256")
        f = out / f"{did}.json"
        if not f.exists():
            problems.append(f"{did}: report file {f.name} is missing")
        elif not verify_bundle(f.read_bytes().decode("utf-8"), sha):
            problems.append(f"{did}: {f.name} was edited (SHA-256 differs from gate_decisions)")
    return problems


def verify_main() -> None:  # pragma: no cover - `make gate-verify`
    import psycopg

    from sentinel.config import Settings
    from sentinel.logging import configure_logging

    configure_logging(Settings().log_level)
    with psycopg.connect(os.environ["SENTINEL_GATE_DSN"], autocommit=True) as conn:
        rows: list[tuple[str, str, str]] = conn.execute(
            "SELECT decision_id, bundle_sha256, bundle_json FROM gate_decisions ORDER BY created_at"
        ).fetchall()
    problems = verify_reports(rows, REPO_ROOT / "docs" / "gate")
    for msg in problems:
        log.error("gate_evidence_mismatch", problem=msg)
    log.info("gate_evidence_verified", decisions=len(rows), problems=len(problems))
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
