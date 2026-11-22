"""Ring rollout state machine and controller (Stage 6). Reasoning: docs/DECISIONS.md, Stage 6.

    APPROVED -> RING0 -> RING1 -> RING2 -> COMPLETE        (any ring state -> ROLLED_BACK)

- The registry aliases each state needs are a pure function of the state
  (`desired_aliases`); the controller writes the transition FIRST (append-only table) and
  then reconciles the aliases to it on every tick, so a crash or a race with the kill switch
  heals on the next tick.
- `evaluate` turns the measured watch signals into advance / hold / rollback with a plain
  reason. A breach rolls back at once, even before the ring has enough events; advancing
  needs enough events AND every signal passing.
- I/O sits behind small protocols (registry, transition store, meter) so the whole loop is
  unit-tested without MLflow, PostgreSQL or the API (adapters: sentinel.rollout.store).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from sentinel.config import RING_ORDER, RingsConfig, RolloutConfig
from sentinel.logging import get_logger
from sentinel.models.evaluate import rate_increase_check, wilson_interval
from sentinel.replay.events import ring_for

log = get_logger(__name__)


class State(StrEnum):
    APPROVED = "APPROVED"
    RING0 = "RING0"
    RING1 = "RING1"
    RING2 = "RING2"
    COMPLETE = "COMPLETE"
    ROLLED_BACK = "ROLLED_BACK"


TERMINAL = frozenset({State.COMPLETE, State.ROLLED_BACK})
NEXT = {
    State.APPROVED: State.RING0,
    State.RING0: State.RING1,
    State.RING1: State.RING2,
    State.RING2: State.COMPLETE,
}
STATE_RING = {State.RING0: "ring0", State.RING1: "ring1", State.RING2: "ring2"}
_RINGS_ON = {
    State.RING0: 1,
    State.RING1: 2,
    State.RING2: 3,
    State.COMPLETE: 3,
}  # how many rings (in RING_ORDER) serve the candidate


def canary_tenant(ring: str, rings: RingsConfig, prefix: str, limit: int = 10_000) -> str:
    """The first `<prefix><ring>-NNNN` tenant id that the stable ring hash puts in `ring`."""
    for i in range(limit):
        tenant = f"{prefix}{ring}-{i:04d}"
        if ring_for(tenant, rings) == ring:
            return tenant
    raise ValueError(f"no canary tenant found for {ring}")  # pragma: no cover


def desired_aliases(
    state: State, candidate: str, production: str, cfg: RolloutConfig
) -> dict[str, str]:
    """Registry aliases this state requires. `production` moves only on COMPLETE."""
    on = _RINGS_ON.get(state, 0)
    out = {
        cfg.ring_aliases[r]: candidate if i < on else production for i, r in enumerate(RING_ORDER)
    }
    if state is State.COMPLETE:
        out[cfg.fallback_alias] = candidate
    return out


# ---------------------------------------------------------------------------- signals


@dataclass(frozen=True)
class RingTraffic:
    """From the lineage table (model verdict = score > threshold; canaries excluded)."""

    ring: str
    candidate_events: int  # candidate-scored events in this ring since the ring switched
    candidate_flagged: int
    # production-scored events over the SAME period (since this ring switched), from the
    # rings still on production: the malware share of the stream changes over time, so a
    # longer baseline would not be comparable (Stage 6 finding, docs/ROLLOUT_DEMO.md)
    production_events: int
    production_flagged: int
    canaries_in_ring: int  # never-break canaries the candidate scored in this ring
    canary_hits: int  # canaries the candidate flagged, in ANY ring


@dataclass(frozen=True)
class ServiceHealth:
    """From the API's request metrics for the candidate's model_version label."""

    candidate_requests: int
    candidate_errors: int  # HTTP 5xx
    candidate_p99_ms: float | None
    candidate_latency_samples: int
    production_p99_ms: float | None


@dataclass(frozen=True)
class WatchSignals:
    traffic: RingTraffic
    health: ServiceHealth | None  # None: the API's metrics could not be read

    def as_dict(self) -> dict[str, Any]:
        return {
            "traffic": asdict(self.traffic),
            "health": asdict(self.health) if self.health else None,
        }


CheckStatus = Literal["pass", "fail", "waiting"]
Action = Literal["advance", "hold", "rollback"]


@dataclass(frozen=True)
class SignalCheck:
    name: str
    status: CheckStatus
    summary: str
    numbers: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    action: Action
    reason: str
    checks: tuple[SignalCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "checks": [asdict(c) for c in self.checks],
        }


def _pct(k: int, n: int) -> str:
    return f"{100 * k / n:.2f}%" if n else "-"


def check_never_break(t: RingTraffic, cfg: RolloutConfig) -> SignalCheck:
    allowed = cfg.signals.never_break_hits_allowed
    need = cfg.canaries.required_per_ring
    nums = {"hits": t.canary_hits, "allowed": allowed, "scored_in_ring": t.canaries_in_ring}
    if t.canary_hits > allowed:
        return SignalCheck(
            "never_break",
            "fail",
            f"candidate flagged {t.canary_hits} never-break file(s) (allowed {allowed})",
            nums,
        )
    if t.canaries_in_ring < need:
        return SignalCheck(
            "never_break",
            "waiting",
            f"{t.canaries_in_ring} of {need} never-break canaries scored in {t.ring}",
            nums,
        )
    return SignalCheck(
        "never_break", "pass", f"0 of {t.canaries_in_ring} never-break canaries flagged", nums
    )


def check_volume(t: RingTraffic, cfg: RolloutConfig) -> SignalCheck:
    need = cfg.min_events[t.ring]  # type: ignore[index]
    nums = {"events": t.candidate_events, "required": need}
    if t.candidate_events < need:
        return SignalCheck(
            "volume", "waiting", f"{t.candidate_events:,} of {need:,} events in {t.ring}", nums
        )
    return SignalCheck("volume", "pass", f"{t.candidate_events:,} events in {t.ring}", nums)


def check_flag_rate(t: RingTraffic, cfg: RolloutConfig) -> SignalCheck:
    s = cfg.signals
    if t.ring == RING_ORDER[-1]:
        return SignalCheck(
            "flag_rate",
            "pass",
            f"not compared in {t.ring}: no production traffic runs alongside the last ring "
            "(compared on the same period in the earlier rings)",
            {"candidate": t.candidate_flagged, "candidate_n": t.candidate_events},
        )
    cmp = rate_increase_check(
        t.candidate_flagged,
        t.candidate_events,
        t.production_flagged,
        t.production_events,
        s.max_flag_rate_increase_relative,
        s.confidence,
    )
    nums = cmp.as_dict()
    text = (
        f"candidate flags {_pct(t.candidate_flagged, t.candidate_events)} of "
        f"{t.candidate_events:,} files in {t.ring} (CI {100 * cmp.candidate_ci[0]:.2f}-"
        f"{100 * cmp.candidate_ci[1]:.2f}%) vs production "
        f"{_pct(t.production_flagged, t.production_events)} of {t.production_events:,}"
    )
    if cmp.breach:
        return SignalCheck(
            "flag_rate",
            "fail",
            f"{text}: clearly above production's +"
            f"{100 * s.max_flag_rate_increase_relative:.0f}% limit",
            nums,
        )
    if not (t.candidate_events and t.production_events):
        return SignalCheck("flag_rate", "waiting", f"{text}: nothing to compare yet", nums)
    return SignalCheck("flag_rate", "pass", text, nums)


def check_errors(h: ServiceHealth | None, cfg: RolloutConfig) -> SignalCheck:
    if h is None:
        return SignalCheck("errors", "waiting", "API metrics unavailable")
    s = cfg.signals
    lo, hi = wilson_interval(h.candidate_errors, h.candidate_requests, s.confidence)
    nums = {
        "requests": h.candidate_requests,
        "errors": h.candidate_errors,
        "ci": [lo, hi],
        "max_error_rate": s.max_error_rate,
    }
    text = f"{h.candidate_errors} server errors in {h.candidate_requests:,} candidate requests"
    if h.candidate_requests and lo > s.max_error_rate:
        return SignalCheck(
            "errors", "fail", f"{text} (clearly above {100 * s.max_error_rate:.1f}%)", nums
        )
    if not h.candidate_requests:
        return SignalCheck("errors", "waiting", "no candidate requests yet", nums)
    return SignalCheck("errors", "pass", text, nums)


def check_latency(h: ServiceHealth | None, cfg: RolloutConfig) -> SignalCheck:
    if h is None:
        return SignalCheck("latency", "waiting", "API metrics unavailable")
    s = cfg.signals
    nums = {
        "candidate_p99_ms": h.candidate_p99_ms,
        "production_p99_ms": h.production_p99_ms,
        "samples": h.candidate_latency_samples,
        "required_samples": s.min_latency_samples,
    }
    if (
        h.candidate_latency_samples < s.min_latency_samples
        or h.candidate_p99_ms is None
        or h.production_p99_ms is None
    ):
        return SignalCheck(
            "latency",
            "waiting",
            f"{h.candidate_latency_samples} of {s.min_latency_samples} latency samples",
            nums,
        )
    limit = h.production_p99_ms * s.max_p99_ratio + s.p99_slack_ms
    nums["limit_ms"] = limit
    text = f"p99 {h.candidate_p99_ms:.1f} ms vs production {h.production_p99_ms:.1f} ms"
    if h.candidate_p99_ms > limit:
        return SignalCheck("latency", "fail", f"{text} (limit {limit:.1f} ms)", nums)
    return SignalCheck("latency", "pass", text, nums)


def evaluate(signals: WatchSignals, cfg: RolloutConfig) -> Verdict:
    """Any fail -> rollback; else any waiting -> hold; else advance."""
    t, h = signals.traffic, signals.health
    checks = (
        check_never_break(t, cfg),
        check_flag_rate(t, cfg),
        check_errors(h, cfg),
        check_latency(h, cfg),
        check_volume(t, cfg),
    )
    failed = [c for c in checks if c.status == "fail"]
    if failed:
        return Verdict(
            "rollback",
            f"Rolled back in {t.ring}: " + "; ".join(f"[{c.name}] {c.summary}" for c in failed),
            checks,
        )
    waiting = [c for c in checks if c.status == "waiting"]
    if waiting:
        return Verdict(
            "hold", "Waiting: " + "; ".join(f"[{c.name}] {c.summary}" for c in waiting), checks
        )
    return Verdict(
        "advance",
        f"{t.ring} healthy: " + "; ".join(f"[{c.name}] {c.summary}" for c in checks),
        checks,
    )


# ---------------------------------------------------------------------------- controller


@dataclass(frozen=True)
class Transition:
    rollout_id: str
    seq: int
    file_type: str
    model_name: str
    candidate_version: str
    production_version: str
    from_state: State | None
    to_state: State
    actor: str
    reason: str
    signals: dict[str, Any]
    ts: datetime

    @property
    def candidate_label(self) -> str:
        return f"{self.model_name}/{self.candidate_version}"

    @property
    def production_label(self) -> str:
        return f"{self.model_name}/{self.production_version}"


class TransitionConflictError(RuntimeError):
    """Someone else (the kill switch, another controller) moved the rollout first."""


class TransitionStore(Protocol):
    def latest(self, file_type: str) -> Transition | None: ...

    def first(self, rollout_id: str) -> Transition: ...

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
        """Insert seq = prev.seq + 1 (1 if prev is None) only if `prev` is still the latest
        transition of this file type; otherwise raise TransitionConflictError."""
        ...

    def promote_decision(self, file_type: str, candidate: str, production: str) -> str | None:
        """decision_id of a gate PROMOTE for exactly this pair, if any."""
        ...


class Registry(Protocol):
    def aliases(self, name: str) -> dict[str, str]: ...

    def set_alias(self, name: str, alias: str, version: str) -> None: ...

    def version_tags(self, name: str, version: str) -> dict[str, str]: ...


class Meter(Protocol):
    def traffic(self, current: Transition, rollout_start: Transition, ring: str) -> RingTraffic: ...

    def health(self, candidate_label: str, production_label: str) -> ServiceHealth | None: ...


class CanarySender(Protocol):
    def send(self, ring: str, file_type: str, candidate_version: str) -> int:
        """Score the never-break files as a canary tenant of `ring` through the API, but only
        once the API serves `candidate_version` there; returns how many were scored."""
        ...


class RolloutError(RuntimeError):
    pass


def reconcile(registry: Registry, t: Transition, cfg: RolloutConfig) -> dict[str, str]:
    """Make the registry aliases match transition `t`'s state. Returns the aliases moved."""
    want = desired_aliases(t.to_state, t.candidate_version, t.production_version, cfg)
    have = registry.aliases(t.model_name)
    moved: dict[str, str] = {}
    for alias, version in want.items():
        if have.get(alias) != version:
            registry.set_alias(t.model_name, alias, version)
            moved[alias] = version
    if moved:
        log.info("rollout_aliases_moved", rollout_id=t.rollout_id, state=t.to_state, moved=moved)
    return moved


class Controller:
    def __init__(
        self,
        store: TransitionStore,
        registry: Registry,
        meter: Meter,
        canaries: CanarySender,
        cfg: RolloutConfig,
        approved_alias: str,
    ) -> None:
        self.store, self.registry, self.meter, self.cfg = store, registry, meter, cfg
        self.canaries = canaries
        self.approved_alias = approved_alias

    def start(
        self, file_type: str, model_name: str, candidate: str, now: datetime, demo: bool = False
    ) -> Transition:
        """Open a rollout (state APPROVED). Needs the gate's PROMOTE for exactly this
        candidate vs the current production and the approved alias on the candidate; with
        `demo`, a version tagged demo=true instead (recorded as such)."""
        aliases = self.registry.aliases(model_name)
        production = aliases.get(self.cfg.fallback_alias)
        if production is None:
            raise RolloutError(f"{model_name} has no {self.cfg.fallback_alias!r} alias")
        if candidate == production:
            raise RolloutError(f"{model_name}/{candidate} is already production")
        latest = self.store.latest(file_type)
        if latest is not None and latest.to_state not in TERMINAL:
            raise RolloutError(f"rollout {latest.rollout_id} is still {latest.to_state}")
        if demo:
            tags = self.registry.version_tags(model_name, candidate)
            if tags.get(self.cfg.demo_tag) != "true":
                raise RolloutError(
                    f"--demo needs a version tagged {self.cfg.demo_tag}=true; "
                    f"{model_name}/{candidate} is not"
                )
            reason = (
                f"DEMO approval: {model_name}/{candidate} is tagged {self.cfg.demo_tag}=true; "
                "the release gate was skipped for the Stage 6 demonstration"
            )
        else:
            if aliases.get(self.approved_alias) != candidate:
                raise RolloutError(
                    f"{model_name}/{candidate} does not hold the {self.approved_alias!r} alias "
                    "(only the release gate sets it, on PROMOTE)"
                )
            did = self.store.promote_decision(
                file_type, f"{model_name}/{candidate}", f"{model_name}/{production}"
            )
            if did is None:
                raise RolloutError(
                    f"no gate PROMOTE for {model_name}/{candidate} vs production "
                    f"{model_name}/{production}"
                )
            reason = f"gate decision {did}: PROMOTE"
        rollout_id = f"{file_type}-v{candidate}-{now.strftime('%Y%m%dT%H%M%SZ')}"
        t = self.store.append(
            latest,
            rollout_id=rollout_id,
            file_type=file_type,
            model_name=model_name,
            candidate_version=candidate,
            production_version=production,
            to_state=State.APPROVED,
            actor="controller",
            reason=reason,
            signals={},
        )
        reconcile(self.registry, t, self.cfg)  # every ring on production to begin with
        log.info("rollout_started", rollout_id=rollout_id, reason=reason)
        return t

    def _move(
        self, cur: Transition, to: State, reason: str, signals: Mapping[str, Any]
    ) -> Transition:
        t = self.store.append(
            cur,
            rollout_id=cur.rollout_id,
            file_type=cur.file_type,
            model_name=cur.model_name,
            candidate_version=cur.candidate_version,
            production_version=cur.production_version,
            to_state=to,
            actor="controller",
            reason=reason,
            signals=signals,
        )
        log.info("rollout_transition", rollout_id=t.rollout_id, to=to, reason=reason)
        return t

    def tick(self, file_type: str) -> tuple[Transition, Verdict | None]:
        """One controller step: read the recorded state, reconcile aliases, measure, decide.

        Returns the (possibly new) latest transition and the verdict (None when no
        measurement was due)."""
        cur = self.store.latest(file_type)
        if cur is None:
            raise RolloutError(f"no rollout for {file_type}")
        reconcile(self.registry, cur, self.cfg)
        if cur.to_state in TERMINAL:
            return cur, None
        try:
            if cur.to_state is State.APPROVED:
                nxt = self._move(cur, State.RING0, "approved: candidate starts in ring0", {})
                reconcile(self.registry, nxt, self.cfg)
                return nxt, None
            ring = STATE_RING[cur.to_state]
            traffic = self.meter.traffic(cur, self.store.first(cur.rollout_id), ring)
            if traffic.canaries_in_ring < self.cfg.canaries.required_per_ring:
                sent = self.canaries.send(ring, file_type, cur.candidate_version)
                if sent:
                    log.info("rollout_canaries_sent", rollout_id=cur.rollout_id, ring=ring, n=sent)
            signals = WatchSignals(
                traffic, self.meter.health(cur.candidate_label, cur.production_label)
            )
            verdict = evaluate(signals, self.cfg)
            evidence = {"verdict": verdict.as_dict(), "signals": signals.as_dict()}
            if verdict.action == "hold":
                log.info("rollout_hold", rollout_id=cur.rollout_id, reason=verdict.reason)
                return cur, verdict
            to = State.ROLLED_BACK if verdict.action == "rollback" else NEXT[cur.to_state]
            nxt = self._move(cur, to, verdict.reason, evidence)
        except TransitionConflictError:
            log.warning("rollout_conflict", rollout_id=cur.rollout_id)
            latest = self.store.latest(file_type)
            assert latest is not None
            reconcile(self.registry, latest, self.cfg)
            return latest, None
        reconcile(self.registry, nxt, self.cfg)
        return nxt, verdict
