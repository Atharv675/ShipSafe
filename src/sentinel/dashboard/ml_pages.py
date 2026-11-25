"""ML section of the Streamlit app (Stage 9): eight pages.

ML Overview · Model Registry · Gate Decision Viewer · Head-to-head · Drift · Rollout Timeline ·
Experiment Results · Prediction Lookup.

Reads PostgreSQL (read-only role), the MLflow registry and $SENTINEL_DATA_DIR/results only.
Every chart has a one-line caption, a tooltip and a table view; colours from theme.py; the
sidebar filters (time window, model version, ring, tenant) apply wherever traffic is shown.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from sentinel.config import MLDashboardConfig
from sentinel.dashboard import data as d
from sentinel.dashboard import ml_data as m
from sentinel.dashboard import theme as th
from sentinel.dashboard import ui
from sentinel.dashboard.ops_pages import Context, _empty, _show, _timeseries

MODEL_COLORS = [th.SERIES[0], th.SERIES[1]]  # head-to-head: model A, model B (fixed order)
DECISION_STATUS = {"PROMOTE": "good", "REJECT": "critical", "INSUFFICIENT_EVIDENCE": "warning"}
CHECK_ICON = {
    "pass": ":material/check_circle:",
    "fail": ":material/cancel:",
    "n/a": ":material/remove_circle:",
}


@st.cache_data(ttl=60, show_spinner=False)
def _registry_versions(_registry: m.Registry, key: str) -> pd.DataFrame:
    """Registry versions change rarely; one MLflow round trip per minute, not per page."""
    return _registry.versions()


@st.cache_data(ttl=60, show_spinner=False)
def _flag_hourly(
    _src: d.Source, start: str, end: str, canary: str, filters: tuple[tuple[str, Any], ...]
) -> pd.DataFrame:
    return _src.query(
        "flag_hourly",
        m.FLAG_HOURLY_SQL,
        {
            "start": datetime.fromisoformat(start),
            "end": datetime.fromisoformat(end),
            "canary": canary,
            **dict(filters),
        },
    )


@dataclass(frozen=True)
class MLContext:
    ops: Context
    registry: m.Registry
    results: m.Results
    cfg: MLDashboardConfig
    filters: m.Filters
    ring_aliases: dict[str, str]
    fallback_alias: str
    operating_fpr: float
    cache: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def src(self) -> d.Source:
        return self.ops.src

    def versions(self) -> pd.DataFrame:
        if "versions" not in self.cache:
            try:
                self.cache["versions"] = _registry_versions(
                    self.registry, getattr(self.registry, "_uri", None) or str(id(self.registry))
                )
            except Exception as exc:  # registry down: pages still render from PostgreSQL
                st.warning(f"MLflow registry unavailable: {exc}", icon=":material/cloud_off:")
                self.cache["versions"] = pd.DataFrame(columns=m.REGISTRY_COLUMNS)
        out: pd.DataFrame = self.cache["versions"]
        return out

    def gates(self) -> pd.DataFrame:
        return self.src.query("gates", m.GATES_SQL, {})

    def transitions(self) -> pd.DataFrame:
        return self.src.query("transitions", m.TRANSITIONS_SQL, {})


def _filters_note(ctx: MLContext) -> None:
    st.caption(f"Filters: {ctx.filters.describe()}.")


def _status_card(
    col: Any, label: str, value: str, state: str, help: str, badge: str | None = None
) -> None:
    """Card with a status badge. `badge` names the outcome (e.g. "Rejected"): a gate
    rejecting a candidate is the gate working, not a "Critical" system state."""
    with col.container(border=True):
        st.caption(label)
        st.markdown(f"**{value}**")
        st.badge(
            badge or (th.STATUS_LABEL[state] if state != "info" else "Info"),
            icon=th.STATUS_ICON.get(state, ":material/info:"),
            color=th.BADGE_COLOR.get(state, "blue"),
            help=help,
        )


def _interval_chart(
    df: pd.DataFrame,
    y: str,
    domain: dict[str, str],
    x_format: str,
    tip_format: str,
    x_title: str | None = None,
) -> alt.LayerChart:
    """Dot = value, line = interval, one row per `y` (no facets: their row labels collide)."""
    color = alt.Color(
        f"{y}:N", scale=alt.Scale(domain=list(domain), range=list(domain.values())), legend=None
    )
    tip = [
        alt.Tooltip(f"{y}:N"),
        alt.Tooltip("value:Q", format=tip_format),
        alt.Tooltip("low:Q", format=tip_format),
        alt.Tooltip("high:Q", format=tip_format),
    ]
    base = alt.Chart(df)
    enc_y = alt.Y(f"{y}:N", title=None, sort=list(domain))
    x = alt.X(
        "low:Q",
        title=x_title,
        axis=alt.Axis(format=x_format),
        scale=alt.Scale(zero=False, nice=True),
    )
    rule = base.mark_rule(strokeWidth=3).encode(x=x, x2="high:Q", y=enc_y, color=color, tooltip=tip)
    dot = base.mark_point(filled=True, size=90, opacity=1).encode(
        x="value:Q", y=enc_y, color=color, tooltip=tip
    )
    return cast(alt.LayerChart, alt.layer(rule, dot))


DECISION_LABEL = {
    "PROMOTE": "Promoted",
    "REJECT": "Rejected",
    "INSUFFICIENT_EVIDENCE": "Insufficient evidence",
}
ROLLOUT_LABEL = {"COMPLETE": "Complete", "ROLLED_BACK": "Rolled back"}


# ---------------------------------------------------------------------------- 1. ML overview


def _pct(v: Any, nd: int = 2) -> str:
    return "n/a" if v is None or (isinstance(v, float) and pd.isna(v)) else f"{100 * v:.{nd}f}%"


def _release_path(
    ctx: MLContext,
    name: str,
    versions: pd.DataFrame,
    gates: pd.DataFrame,
    summ: pd.DataFrame,
    serving: pd.DataFrame,
) -> None:
    """Stage cards: production -> candidate (shadow) -> gate -> ring rollout -> backtest."""
    v = versions[versions["name"] == name]
    alias = {
        a: str(r)
        for r, als in zip(v["version"], v["aliases"], strict=True)
        for a in str(als).split(", ")
        if a
    }
    prod_v, cand_v = alias.get(ctx.fallback_alias), alias.get("candidate")
    gate = m.latest_gate(gates, name)
    bundle: dict[str, Any] = {}
    if gate is not None:
        row = ctx.src.query("gate_bundle", m.GATE_BUNDLE_SQL, {"decision_id": gate["decision_id"]})
        if not row.empty:
            bundle = json.loads(str(row["bundle_json"].iloc[0]))
    rates = m.bundle_rates(bundle) if bundle else {"production": {}, "candidate": {}}
    rings = serving[serving["model"] == name] if len(serving) else serving

    def info(ver: str | None, col: str) -> str:
        if ver is None:
            return "n/a"
        r = v[v["version"].astype(str) == ver]
        return str(r[col].iloc[0]) if len(r) and str(r[col].iloc[0]) else "n/a"

    def nb(model: str) -> str:
        n, files = rates[model].get("never_break"), rates[model].get("never_break_files")
        return "n/a" if n is None else f"{int(n)}/{int(files or 0)}"

    checks = m.gate_checks(bundle) if bundle else pd.DataFrame(columns=["check", "status"])
    status_color = {
        "pass": th.STATUS["good"],
        "fail": th.STATUS["critical"],
        "insufficient": th.STATUS["warning"],
    }
    m_rollouts = (
        summ[summ["candidate_version"].astype(str).str.startswith(f"{name}/")]
        if len(summ)
        else summ
    )
    diffs = ctx.results.table("backtest_pass2_differences")
    real = 0 if diffs is None else int(((diffs["ci_low"] > 0) | (diffs["ci_high"] < 0)).sum())
    stages = [
        ui.Stage(
            "Production",
            th.SERIES[0],
            f"v{prod_v or '?'}",
            "The model every ring serves unless a rollout is running.",
            [
                ui.Chip("LightGBM"),
                ui.Chip("isotonic calibrated"),
                ui.Chip(f"cut-off @ {100 * ctx.operating_fpr:g}% FPR"),
            ],
            "Serves",
            [
                ui.Chip(str(r.ring), th.RING_COLORS.get(str(r.ring), th.MUTED))
                for r in rings.itertuples()
                if str(r.version).endswith(f"/{prod_v}")
            ],
            [
                ("Recall", _pct(rates["production"].get("recall"))),
                ("FPR shipped", _pct(rates["production"].get("fpr"), 3)),
                ("Never-break", nb("production")),
            ],
            f"{name}/{prod_v} · fit {info(prod_v, 'fit_weeks')}",
            active=True,
        ),
        ui.Stage(
            "Candidate",
            th.SERIES[1],
            f"v{cand_v or '?'}",
            "The latest retrain, scored in shadow on the same traffic; no verdict counts.",
            [ui.Chip(info(cand_v, "policy")), ui.Chip(f"as of week {info(cand_v, 'as_of_week')}")],
            "Shadow coverage",
            [
                ui.Chip(
                    "paired with production",
                    th.SERIES[2],
                    _pct(rates["candidate"].get("coverage"), 1),
                )
            ],
            [
                ("Recall", _pct(rates["candidate"].get("recall"))),
                ("FPR shipped", _pct(rates["candidate"].get("fpr"), 3)),
                ("Never-break", nb("candidate")),
            ],
            f"{name}/{cand_v} · fit {info(cand_v, 'fit_weeks')}",
        ),
        ui.Stage(
            "Release gate",
            th.SERIES[2],
            str(bundle.get("policy", {}).get("policy_version", "-")),
            "Pre-registered checks on the shadow weeks; any hard failure blocks the release.",
            [ui.Chip("McNemar"), ui.Chip("Wilson intervals"), ui.Chip("never-break")],
            "Checks",
            [
                ui.Chip(str(r.check), status_color.get(str(r.status), th.MUTED), str(r.status))
                for r in checks.itertuples()
            ],
            [
                ("Decision", str(gate["decision"]) if gate else "none"),
                ("Failed", str(int((checks["status"] == "fail").sum())) if len(checks) else "-"),
                ("Weeks", str(len(bundle.get("evaluation", {}).get("weeks", [])) or "-")),
            ],
            str(gate["decision_id"]) if gate else "",
        ),
        ui.Stage(
            "Ring rollout",
            th.SERIES[3],
            "5% → 25% → all",
            "Moves ring aliases one ring at a time; any breached signal rolls back at once.",
            [ui.Chip("canaries"), ui.Chip("paired scoring"), ui.Chip("kill switch")],
            "Signals",
            [
                ui.Chip(s, c)
                for s, c in (
                    ("never-break", th.SERIES[4]),
                    ("flag rate", th.SERIES[1]),
                    ("errors", th.SERIES[7]),
                    ("p99", th.SERIES[3]),
                    ("disagreement", th.SERIES[6]),
                )
            ],
            [
                ("Rollouts", str(len(m_rollouts))),
                (
                    "Complete",
                    str(int((m_rollouts["final_state"] == "COMPLETE").sum()))
                    if len(m_rollouts)
                    else "0",
                ),
                (
                    "Rolled back",
                    str(int(m_rollouts["rolled_back"].sum())) if len(m_rollouts) else "0",
                ),
            ],
            "rollout_transitions (append-only)",
        ),
        ui.Stage(
            "Backtest verdict",
            th.SERIES[6],
            "Stage 7b",
            "What the point-in-time backtest says the release machinery is worth.",
            [ui.Chip("labels 6 weeks late"), ui.Chip("family bootstrap")],
            "Recommends",
            [
                ui.Chip("recency_weighted", th.SERIES[5]),
                ui.Chip("weekly recalibration", th.SERIES[2]),
            ],
            [
                ("Real differences", f"{real}/{0 if diffs is None else len(diffs)}"),
                (
                    "On recall",
                    "none"
                    if diffs is not None
                    and real
                    and not (
                        (diffs["metric"] == "recall@0.1%FPR")
                        & ((diffs["ci_low"] > 0) | (diffs["ci_high"] < 0))
                    ).any()
                    else "-",
                ),
                ("M (noise)", "2.76 pts"),
            ],
            "docs/BACKTEST.md",
        ),
    ]
    ui.stage_grid(stages, per_row=3)


def ml_overview(ctx: MLContext) -> None:
    st.title("ML overview")
    st.caption(
        "Which model serves each ring, what the gate and the rollout last decided, and "
        "how often the models flag files."
    )
    versions = ctx.versions()
    gates = ctx.gates()
    tr = ctx.transitions()
    serving = m.ring_serving(versions, ctx.ring_aliases, ctx.fallback_alias)

    last_gate = gates.iloc[0] if len(gates) else None
    summ = m.rollout_summary(tr)
    last_ro = summ.iloc[0] if len(summ) else None
    # a 7-day hourly aggregate needs no 10-second freshness: align the window to the minute
    # and keep it for 60 s (one ~1 s scan of the week per minute, not per page view)
    end = ctx.ops.now.replace(second=0, microsecond=0)
    fr = m.hourly_flag_rate(
        _flag_hourly(
            ctx.src,
            (end - timedelta(days=ctx.cfg.flag_rate_days)).isoformat(),
            end.isoformat(),
            f"{ctx.ops.canary_prefix}%",
            tuple(sorted(ctx.filters.params().items())),
        )
    )
    # ---- KPI row (icon + label, big value, caption, accent line)
    c1, c2, c3, c4 = st.columns(4)
    n_models = versions["name"].nunique() if len(versions) else 0
    n_rej = int((gates["decision"] == "REJECT").sum()) if len(gates) else 0
    ui.kpi(
        c1,
        "Registered versions",
        f"{len(versions)}",
        f"across {n_models} models",
        icon=":material/inventory_2:",
        accent="blue",
    )
    ui.kpi(
        c2,
        "Gate decisions",
        f"{len(gates)}",
        f"{n_rej} rejected",
        icon=":material/gavel:",
        accent="aqua",
    )
    n_rb = int(summ["rolled_back"].sum()) if len(summ) else 0
    ui.kpi(
        c3,
        "Rollouts",
        f"{len(summ)}",
        f"{n_rb} rolled back automatically",
        icon=":material/timeline:",
        accent="yellow",
    )
    tot = fr["events"].sum() if len(fr) else 0
    rate = fr["model_flags"].sum() / tot if tot else None
    ui.kpi(
        c4,
        f"Flag rate, {ctx.cfg.flag_rate_days} days",
        "n/a" if rate is None else f"{100 * rate:.2f}%",
        f"of {int(tot):,} files scored",
        icon=":material/flag:",
        accent="orange",
        trend=list(fr.groupby("hour")["model_flags"].sum() / fr.groupby("hour")["events"].sum())
        if len(fr)
        else None,
    )
    _filters_note(ctx)

    # ---- the release path as stage cards (one model at a time)
    names = sorted(versions["name"].unique()) if len(versions) else []
    with ui.card(
        "Release path",
        "Every stage a retrained model passes before it reaches all "
        "traffic, with the evidence each stage last produced",
    ):
        if not names:
            _empty("registered models")
        else:
            busiest = str(versions["name"].value_counts().idxmax())
            pick = st.segmented_control("Model", names, default=busiest, key="path-model")
            _release_path(ctx, str(pick or busiest), versions, gates, summ, serving)

    left, right = st.columns([3, 2], gap="medium")
    with left, st.container(border=True):
        st.subheader("Production model per ring")
        if serving.empty:
            _empty("registered models")
        else:
            piv = serving.pivot(index="model", columns="ring", values="version").reset_index()
            st.dataframe(piv, hide_index=True, width="stretch")
            st.caption(
                "From the registry aliases (ring0_model / ring1_model / ring2_model; a ring "
                "without its alias serves `production`)."
            )
    with right, st.container(border=True):
        st.subheader("Latest decisions")
        if last_gate is not None:
            ui.chips(
                [
                    ui.Chip(
                        f"gate {DECISION_LABEL.get(str(last_gate['decision']), '')}",
                        th.STATUS[DECISION_STATUS.get(str(last_gate["decision"]), "warning")],
                    )
                ]
            )
            st.markdown(
                f"**{last_gate['decision']}**: {last_gate['candidate_version']} vs "
                f"{last_gate['production_version']}"
            )
        if last_ro is not None:
            state = (
                "critical"
                if last_ro["rolled_back"]
                else ("good" if last_ro["final_state"] == "COMPLETE" else "warning")
            )
            ui.chips(
                [
                    ui.Chip(
                        f"rollout {ROLLOUT_LABEL.get(str(last_ro['final_state']), 'running')}",
                        th.STATUS[state],
                    )
                ]
            )
            st.markdown(
                f"**{last_ro['final_state']}**: {last_ro['candidate_version']} · "
                f"{str(last_ro['reason'])[:120]}"
            )
        if last_gate is None and last_ro is None:
            _empty("gate decisions or rollouts")

    st.subheader(f"Flag rate per ring, last {ctx.cfg.flag_rate_days} days")
    if fr.empty:
        _empty("scored files")
    else:
        dom = {r: c for r, c in th.RING_COLORS.items() if r in set(fr["ring"])}
        gapped = d.break_gaps(fr, "hour", ["ring"], "flag_rate", 3600, zero=False)
        _show(
            _timeseries(
                gapped,
                "flag_rate",
                "flag rate",
                color="ring",
                x="hour",
                y_format=".1%",
                domain=dom,
                points=True,
            ),
            "Hourly share of files flagged by the model in each ring; a jump in one ring "
            "after a rollout step is the first thing to look at.",
            fr,
            "ml-flag",
        )


# ---------------------------------------------------------------------------- 2. registry


def model_registry(ctx: MLContext) -> None:
    st.title("Model registry")
    st.caption(
        "Every registered version with its training window, retrain policy, data "
        "fingerprint and latest gate outcome (MLflow, read-only)."
    )
    versions = ctx.versions()
    if versions.empty:
        _empty("registered model versions")
        return
    t = m.registry_table(versions, ctx.gates(), ctx.cfg.mlflow_ui_url)
    names = sorted(t["name"].unique())
    busiest = str(t["name"].value_counts().idxmax())  # default: the model with most versions
    pick = st.segmented_control("Model", names, default=busiest, key="reg-model")
    t = t[t["name"] == (pick or busiest)]
    counts = t["gate_outcome"].value_counts()
    cols = st.columns(4)
    ui.kpi(
        cols[0],
        "Versions",
        f"{len(t)}",
        f"registered as {pick or busiest}",
        icon=":material/inventory_2:",
        accent="blue",
    )
    ui.kpi(
        cols[1],
        "Promoted by the gate",
        f"{int(counts.get('PROMOTE', 0))}",
        "PROMOTE decisions",
        icon=":material/check_circle:",
        accent="green",
    )
    ui.kpi(
        cols[2],
        "Rejected by the gate",
        f"{int(counts.get('REJECT', 0))}",
        "REJECT decisions",
        icon=":material/block:",
        accent="red",
    )
    ui.kpi(
        cols[3],
        "Never gated",
        f"{int(counts.get('not gated', 0))}",
        "baseline or demo versions",
        icon=":material/remove_circle:",
        accent="violet",
    )

    alias_color = {
        ctx.fallback_alias: th.STATUS["good"],
        "candidate": th.SERIES[1],
        **{a: th.RING_COLORS.get(r, th.MUTED) for r, a in ctx.ring_aliases.items()},
    }
    gate_color = {
        "PROMOTE": th.STATUS["good"],
        "REJECT": th.STATUS["critical"],
        "INSUFFICIENT_EVIDENCE": th.STATUS["warning"],
    }
    cards = []
    for r in t.sort_values("version", key=lambda s: s.astype(int), ascending=False).to_dict(
        "records"
    ):
        aliases = [a for a in str(r["aliases"]).split(", ") if a]
        kind = str(r["kind"] or ("demo" if r["demo"] else "version"))
        cards.append(
            ui.Stage(
                f"v{r['version']}",
                alias_color.get(aliases[0], th.MUTED) if aliases else th.MUTED,
                kind,
                "Rollout demo built from production's own model (Stage 6); the only kind "
                "allowed to skip the gate."
                if r["demo"]
                else f"Retrain policy {r['policy'] or 'baseline'} · as of week "
                f"{r['as_of_week'] or 'n/a'}",
                [ui.Chip(a, alias_color.get(a, th.MUTED)) for a in aliases]
                or [ui.Chip("no alias")],
                "Gate",
                [ui.Chip(str(r["gate_outcome"]), gate_color.get(str(r["gate_outcome"])))],
                [
                    ("Fit weeks", str(r["fit_weeks"] or "-")),
                    ("Calibration", str(r["calibration_weeks"] or "-")),
                    ("Data", str(r["fingerprint"] or "-")[:8]),
                ],
                f"{r['name']}/{r['version']} · run {str(r['run_id'])[:12]}",
                active=ctx.fallback_alias in aliases,
            )
        )
    with ui.card("Versions", "Newest first; the outlined card is the one serving production"):
        ui.stage_grid(cards, per_row=3)
    show = t[
        [
            "version",
            "aliases",
            "kind",
            "policy",
            "as_of_week",
            "fit_weeks",
            "calibration_weeks",
            "fingerprint",
            "gate_outcome",
            "demo",
            "created",
            "mlflow",
        ]
    ]
    with st.expander("Table view (with MLflow links)", icon=":material/table:"):
        st.dataframe(
            show.sort_values("version", key=lambda s: s.astype(int), ascending=False),
            hide_index=True,
            width="stretch",
            column_config={
                "mlflow": st.column_config.LinkColumn("MLflow", display_text="open"),
                "created": st.column_config.DatetimeColumn(
                    "created (UTC)", format="YYYY-MM-DD HH:mm"
                ),
                "fingerprint": st.column_config.TextColumn("data fingerprint", help="first 12 hex"),
            },
        )
    st.caption(
        "Aliases are what serving trusts: `production` and the ring aliases. Demo "
        "versions (demo = true) are the only ones allowed to skip the gate."
    )


# ---------------------------------------------------------------------------- 3. gate viewer


def gate_viewer(ctx: MLContext) -> None:
    st.title("Gate decision viewer")
    st.caption(
        "Every release-gate decision with its evidence: each check's numbers, interval "
        "bars, McNemar tables and the files the two models disagree on."
    )
    gates = ctx.gates()
    if gates.empty:
        _empty("gate decisions")
        return
    left, right = st.columns(2, gap="medium")
    with left, ui.card("Decisions", "Every gate decision on record, by outcome"):
        dc = m.decision_counts(gates)
        ui.donut(
            dc,
            "decision",
            "n",
            {
                "PROMOTE": th.STATUS["good"],
                "REJECT": th.STATUS["critical"],
                "INSUFFICIENT_EVIDENCE": th.STATUS["warning"],
            },
            f"{len(gates)}",
            "decisions",
            height=200,
            key="gate-mix",
        )
    with (
        right,
        ui.card(
            "What blocks releases",
            "How often each check is named in a rejection (a decision can name several)",
        ),
    ):
        bc = m.blocking_checks(gates)
        if bc.empty:
            st.success("No decision has been blocked.", icon=":material/check_circle:")
        else:
            worst = max(1, int(bc["decisions"].max()))
            ui.progress_rows(
                [
                    ui.Progress(
                        str(r["check"]),
                        int(r["decisions"]) / worst,
                        th.SERIES[i % len(th.SERIES)],
                        f"{int(r['decisions'])} of {len(gates)}",
                    )
                    for i, r in enumerate(bc.to_dict("records"))
                ]
            )

    ids = list(gates["decision_id"])
    pick = st.selectbox(
        "Decision",
        ids,
        format_func=lambda i: f"{i} · {gates.set_index('decision_id').loc[i, 'decision']}",
        key="gate-pick",
    )
    row = ctx.src.query("gate_bundle", m.GATE_BUNDLE_SQL, {"decision_id": pick})
    if row.empty:
        _empty("bundle for this decision")
        return
    sha, text = str(row["bundle_sha256"].iloc[0]), str(row["bundle_json"].iloc[0])
    bundle = json.loads(text)
    decision = str(bundle.get("decision", ""))
    state = DECISION_STATUS.get(decision, "warning")
    with st.container(border=True):
        b1, b2 = st.columns([1, 5], vertical_alignment="center")
        b1.badge(
            DECISION_LABEL.get(decision, decision),
            icon=th.STATUS_ICON[state],
            color=th.BADGE_COLOR[state],
        )
        b2.markdown(str(bundle.get("reason", "")))
    c1, c2 = st.columns([3, 1], vertical_alignment="center")
    mods = bundle.get("models", {})
    weeks = [
        w.get("week", w) if isinstance(w, dict) else w
        for w in bundle.get("evaluation", {}).get("weeks", [])
    ]
    c1.caption(
        f"Candidate {mods.get('candidate', {}).get('label')} vs production "
        f"{mods.get('production', {}).get('label')} · policy "
        f"{bundle.get('policy', {}).get('policy_version')} · weeks {weeks}"
    )
    if c2.button("Verify bundle hash", icon=":material/verified:", key="gate-verify"):
        if m.verify_bundle(sha, text):
            st.success(f"SHA-256 matches the stored hash ({sha[:16]}…).", icon=":material/lock:")
        else:
            st.error("Hash mismatch: the stored bundle was changed.", icon=":material/gpp_bad:")

    st.subheader("Checks")
    checks = m.gate_checks(bundle)
    for r in checks.itertuples():
        st.markdown(
            f"{CHECK_ICON.get(str(r.status), '')} **{r.check}** "
            f"({'hard' if r.hard else 'soft'}): {r.summary}"
        )

    bars = m.interval_bars(bundle)
    if not bars.empty:
        st.subheader("Recall and false alarms with 95% intervals")
        dom = {"production": MODEL_COLORS[0], "candidate": MODEL_COLORS[1]}
        cols = st.columns(2)
        for col, (measure, g) in zip(cols, bars.groupby("measure", sort=False), strict=False):
            with col:
                st.markdown(f"**{measure}**")
                chart = _interval_chart(g, "model", dom, ".2%", ".3%")
                st.altair_chart(chart.properties(height=120), theme=None, width="stretch")
        st.caption(
            "Dot = measured value, line = 95% Wilson interval. Recall is compared at the "
            "same false-alarm rate; FPR is what users see at each model's shipped cut-off."
        )
        with st.expander("data", icon=":material/table:"):
            st.dataframe(bars, hide_index=True, width="stretch")

    wk = m.weekly_regression(bundle)
    if not wk.empty:
        st.subheader("Recall per week")
        dom = {"production": MODEL_COLORS[0], "candidate": MODEL_COLORS[1]}
        _show(
            _timeseries(
                wk,
                "recall",
                "recall",
                color="model",
                x="week",
                y_format=".1%",
                domain=dom,
                x_type="O",
                y_zero=False,
            ),
            "Recall at the operating FPR per evaluated week; check (d) fails a week where the "
            "candidate drops more than the allowed amount.",
            wk,
            "gate-weekly",
            220,
        )

    tables = m.mcnemar_tables(bundle)
    if tables:
        st.subheader("McNemar 2x2 tables")
        cols = st.columns(len(tables))
        for col, (cls, tbl) in zip(cols, tables.items(), strict=True):
            col.markdown(
                f"**{cls} files** · McNemar p = {tbl.attrs.get('mcnemar_p', float('nan')):.3g}"
            )
            col.dataframe(tbl, width="stretch")
        st.caption(
            "Rows: production right / wrong; columns: candidate right / wrong. Only the "
            "off-diagonal cells (one model right, the other wrong) carry evidence."
        )

    flips = m.flipped_files(bundle, ctx.cfg.flipped_files_shown)
    st.subheader("Files the models disagree on")
    if flips.empty:
        _empty("flipped files")
    else:
        st.dataframe(
            flips,
            hide_index=True,
            width="stretch",
            column_config={
                "production_score": st.column_config.NumberColumn(format="%.8f"),
                "candidate_score": st.column_config.NumberColumn(format="%.8f"),
            },
        )
        st.caption(f"First {len(flips)} files whose shipped verdict differs, with both raw scores.")


# ---------------------------------------------------------------------------- 4. head-to-head


@st.cache_data(ttl=600, show_spinner="Loading scores…")
def _scores(_results: Any, key: str) -> pd.DataFrame | None:
    out: pd.DataFrame | None = _results.table("ml_scores")
    return out


def head_to_head_page(ctx: MLContext) -> None:
    st.title("Head-to-head")
    st.caption(
        "Any two models on the same files: ROC and PR curves (zoomed to the low-FPR "
        "region), recall and FPR per week, calibration."
    )
    scores = _scores(ctx.results, str(getattr(ctx.results, "path", "x")))
    doc = ctx.results.document("ml_models")
    if scores is None or doc is None:
        _empty("model scores (run `make ml-results`)")
        return
    models = doc["models"]
    ids = [i for i in m.model_ids(scores) if i in models]
    c1, c2 = st.columns(2)
    a = c1.selectbox("Model A", ids, index=ids.index("v1") if "v1" in ids else 0, key="h2h-a")
    b = c2.selectbox("Model B", ids, index=min(1, len(ids) - 1), key="h2h-b")
    st.caption(
        "Models: v1 (production) and the 18 point-in-time candidates of the Stage 7b "
        "backtest. Files: only weeks after both models' as-of week (neither learned "
        "from them)."
    )
    h = m.head_to_head(
        scores, models, a, b, ctx.operating_fpr, ctx.cfg.roc_max_fpr, ctx.cfg.calibration_bins
    )
    if h.summary.empty:
        _empty("weeks after both models' training data")
        return
    cols = st.columns(len(h.summary))
    for i, (col, r) in enumerate(zip(cols, h.summary.to_dict("records"), strict=True)):
        ui.kpi(
            col,
            f"{r['model']}: recall @ {100 * ctx.operating_fpr:.1f}% FPR",
            f"{100 * r['recall_at_fpr']:.2f}%",
            f"95% CI {100 * r['recall_low']:.2f}-{100 * r['recall_high']:.2f}% · AUC "
            f"{r['auc']:.4f} · {r['files']:,} files, weeks {h.weeks[0]}-{h.weeks[-1]}",
            icon=":material/model_training:",
            accent=("blue", "orange")[i % 2],
        )
    dom = dict(zip(dict.fromkeys((a, b)), MODEL_COLORS, strict=False))
    comp = h.summary.melt(
        id_vars="model",
        value_vars=["recall_at_fpr", "shipped_recall", "auc"],
        var_name="measure",
        value_name="value",
    )
    comp["measure"] = comp["measure"].map(
        {
            "recall_at_fpr": f"recall @ {100 * ctx.operating_fpr:.1f}% FPR",
            "shipped_recall": "recall at shipped cut-off",
            "auc": "AUC",
        }
    )
    with ui.card("Model comparison", "Both models on the same files; bars start at zero"):
        st.altair_chart(
            ui.grouped_bars(comp, "measure", "model", "value", dom, y_title="share", height=220),
            theme=None,
            width="stretch",
        )
    color = alt.Color(
        "model:N", scale=alt.Scale(domain=list(dom), range=list(dom.values())), title=None
    )
    l1, l2 = st.columns(2)
    with l1:
        roc = (
            alt.Chart(h.roc)
            .mark_line(strokeWidth=2, interpolate="step-after")
            .encode(
                x=alt.X(
                    "fpr:Q",
                    title="false-positive rate",
                    axis=alt.Axis(format=".2%"),
                    scale=alt.Scale(domain=[0, ctx.cfg.roc_max_fpr]),
                ),
                y=alt.Y("recall:Q", title="recall", axis=alt.Axis(format=".0%")),
                color=color,
                tooltip=[
                    alt.Tooltip("model:N"),
                    alt.Tooltip("fpr:Q", format=".3%"),
                    alt.Tooltip("recall:Q", format=".2%"),
                ],
            )
        )
        op = (
            alt.Chart(pd.DataFrame({"x": [ctx.operating_fpr]}))
            .mark_rule(color=th.MUTED, strokeDash=[4, 4])
            .encode(x="x:Q")
        )
        _show(
            roc + op,
            f"ROC zoomed to 0-{100 * ctx.cfg.roc_max_fpr:.0f}% FPR; dashed line = the "
            f"{100 * ctx.operating_fpr:.1f}% operating point. Higher is better.",
            h.roc,
            "h2h-roc",
            300,
        )
    with l2:
        pr = (
            alt.Chart(h.pr)
            .mark_line(strokeWidth=2)
            .encode(
                x=alt.X("recall:Q", title="recall", axis=alt.Axis(format=".0%")),
                y=alt.Y(
                    "precision:Q",
                    title="precision",
                    axis=alt.Axis(format=".1%"),
                    scale=alt.Scale(zero=False),
                ),
                color=color,
                tooltip=[
                    alt.Tooltip("model:N"),
                    alt.Tooltip("recall:Q", format=".2%"),
                    alt.Tooltip("precision:Q", format=".3%"),
                ],
            )
        )
        _show(
            pr,
            "Precision-recall on the same files (half the stream is malware, so precision "
            "stays high until recall approaches 100%).",
            h.pr,
            "h2h-pr",
            300,
        )
    st.subheader("Per week")
    w1, w2 = st.columns(2)
    with w1:
        _show(
            _timeseries(
                h.weekly,
                "recall_at_fpr",
                "recall",
                color="model",
                x="week",
                y_format=".1%",
                domain=dom,
                x_type="O",
                y_zero=False,
            ),
            f"Recall at {100 * ctx.operating_fpr:.1f}% FPR each week.",
            h.weekly,
            "h2h-wr",
            220,
        )
    with w2:
        _show(
            _timeseries(
                h.weekly,
                "shipped_fpr",
                "FPR",
                color="model",
                x="week",
                y_format=".2%",
                domain=dom,
                limit=ctx.operating_fpr,
                x_type="O",
            ),
            "False-positive rate at each model's shipped cut-off; the line is the target.",
            h.weekly,
            "h2h-wf",
            220,
        )
    st.subheader("Calibration")
    diag = (
        alt.Chart(pd.DataFrame({"x": [0, 1], "y": [0, 1]}))
        .mark_line(color=th.MUTED, strokeDash=[4, 4])
        .encode(x="x:Q", y="y:Q")
    )
    cal = (
        alt.Chart(h.calibration)
        .mark_line(point=True, strokeWidth=2)
        .encode(
            x=alt.X(
                "predicted:Q", title="mean predicted probability", scale=alt.Scale(domain=[0, 1])
            ),
            y=alt.Y("observed:Q", title="share malicious", scale=alt.Scale(domain=[0, 1])),
            color=color,
            tooltip=[
                "model:N",
                alt.Tooltip("predicted:Q", format=".3f"),
                alt.Tooltip("observed:Q", format=".3f"),
                alt.Tooltip("files:Q", format=","),
            ],
        )
    )
    _show(
        diag + cal,
        "Reliability: points on the dashed diagonal mean the calibrated "
        "probability matches the observed share of malware.",
        h.calibration,
        "h2h-cal",
        280,
    )


# ---------------------------------------------------------------------------- 5. drift


def drift_page(ctx: MLContext) -> None:
    st.title("Drift")
    st.caption(
        "How far this week's files have moved from what v1 learned on, feature by "
        "feature and in the model's own scores. Drift never blocks a release."
    )
    psi = ctx.results.table("ml_psi")
    if psi is None or psi.empty:
        _empty("feature PSI (run `make ml-results`)")
        return
    alerts = m.drift_alert_weeks(ctx.results.table("backtest_pass2_drift"))
    ref = ctx.cfg.psi_reference_weeks
    heat = (
        alt.Chart(psi)
        .mark_rect()
        .encode(
            x=alt.X("week:O", title="week"),
            y=alt.Y(
                "feature:N",
                title=None,
                sort=alt.EncodingSortField("psi", op="max", order="descending"),
            ),
            color=alt.Color("psi:Q", title="PSI", scale=alt.Scale(range=th.BLUE_RAMP, domainMin=0)),
            tooltip=["feature:N", "week:O", alt.Tooltip("psi:Q", format=".3f")],
        )
    )
    layers: Any = heat
    if alerts:
        marks = (
            alt.Chart(pd.DataFrame({"week": alerts}))
            .mark_point(
                shape="triangle-down", filled=True, size=120, color=th.STATUS["warning"], yOffset=-8
            )
            .encode(x="week:O", tooltip=[alt.Tooltip("week:O", title="drift alert week")])
        )
        layers = alt.layer(heat, marks)
    _show(
        layers,
        f"PSI of v1's top-20 features each week against weeks {ref.first}-{ref.last} (v1's "
        "last training weeks). Darker = larger shift; above 0.25 is a large shift. Triangles "
        f"mark weeks the drift rule alerted ({', '.join(map(str, alerts)) or 'none'}).",
        psi.pivot(index="feature", columns="week", values="psi").rename(columns=str).reset_index(),
        "psi",
        420,
    )

    top = (
        psi[psi["week"] > ref.last]
        .groupby("feature", as_index=False)
        .agg(psi=("psi", "max"))
        .sort_values("psi", ascending=False)
        .head(8)
    )
    if not top.empty:
        with ui.card(
            "Most-shifted features",
            "Largest weekly PSI after the reference window; "
            "the bar is scaled to 0.5, the line between 0.25 and above is a large shift",
        ):
            ui.progress_rows(
                [
                    ui.Progress(
                        str(r["feature"]),
                        min(1.0, float(r["psi"]) / 0.5),
                        th.STATUS["serious"] if float(r["psi"]) > 0.25 else th.SERIES[0],
                        f"PSI {float(r['psi']):.2f}"
                        + (" · large" if float(r["psi"]) > 0.25 else ""),
                    )
                    for r in top.to_dict("records")
                ]
            )

    scores = _scores(ctx.results, str(getattr(ctx.results, "path", "x")))
    if scores is None:
        return
    st.subheader("Score distribution by week")
    ids = m.model_ids(scores)
    model = st.selectbox(
        "Model", ids, index=ids.index("v1") if "v1" in ids else 0, key="drift-model"
    )
    dist = m.score_distribution(scores, model, ctx.cfg.score_hist_bins)
    chart = (
        alt.Chart(dist)
        .mark_rect()
        .encode(
            x=alt.X("week:O", title="week"),
            y=alt.Y(
                "log_odds:O",
                title="score (log-odds)",
                sort="descending",
                axis=alt.Axis(format=".0f", labelOverlap=True),
            ),
            color=alt.Color(
                "share:Q",
                title="share of files",
                scale=alt.Scale(range=th.BLUE_RAMP),
                legend=alt.Legend(format=".0%"),
            ),
            tooltip=[
                "week:O",
                alt.Tooltip("log_odds:O", format=".1f"),
                alt.Tooltip("share:Q", format=".2%"),
            ],
        )
    )
    _show(
        chart,
        "Each column is one week's score histogram (log-odds of the raw score, fixed "
        "bins): a column that looks different from its neighbours is score drift.",
        dist,
        "score-dist",
        380,
    )


# ---------------------------------------------------------------------------- 6. rollouts


def rollout_timeline(ctx: MLContext) -> None:
    st.title("Rollout timeline")
    st.caption(
        "Each rollout ring by ring: when it entered each state, the flag rate in every "
        "ring meanwhile, and why it stopped."
    )
    tr = ctx.transitions()
    summ = m.rollout_summary(tr)
    if summ.empty:
        _empty("rollouts")
        return
    c = st.columns(3)
    ui.kpi(
        c[0],
        "Rollouts",
        f"{len(summ)}",
        "started by the controller",
        icon=":material/timeline:",
        accent="blue",
    )
    ui.kpi(
        c[1],
        "Completed",
        f"{int((summ['final_state'] == 'COMPLETE').sum())}",
        "reached every ring",
        icon=":material/check_circle:",
        accent="green",
    )
    ui.kpi(
        c[2],
        "Rolled back",
        f"{int(summ['rolled_back'].sum())}",
        "stopped by a signal or the kill switch",
        icon=":material/undo:",
        accent="red",
    )
    left, right = st.columns(2, gap="medium")
    with left, ui.card("Outcomes", "How every rollout ended"):
        ui.donut(
            m.outcome_counts(summ),
            "outcome",
            "rollouts",
            {
                "complete": th.STATUS["good"],
                "rolled back": th.STATUS["critical"],
                "in progress": th.STATUS["warning"],
            },
            f"{len(summ)}",
            "rollouts",
            height=200,
            key="ro-mix",
        )
    with right, ui.card("What stopped them", "The signal named in each rollback"):
        rr = m.rollback_reasons(summ)
        if rr.empty:
            st.success("No rollout was rolled back.", icon=":material/check_circle:")
        else:
            top = max(1, int(rr["rollouts"].max()))
            ui.progress_rows(
                [
                    ui.Progress(
                        str(r["signal"]).replace("_", " "),
                        int(r["rollouts"]) / top,
                        th.SERIES[i % len(th.SERIES)],
                        f"{int(r['rollouts'])} rollout{'s' if int(r['rollouts']) != 1 else ''}",
                    )
                    for i, r in enumerate(rr.to_dict("records"))
                ]
            )
    pick = st.selectbox(
        "Rollout",
        list(summ["rollout_id"]),
        key="ro-pick",
        format_func=lambda r: f"{r} · {summ.set_index('rollout_id').loc[r, 'final_state']}",
    )
    one = summ.set_index("rollout_id").loc[pick]
    spans = m.state_spans(tr, pick, ctx.ops.now)
    dom = {k: v for k, v in th.ROLLOUT_STATE_COLORS.items() if k in set(spans["state"])}
    bars = (
        alt.Chart(spans)
        .mark_bar(height=22)
        .encode(
            x=alt.X("start:T", title=None),
            x2="end:T",
            y=alt.Y("state:N", title=None, sort=list(th.ROLLOUT_STATE_COLORS)),
            color=alt.Color(
                "state:N", scale=alt.Scale(domain=list(dom), range=list(dom.values())), legend=None
            ),
            tooltip=[
                "state:N",
                alt.Tooltip("start:T", format="%H:%M:%S"),
                alt.Tooltip("end:T", format="%H:%M:%S"),
                "reason:N",
            ],
        )
    )
    _show(bars, "State per time; red = rolled back, green = complete.", spans, "ro-spans", 200)

    start = pd.Timestamp(one["started"]) - pd.Timedelta(minutes=2)
    end = pd.Timestamp(one["ended"]) + pd.Timedelta(minutes=2)
    fr = m.minute_flag_rate(
        ctx.src.query(
            "ring_flag",
            m.RING_FLAG_SQL,
            {
                "start": start.to_pydatetime(),
                "end": end.to_pydatetime(),
                "canary": f"{ctx.ops.canary_prefix}%",
                "tenant": ctx.filters.tenant,
            },
        )
    )
    st.subheader("Flag rate per ring during the rollout")
    if fr.empty:
        _empty("scored files during this rollout")
    else:
        rdom = {r: col for r, col in th.RING_COLORS.items() if r in set(fr["ring"])}
        gapped = d.break_gaps(fr, "minute", ["ring", "model_version"], "flag_rate", 60, zero=False)
        _show(
            _timeseries(
                gapped,
                "flag_rate",
                "flag rate",
                color="ring",
                x="minute",
                y_format=".1%",
                domain=rdom,
                dash="model_version",
            ),
            "Per-minute share of files flagged in each ring (dash = model version); the "
            "candidate's ring should look like the others.",
            fr,
            "ro-flag",
        )
    st.subheader("Rollbacks")
    rb = summ[summ["rolled_back"]][["rollout_id", "candidate_version", "ended", "reason"]]
    if rb.empty:
        st.success("No rollout was rolled back.", icon=":material/check_circle:")
    else:
        st.dataframe(rb, hide_index=True, width="stretch")
        st.caption("Every rollback with the controller's (or the operator's) reason.")


# ---------------------------------------------------------------------------- 7. experiment


def experiment_results(ctx: MLContext) -> None:
    st.title("Experiment results")
    st.caption(
        "Release policies compared on the replayed stream: Stage 7 main experiment and "
        "the Stage 7b point-in-time backtest (docs/RESULTS.md, docs/BACKTEST.md)."
    )
    tab7b, tab7 = st.tabs(["Stage 7b backtest (current)", "Stage 7 experiment"])
    with tab7:
        iv = m.experiment_intervals(ctx.results.table("experiment_summary"))
        if iv.empty:
            _empty("Stage 7 results")
        else:
            pol = sorted(iv["policy"].unique())
            dom = dict(zip(pol, th.SERIES, strict=False))
            cols = st.columns(3)
            for col, (measure, g) in zip(cols, iv.groupby("measure", sort=False), strict=False):
                with col:
                    st.markdown(f"**{measure}**")
                    chart = _interval_chart(g, "policy", dom, ",.0f", ",.1f", "count")
                    st.altair_chart(chart.properties(height=150), theme=None, width="stretch")
            st.caption(
                "Dot = real stream, line = range over bootstrap resamples; counts summed "
                "over the three retraining policies."
            )
            with st.expander("data", icon=":material/table:"):
                st.dataframe(iv, hide_index=True, width="stretch")
            rot = m.recall_over_time(ctx.results.table("experiment_weekly"))
            if not rot.empty:
                _show(
                    _timeseries(
                        rot,
                        "recall_at_fpr",
                        "recall",
                        color="policy",
                        x="week",
                        y_format=".1%",
                        domain=dom,
                        x_type="O",
                        y_zero=False,
                    ),
                    "Mean recall at 0.1% FPR of the models each policy kept live.",
                    rot,
                    "exp-rot",
                    240,
                )
            dp = m.drift_precision(ctx.results.table("experiment"))
            if dp:
                prec = f"{100 * dp.alerts_on_harmful / dp.alerts:.0f}%" if dp.alerts else "n/a"
                st.metric(
                    "Drift-alert precision (P1 candidates)",
                    prec,
                    help=f"{dp.alerts} alerts on {dp.candidates} candidates, "
                    f"{dp.alerts_on_harmful} on a harmful one; {dp.harmful} harmful "
                    "candidates in all.",
                    border=True,
                )
    with tab7b:
        s = ctx.results.table("backtest_pass2_summary")
        diffs = ctx.results.table("backtest_pass2_differences")
        if s is None:
            _empty("Stage 7b results")
        else:
            # headline policies only; the in-sample tuning and the early-label sensitivity run
            # are side results (labelled in the table below), not policies to compare
            main = s[
                ~s["release_policy"].isin(["P4_IN_SAMPLE"])
                & ~s["release_policy"].astype(str).str.contains("flip")
            ]
            pol_colors = dict(zip(m.POLICY_SHORT.values(), th.SERIES, strict=False))
            rec = main.assign(
                policy=main["release_policy"].map(lambda p: m.POLICY_SHORT.get(str(p), str(p))),
                recal=main["recalibration"].map(lambda r: "recalibration on" if r else "off"),
            )
            with ui.card(
                "Mean recall across release policies",
                "Recall at 0.1% FPR of the models each policy kept live, weeks 53-64; "
                "bars start at zero",
            ):
                st.altair_chart(
                    ui.grouped_bars(
                        rec,
                        "policy",
                        "recal",
                        "mean_recall",
                        {"off": th.SERIES[0], "recalibration on": th.SERIES[2]},
                        y_title="mean recall @ 0.1% FPR",
                    ),
                    theme=None,
                    width="stretch",
                )
                st.caption(
                    "Every policy sits within about a point of the others; the CIs "
                    "(table below) all overlap - no policy is better on recall."
                )
            comp = m.policy_comparison(main)
            comp = comp[comp["recalibration"] == "recalibration on"]
            with ui.card(
                "Promotions, harms and blocked improvements",
                "Counts over the 18 candidates (recalibration on)",
            ):
                st.altair_chart(
                    ui.grouped_bars(
                        comp,
                        "measure",
                        "policy",
                        "value",
                        pol_colors,
                        y_title="candidates",
                        y_format=",.0f",
                    ),
                    theme=None,
                    width="stretch",
                )
            tw = ctx.results.table("backtest_pass2_training_window")
            if tw is not None and not tw.empty:
                with ui.card(
                    "Training window",
                    "Recall at 0.1% FPR on the 4 weeks after each retrain, per retraining policy",
                ):
                    tw2 = tw.assign(T=tw["as_of_week"].map(lambda t: f"T={t}"))
                    st.altair_chart(
                        ui.grouped_bars(
                            tw2,
                            "T",
                            "retrain_policy",
                            "recall",
                            {
                                "all_history": th.SERIES[0],
                                "last_n_weeks": th.SERIES[1],
                                "recency_weighted": th.SERIES[2],
                            },
                            y_title="recall @ 0.1% FPR",
                        ),
                        theme=None,
                        width="stretch",
                    )
                    st.caption(
                        "recency_weighted beats last_n_weeks at four of six retrains "
                        "(CIs above 0 in docs/BACKTEST.md)."
                    )
            st.dataframe(s, hide_index=True, width="stretch")
            st.caption(
                "Point-in-time backtest (labels 6 weeks late). Harmful = loss above the "
                "measured noise M with a CI excluding 0."
            )
        if diffs is not None and not diffs.empty:
            st.markdown("**Which differences are real**")
            dd = diffs.copy()
            dd["real"] = np.where((dd["ci_low"] > 0) | (dd["ci_high"] < 0), "real", "within noise")
            for metric, g in dd.groupby("metric", sort=False):
                st.markdown(f"*{metric}* (percentage points)")
                rule = (
                    alt.Chart(g)
                    .mark_rule(strokeWidth=3)
                    .encode(
                        x=alt.X("ci_low:Q", title=None),
                        x2="ci_high:Q",
                        y=alt.Y("comparison:N", title=None, axis=alt.Axis(labelLimit=320)),
                        color=alt.Color(
                            "real:N",
                            scale=alt.Scale(
                                domain=["real", "within noise"], range=[th.SERIES[1], th.MUTED]
                            ),
                            title=None,
                        ),
                    )
                )
                dot = (
                    alt.Chart(g)
                    .mark_point(filled=True, size=70, color=th.INK)
                    .encode(
                        x="diff:Q",
                        y="comparison:N",
                        tooltip=[
                            "comparison:N",
                            alt.Tooltip("diff:Q", format="+.3f"),
                            alt.Tooltip("ci_low:Q", format="+.3f"),
                            alt.Tooltip("ci_high:Q", format="+.3f"),
                            "real:N",
                        ],
                    )
                )
                zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color=th.INK_2).encode(x="x:Q")
                st.altair_chart(
                    alt.layer(rule, dot, zero).properties(height=28 * len(g) + 40),
                    theme=None,
                    width="stretch",
                )
            st.caption(
                "Dot = difference, line = 95% family-clustered CI; orange = the CI "
                "excludes 0 (a real difference), grey = within noise."
            )


# ---------------------------------------------------------------------------- 8. lookup


def prediction_lookup(ctx: MLContext) -> None:
    st.title("Prediction lookup")
    st.caption(
        "Everything recorded about one file or request: the live verdict, the "
        "production reference score, shadow scores and the trace."
    )
    q = st.text_input("file_id or request_id", key="lookup-q", placeholder="sha256 or request id")
    if not q:
        st.info("Enter a file_id (sha256) or a request_id.", icon=":material/search:")
        return
    q = q.strip()
    live = ctx.src.query("lookup", m.LOOKUP_SQL, {"q": q, "limit": ctx.cfg.lookup_limit})
    shadow = ctx.src.query(
        "shadow_lookup", m.SHADOW_LOOKUP_SQL, {"q": q, "limit": ctx.cfg.lookup_limit}
    )
    if live.empty and shadow.empty:
        st.warning("No prediction with this file_id or request_id.", icon=":material/search_off:")
        return
    if not live.empty:
        st.subheader(f"Live verdicts ({len(live)})")
        top = live.iloc[0]
        cols = st.columns(4)
        cols[0].metric("Verdict", str(top["verdict"]), border=True)
        cols[1].metric(
            "Score / cut-off",
            f"{top['score']:.6f}",
            f"cut-off {top['threshold']:.6f}",
            delta_color="off",
            border=True,
        )
        cols[2].metric(
            "Model · ring",
            f"{top['model_version']}",
            str(top["ring"] or "-"),
            delta_color="off",
            border=True,
        )
        cols[3].metric("Override", "yes" if top["override_applied"] else "no", border=True)
        live = live.copy()
        live["trace"] = [
            f"{ctx.ops.mon.tracing.jaeger_ui_url.rstrip('/')}/trace/{t}" if t else None
            for t in live["trace_id"]
        ]
        st.dataframe(
            live,
            hide_index=True,
            width="stretch",
            column_config={
                "trace": st.column_config.LinkColumn("Jaeger", display_text="trace"),
                "score": st.column_config.NumberColumn(format="%.8f"),
                "reference_score": st.column_config.NumberColumn(format="%.8f"),
            },
        )
        st.caption(
            "reference_* = production's score of the same file while a ring served a "
            "candidate (paired scoring); trace opens the request in Jaeger."
        )
    if not shadow.empty:
        st.subheader(f"Shadow scores ({len(shadow)})")
        st.dataframe(shadow, hide_index=True, width="stretch")
        st.caption("Candidate scores recorded by the shadow consumer for the same file.")
