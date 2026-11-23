"""Adapters for the rollout controller: PostgreSQL transitions + lineage, MLflow, the API.

Also `emergency_rollback`, shared by the kill switch (POST /admin/rollback) and the CLI.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow.parquet as pq

from sentinel.config import RING_ORDER, RingsConfig, RolloutConfig
from sentinel.data.schema import ID_COLUMN
from sentinel.data.splits import feature_columns, load_manifest
from sentinel.logging import get_logger
from sentinel.rollout.controller import (
    TERMINAL,
    Registry,
    RingTraffic,
    ServiceHealth,
    State,
    Transition,
    TransitionConflictError,
    TransitionStore,
    canary_tenant,
)

log = get_logger(__name__)

_COLS = (
    "rollout_id, seq, file_type, model_name, candidate_version, production_version, "
    "from_state, to_state, actor, reason, signals, ts"
)
SELECT_LATEST = (
    f"SELECT {_COLS} FROM rollout_transitions WHERE file_type = %s ORDER BY id DESC LIMIT 1"
)
SELECT_FIRST = f"SELECT {_COLS} FROM rollout_transitions WHERE rollout_id = %s ORDER BY seq LIMIT 1"
SELECT_ROLLOUT = f"SELECT {_COLS} FROM rollout_transitions WHERE rollout_id = %s ORDER BY seq"
INSERT = (
    f"INSERT INTO rollout_transitions ({_COLS}) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now()) RETURNING ts"
)
# One writer at a time per file type (controller and kill switch), inside the transaction.
LOCK = "SELECT pg_advisory_xact_lock(hashtext('sentinel-rollout:' || %s))"
SELECT_PROMOTE = (
    "SELECT decision_id FROM gate_decisions WHERE file_type = %s AND decision = 'PROMOTE' "
    "AND candidate_version = %s AND production_version = %s ORDER BY created_at DESC LIMIT 1"
)
SELECT_BUNDLE = "SELECT bundle_json FROM gate_decisions WHERE decision_id = %s"


def _row(r: tuple[Any, ...]) -> Transition:
    sig = r[10] if isinstance(r[10], dict) else json.loads(r[10] or "{}")
    return Transition(
        rollout_id=r[0],
        seq=int(r[1]),
        file_type=r[2],
        model_name=r[3],
        candidate_version=r[4],
        production_version=r[5],
        from_state=State(r[6]) if r[6] else None,
        to_state=State(r[7]),
        actor=r[8],
        reason=r[9],
        signals=sig,
        ts=r[11],
    )


class PostgresTransitionStore:  # pragma: no cover - SQL exercised by integration tests
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self) -> Any:
        import psycopg

        return psycopg.connect(self._dsn, connect_timeout=5)

    def latest(self, file_type: str) -> Transition | None:
        with self._connect() as conn:
            r = conn.execute(SELECT_LATEST, (file_type,)).fetchone()
        return _row(r) if r else None

    def first(self, rollout_id: str) -> Transition:
        with self._connect() as conn:
            r = conn.execute(SELECT_FIRST, (rollout_id,)).fetchone()
        if r is None:
            raise LookupError(rollout_id)
        return _row(r)

    def history(self, rollout_id: str) -> list[Transition]:
        with self._connect() as conn:
            return [_row(r) for r in conn.execute(SELECT_ROLLOUT, (rollout_id,)).fetchall()]

    def append(
        self,
        prev: Transition | None,
        *,
        rollout_id: str,
        file_type: str,
        model_name: str,
        candidate_version: str,
        production_version: str,
        to_state: State,
        actor: str,
        reason: str,
        signals: Mapping[str, Any],
    ) -> Transition:
        with self._connect() as conn, conn.transaction():
            conn.execute(LOCK, (file_type,))
            r = conn.execute(SELECT_LATEST, (file_type,)).fetchone()
            now_latest = _row(r) if r else None
            same = (now_latest is None and prev is None) or (
                now_latest is not None
                and prev is not None
                and (now_latest.rollout_id, now_latest.seq) == (prev.rollout_id, prev.seq)
            )
            if not same:
                raise TransitionConflictError(f"{file_type}: latest transition changed")
            seq = prev.seq + 1 if prev is not None and prev.rollout_id == rollout_id else 1
            from_state = prev.to_state.value if prev is not None and seq > 1 else None
            inserted = conn.execute(
                INSERT,
                (
                    rollout_id,
                    seq,
                    file_type,
                    model_name,
                    candidate_version,
                    production_version,
                    from_state,
                    to_state.value,
                    actor,
                    reason,
                    json.dumps(signals, sort_keys=True, default=str),
                ),
            ).fetchone()
            assert inserted is not None  # RETURNING always yields the row
            ts = inserted[0]
        return Transition(
            rollout_id,
            seq,
            file_type,
            model_name,
            candidate_version,
            production_version,
            State(from_state) if from_state else None,
            to_state,
            actor,
            reason,
            dict(signals),
            ts,
        )

    def promote_decision(self, file_type: str, candidate: str, production: str) -> str | None:
        with self._connect() as conn:
            r = conn.execute(SELECT_PROMOTE, (file_type, candidate, production)).fetchone()
        return str(r[0]) if r else None

    def gate_bundle(self, decision_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            r = conn.execute(SELECT_BUNDLE, (decision_id,)).fetchone()
        if r is None:
            raise LookupError(decision_id)
        bundle: dict[str, Any] = json.loads(r[0])
        return bundle


class MlflowRegistry:  # pragma: no cover - thin MLflow wrapper (integration / demo)
    def __init__(self, tracking_uri: str) -> None:
        from mlflow import MlflowClient

        self._c = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)

    def aliases(self, name: str) -> dict[str, str]:
        return {a: str(v) for a, v in self._c.get_registered_model(name).aliases.items()}

    def set_alias(self, name: str, alias: str, version: str) -> None:
        self._c.set_registered_model_alias(name, alias, version)

    def version_tags(self, name: str, version: str) -> dict[str, str]:
        return dict(self._c.get_model_version(name, version).tags)


def like_prefix(prefix: str) -> str:
    """SQL LIKE pattern matching strings that start with `prefix` (wildcards escaped)."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


TRAFFIC_SQL = """
SELECT
  (SELECT count(*) FROM predictions WHERE model_version = %(cand)s AND ring = %(ring)s
     AND ts >= %(since)s AND tenant_id NOT LIKE %(canary)s),
  (SELECT count(*) FROM predictions WHERE model_version = %(cand)s AND ring = %(ring)s
     AND ts >= %(since)s AND tenant_id NOT LIKE %(canary)s AND score > threshold),
  (SELECT count(*) FROM predictions WHERE model_version = %(prod)s
     AND ts >= %(since)s AND tenant_id NOT LIKE %(canary)s),
  (SELECT count(*) FROM predictions WHERE model_version = %(prod)s
     AND ts >= %(since)s AND tenant_id NOT LIKE %(canary)s AND score > threshold),
  -- canaries: no time filter on purpose. Their event ids are fixed per (ring, file), so a
  -- version's canary rows from an earlier rollout are not written again; the model version
  -- and file are identical, so its verdicts are too.
  (SELECT count(*) FROM predictions WHERE model_version = %(cand)s AND ring = %(ring)s
     AND tenant_id LIKE %(canary)s),
  (SELECT count(*) FROM predictions WHERE model_version = %(cand)s
     AND tenant_id LIKE %(canary)s AND score > threshold),
  -- Stage 7 paired scoring: the same files, also scored by production at the same moment
  (SELECT count(*) FROM predictions WHERE model_version = %(cand)s AND ring = %(ring)s
     AND ts >= %(since)s AND tenant_id NOT LIKE %(canary)s AND reference_version = %(prod)s),
  (SELECT count(*) FROM predictions WHERE model_version = %(cand)s AND ring = %(ring)s
     AND ts >= %(since)s AND tenant_id NOT LIKE %(canary)s AND reference_version = %(prod)s
     AND score > threshold AND reference_verdict = 'benign'),
  (SELECT count(*) FROM predictions WHERE model_version = %(cand)s AND ring = %(ring)s
     AND ts >= %(since)s AND tenant_id NOT LIKE %(canary)s AND reference_version = %(prod)s
     AND score <= threshold AND reference_verdict = 'malicious')
"""


def health_from_stats(
    stats: list[dict[str, Any]], candidate: str, production: str
) -> ServiceHealth:
    """Candidate request/error counts and p99 latencies from GET /v1/ops/stats."""
    requests = errors = samples = 0
    p99: dict[str, float | None] = {candidate: None, production: None}
    for s in stats:
        labels = s.get("labels", {})
        mv = labels.get("model_version")
        if s.get("metric") == "requests" and mv == candidate:
            requests += int(s["count"])
            if str(labels.get("status", "")).startswith("5"):
                errors += int(s["count"])
        elif s.get("metric") == "latency_ms" and mv in p99:
            p99[mv] = s.get("p99")
            if mv == candidate:
                samples = int(s["count"])
    return ServiceHealth(requests, errors, p99[candidate], samples, p99[production])


class LineageMeter:  # pragma: no cover - SQL + HTTP, exercised by the demo
    def __init__(
        self,
        dsn: str,
        cfg: RolloutConfig,
        fetch_stats: Callable[[], list[dict[str, Any]]],
    ) -> None:
        self._dsn, self._cfg, self._fetch = dsn, cfg, fetch_stats

    def traffic(self, current: Transition, rollout_start: Transition, ring: str) -> RingTraffic:
        import psycopg

        with psycopg.connect(self._dsn, connect_timeout=5) as conn:
            r = conn.execute(
                TRAFFIC_SQL,
                {
                    "cand": current.candidate_label,
                    "prod": current.production_label,
                    "ring": ring,
                    "since": current.ts,
                    "start": rollout_start.ts,
                    "canary": like_prefix(self._cfg.canaries.tenant_prefix),
                },
            ).fetchone()
        assert r is not None  # an aggregate always returns one row
        return RingTraffic(ring, *(int(v) for v in r))

    def health(self, candidate_label: str, production_label: str) -> ServiceHealth | None:
        try:
            stats = self._fetch()
        except Exception as exc:
            log.warning("rollout_api_metrics_unavailable", error=type(exc).__name__)
            return None
        return health_from_stats(stats, candidate_label, production_label)


@dataclass(frozen=True)
class CanarySet:
    file_ids: list[str]
    features: npt.NDArray[np.float32]
    feature_schema_version: str


def load_canary_set(splits_root: Path, clean_root: Path, file_type: str) -> CanarySet:
    """The never-break files (Stage 1) in the model's feature order."""
    manifest = load_manifest(splits_root, file_type)
    feats = feature_columns(manifest)
    t = pq.read_table(clean_root / file_type / "never_break.parquet", columns=[ID_COLUMN, *feats])
    x = np.column_stack([t.column(f).to_numpy() for f in feats]).astype(np.float32)
    return CanarySet(
        [str(v) for v in t.column(ID_COLUMN).to_pylist()],
        x,
        str(manifest["feature_schema_version"]),
    )


def canary_event_id(file_type: str, ring: str, file_id: str) -> str:
    return f"{file_type}:canary:{ring}:{file_id}"


class ApiCanarySender:  # pragma: no cover - HTTP, exercised by the demo
    """Scores the never-break files through POST /v1/score as a canary tenant of the ring,
    only after GET /v1/rings shows that ring serving the candidate."""

    def __init__(
        self,
        client: Any,  # httpx.Client with base_url = the API
        cfg: RolloutConfig,
        rings: RingsConfig,
        load: Callable[[str], CanarySet],
        workers: int,
    ) -> None:
        self._client, self._cfg, self._rings, self._load = client, cfg, rings, load
        self._workers = workers
        self._sets: dict[str, CanarySet] = {}

    def send(self, ring: str, file_type: str, candidate_version: str) -> int:
        serving = self._client.get("/v1/rings").json()
        if serving.get(ring, {}).get(file_type) != candidate_version:
            return 0  # the alias move has not reached the API yet: next tick
        if file_type not in self._sets:
            self._sets[file_type] = self._load(file_type)
        cs = self._sets[file_type]
        tenant = canary_tenant(ring, self._rings, self._cfg.canaries.tenant_prefix)

        def post(i: int) -> int:
            body = {
                "file_id": cs.file_ids[i],
                "tenant_id": tenant,
                "features": cs.features[i].tolist(),
                "feature_schema_version": cs.feature_schema_version,
                "event_id": canary_event_id(file_type, ring, cs.file_ids[i]),
            }
            return int(self._client.post("/v1/score", json=body).status_code)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            codes = list(pool.map(post, range(len(cs.file_ids))))
        return sum(c == 200 for c in codes)


def emergency_rollback(
    store: TransitionStore,
    registry: Registry,
    model_names: Mapping[str, str],
    cfg: RolloutConfig,
    actor: str,
    reason: str,
) -> dict[str, dict[str, Any]]:
    """Point every ring alias back at production for each file type and record ROLLED_BACK
    for any rollout still in progress. The record is written first, so the controller's next
    reconcile cannot move the rings forward again; the aliases are moved even if recording
    fails (safety first) and the failure is reported."""
    out: dict[str, dict[str, Any]] = {}
    for ft, name in model_names.items():
        res: dict[str, Any] = {"recorded": None, "moved": {}}
        try:
            for _ in range(3):  # a concurrent controller move -> re-read and retry
                latest = store.latest(ft)
                if latest is None or latest.to_state in TERMINAL:
                    break
                try:
                    t = store.append(
                        latest,
                        rollout_id=latest.rollout_id,
                        file_type=ft,
                        model_name=latest.model_name,
                        candidate_version=latest.candidate_version,
                        production_version=latest.production_version,
                        to_state=State.ROLLED_BACK,
                        actor=actor,
                        reason=reason,
                        signals={},
                    )
                    res["recorded"] = t.rollout_id
                    break
                except TransitionConflictError:
                    continue
        except Exception as exc:
            res["record_error"] = f"{type(exc).__name__}: {exc}"
            log.error("kill_switch_record_failed", file_type=ft, error=res["record_error"])
        production = registry.aliases(name).get(cfg.fallback_alias)
        if production is None:
            res["error"] = f"{name} has no {cfg.fallback_alias!r} alias"
            out[ft] = res
            continue
        have = registry.aliases(name)
        for r in RING_ORDER:
            alias = cfg.ring_aliases[r]
            if have.get(alias) != production:
                registry.set_alias(name, alias, production)
                res["moved"][alias] = production
        res["production"] = production
        out[ft] = res
    log.warning("kill_switch", actor=actor, reason=reason, result=out)
    return out


def timeline_markdown(history: list[Transition]) -> str:
    """A Markdown table of a rollout's transitions (seconds since it started)."""
    lines = ["| t (s) | From | To | By | Reason |", "|---|---|---|---|---|"]
    for r in timeline_rows(history):
        reason = str(r["reason"]).replace("|", "\\|")
        lines.append(
            f"| {r['t_seconds']} | {r['from']} | **{r['to']}** | {r['actor']} | {reason} |"
        )
    return "\n".join(lines) + "\n"


def timeline_rows(history: list[Transition]) -> list[dict[str, Any]]:
    """Seconds since the rollout started, state change, actor, reason (for the demo doc)."""
    if not history:
        return []
    t0: datetime = history[0].ts
    return [
        {
            "t_seconds": round((t.ts - t0).total_seconds(), 1),
            "ts": t.ts.isoformat(),
            "from": t.from_state.value if t.from_state else "-",
            "to": t.to_state.value,
            "actor": t.actor,
            "reason": t.reason,
        }
        for t in history
    ]
