"""Operations section of the Streamlit app: Service Health, Model Live, Alerts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

import altair as alt
import pandas as pd
import streamlit as st

from sentinel.config import AlertRulesConfig, MonitoringConfig, ThresholdRule
from sentinel.dashboard import data as d


@dataclass(frozen=True)
class Context:
    src: d.Source
    window: d.Window
    mon: MonitoringConfig
    rules: AlertRulesConfig
    api_service: str  # ops_metrics.service of the scoring API
    canary_prefix: str  # rollout canary tenants (excluded from real-traffic rates)
    now: datetime


def _p99_limit(rules: AlertRulesConfig) -> float | None:
    for r in rules.rules:
        if isinstance(r, ThresholdRule) and r.metric == "latency_ms" and r.field == "p99":
            return r.above
    return None


def _line(
    df: pd.DataFrame,
    y: str,
    color: str,
    y_title: str,
    *,
    x: str = "ts_bucket",
    dash: str | None = None,
    markers: pd.DataFrame | None = None,
    y_format: str | None = None,
) -> alt.LayerChart | alt.Chart:
    y_enc = (
        alt.Y(f"{y}:Q", title=y_title, axis=alt.Axis(format=y_format))
        if y_format
        else alt.Y(f"{y}:Q", title=y_title)
    )
    enc: dict[str, object] = {
        "x": alt.X(f"{x}:T", title="time (UTC)"),
        "y": y_enc,
        "color": alt.Color(f"{color}:N"),
        "tooltip": [f"{x}:T", f"{color}:N", alt.Tooltip(f"{y}:Q", format=y_format or ",.3f")],
    }
    if dash:
        enc["strokeDash"] = alt.StrokeDash(f"{dash}:N")
    chart = cast(alt.Chart, alt.Chart(df).mark_line(point=len(df) < 60).encode(**enc))
    if markers is not None and not markers.empty:
        rules = (
            alt.Chart(markers)
            .mark_rule(strokeDash=[4, 3], color="gray")
            .encode(x="ts_bucket:T", tooltip=["ts_bucket:T", "model_version:N"])
        )
        return cast(alt.LayerChart, alt.layer(chart, rules))
    return chart


def _empty(what: str) -> None:
    st.info(f"No {what} in this time window yet.")


def service_health(ctx: Context) -> None:
    st.header("Service Health")
    w = ctx.window
    series = ctx.src.query(
        "ops_api",
        d.OPS_SERIES_SQL,
        {
            "service": ctx.api_service,
            "metrics": ["requests", "errors", "latency_ms", "model_reloads"],
            **w.params(),
        },
    )
    lag_rows = ctx.src.query(
        "ops_lag",
        d.OPS_SERIES_SQL,
        {"service": ctx.mon.consumer_lag.service, "metrics": ["consumer_lag"], **w.params()},
    )
    reloads = d.reload_events(series) if not series.empty else None

    st.subheader("Requests per second")
    rps = d.requests_per_second(series) if not series.empty else pd.DataFrame()
    if rps.empty:
        _empty("requests")
    else:
        st.altair_chart(
            _line(rps, "requests_per_second", "model_version", "req/s", markers=reloads),
            width="stretch",
        )
    st.caption("Answered requests per second by model version; dashed lines = model reloads.")

    st.subheader("Error rate (HTTP 5xx)")
    er = d.error_rate(series) if not series.empty else pd.DataFrame()
    if er.empty:
        _empty("requests")
    else:
        st.altair_chart(
            _line(er, "error_rate", "model_version", "5xx share", y_format=".2%"),
            width="stretch",
        )
    st.caption("Share of requests answered with a server error, per 10-second bucket.")

    st.subheader("Latency")
    lat = d.latency(series) if not series.empty else pd.DataFrame()
    if lat.empty:
        _empty("latency samples")
    else:
        st.altair_chart(
            _line(lat, "ms", "percentile", "ms", dash="model_version", markers=reloads),
            width="stretch",
        )
    limit = _p99_limit(ctx.rules)
    st.caption(
        "In-service handler time per 10-second bucket (p50/p95/p99) by model version"
        + (f"; the p99 alert fires above {limit:g} ms for 5 min." if limit is not None else ".")
    )
    if limit is not None and not lat.empty:
        spikes = d.latency_spikes(lat, limit)
        if not spikes.empty:
            secs = max(int(series["bucket_seconds"].max()), 1)
            spikes["jaeger"] = [
                d.jaeger_url(
                    ctx.mon.tracing.jaeger_ui_url,
                    ctx.mon.tracing.api_service_name,
                    pd.Timestamp(t).to_pydatetime(),
                    pd.Timestamp(t).to_pydatetime() + timedelta(seconds=secs),
                    ctx.mon.dashboard.spike_link_margin_seconds,
                )
                for t in spikes["ts_bucket"]
            ]
            st.markdown(f"**Latency spikes** (p99 above {limit:g} ms)")
            st.dataframe(
                spikes[["ts_bucket", "model_version", "ms", "jaeger"]],
                column_config={
                    "jaeger": st.column_config.LinkColumn("traces", display_text="open in Jaeger")
                },
                hide_index=True,
                width="stretch",
            )
            st.caption("Each link opens Jaeger filtered to the API service and that time window.")

    st.subheader("Consumer lag")
    lag = d.consumer_lag(lag_rows) if not lag_rows.empty else pd.DataFrame()
    if lag.empty:
        _empty("consumer lag readings")
    else:
        st.altair_chart(_line(lag, "lag", "group", "events behind"), width="stretch")
    st.caption("Events published but not yet committed, live and shadow consumer groups.")


def model_live(ctx: Context) -> None:
    st.header("Model Live")
    w = ctx.window
    canary = f"{ctx.canary_prefix}%"
    flags = ctx.src.query("flag_rate", d.FLAG_RATE_SQL, {"canary": canary, **w.params()})
    st.subheader("Flag rate by ring and model version")
    if flags.empty:
        _empty("scored events")
    else:
        fr = d.flag_rate(flags)
        st.altair_chart(
            _line(
                fr,
                "flag_rate",
                "ring",
                "flagged share",
                x="minute",
                dash="model_version",
                y_format=".1%",
            ),
            width="stretch",
        )
    st.caption(
        "Share of real events the model flags (score above its threshold, before "
        "tenant overrides), per minute."
    )

    st.subheader("Override rate per tenant")
    ov = ctx.src.query(
        "override_rate",
        d.OVERRIDE_RATE_SQL,
        {"canary": canary, "top": ctx.mon.dashboard.top_tenants, **w.params()},
    )
    if ov.empty:
        _empty("scored events")
    else:
        st.dataframe(
            d.override_rate(ov),
            column_config={"override_rate": st.column_config.NumberColumn(format="percent")},
            hide_index=True,
            width="stretch",
        )
    st.caption(
        f"Tenants with the most allow/block-list overrides (top {ctx.mon.dashboard.top_tenants})."
    )

    st.subheader("Never-break hits")
    nb_series = ctx.src.query(
        "never_break",
        d.OPS_SERIES_SQL,
        {
            "service": ctx.api_service,
            "metrics": ["never_break_hits", "canary_requests"],
            **w.params(),
        },
    )
    canaries = (
        nb_series[nb_series["metric"] == "canary_requests"] if not nb_series.empty else nb_series
    )
    nb = d.never_break_hits(nb_series, canaries) if not nb_series.empty else pd.DataFrame()
    total = int(nb["never_break_hits"].sum()) if not nb.empty else 0
    scored = int(nb["canaries_scored"].sum()) if not nb.empty else 0
    c1, c2 = st.columns(2)
    c1.metric("never-break hits", total)
    c2.metric("canaries scored", scored)
    if not nb.empty:
        st.altair_chart(
            _line(
                nb.melt("ts_bucket", var_name="series", value_name="files"),
                "files",
                "series",
                "files",
            ),
            width="stretch",
        )
    st.caption(
        "Never-break canaries flagged by the model in this window: a flat zero is expected; "
        "any hit pages at once."
    )

    st.subheader("Rollout state")
    ro = ctx.src.query("rollouts", d.ROLLOUT_SQL, w.params())
    if ro.empty:
        _empty("rollout transitions")
    else:
        order = ["APPROVED", "RING0", "RING1", "RING2", "COMPLETE", "ROLLED_BACK"]
        chart = (
            alt.Chart(ro)
            .mark_line(interpolate="step-after", point=True)
            .encode(
                x=alt.X("ts:T", title="time (UTC)"),
                y=alt.Y("to_state:N", sort=order, title="state"),
                color="file_type:N",
                tooltip=["ts:T", "file_type:N", "rollout_id:N", "to_state:N", "reason:N"],
            )
        )
        st.altair_chart(chart, width="stretch")
        st.dataframe(
            ro[["ts", "file_type", "rollout_id", "from_state", "to_state", "actor", "reason"]],
            hide_index=True,
            width="stretch",
        )
    st.caption("Ring rollout states over time (the last state before the window is included).")


def alerts_page(ctx: Context) -> None:
    st.header("Alerts")
    alerts = ctx.src.query("alerts", d.ALERTS_SQL, ctx.window.params())
    st.subheader("Firing now")
    now_df = d.firing_now(alerts)
    if now_df.empty:
        st.success("No alert is firing.")
    else:
        st.dataframe(
            now_df[["ts", "rule", "severity", "dedup_key", "message"]],
            hide_index=True,
            width="stretch",
        )
    st.caption("Alerts whose latest state is FIRING.")

    st.subheader("History")
    if alerts.empty:
        _empty("alerts")
    else:
        st.dataframe(d.alert_history(alerts, ctx.now), hide_index=True, width="stretch")
    st.caption("One row per firing episode with its duration (open episodes run to now).")

    st.subheader("Rules")
    st.dataframe(d.rule_table(ctx.rules), hide_index=True, width="stretch")
    st.caption(f"configs/alert_rules.yaml, evaluated every {ctx.rules.evaluate_every_seconds:g} s.")
