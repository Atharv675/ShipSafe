"""Operations section of the Streamlit app: Overview, Service Health, Model Live, Alerts.

Layout follows the usual operations-dashboard order: overall health first (banner + KPI
cards), then the golden signals (traffic, errors, latency, saturation), then per-component
detail, with drill-down links to Jaeger. Every chart has a one-line caption, a hover
tooltip and a table view ("data" expander). Colours come from dashboard/theme.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import altair as alt
import pandas as pd
import streamlit as st

from sentinel.config import AlertRulesConfig, MonitoringConfig, ThresholdRule
from sentinel.dashboard import data as d
from sentinel.dashboard import theme as th

SCORE_BINS = 20
OPS_METRICS = [
    "requests",
    "errors",
    "latency_ms",
    "model_reloads",
    "lineage_dropped",
    "ops_metrics_dropped",
    "metrics_samples_dropped",
]


@dataclass(frozen=True)
class Context:
    src: d.Source
    window: d.Window
    mon: MonitoringConfig
    rules: AlertRulesConfig
    api_service: str  # ops_metrics.service of the scoring API
    canary_prefix: str  # rollout canary tenants (excluded from real-traffic rates)
    now: datetime

    @property
    def bucket_seconds(self) -> float:
        return self.mon.metrics.flush_seconds


# ---------------------------------------------------------------------------- building blocks


def _p99_limit(rules: AlertRulesConfig) -> float | None:
    for r in rules.rules:
        if isinstance(r, ThresholdRule) and r.metric == "latency_ms" and r.field == "p99":
            return r.above
    return None


def _empty(what: str) -> None:
    st.info(f"No {what} in this time window yet.", icon=":material/hourglass_empty:")


def _show(chart: Any, caption: str, table: pd.DataFrame, key: str, height: int = 260) -> None:
    """Chart + one-line caption + table view (every value reachable without hover)."""
    st.altair_chart(chart.properties(height=height), theme=None, width="stretch")
    st.caption(caption)
    with st.expander("data", icon=":material/table:"):
        st.dataframe(table, hide_index=True, width="stretch", key=f"tbl-{key}")


def _timeseries(
    df: pd.DataFrame,
    y: str,
    y_title: str,
    *,
    color: str | None = None,
    x: str = "ts_bucket",
    dash: str | None = None,
    y_format: str = ",.2f",
    domain: dict[str, str] | None = None,
    limit: float | None = None,
    markers: pd.DataFrame | None = None,
    area: bool = False,
    log: bool = False,
) -> alt.LayerChart:
    """Line (or stacked area) over time with a hover crosshair and tooltip, an optional
    limit line (status colour, labelled) and optional event markers (model reloads).
    `log`: symmetric-log y scale, so one large spike does not flatten the normal range."""
    base = alt.Chart(df)
    enc: dict[str, Any] = {
        "x": alt.X(f"{x}:T", title=None),
        "y": alt.Y(
            f"{y}:Q",
            title=f"{y_title} (log scale)" if log else y_title,
            axis=alt.Axis(format=y_format),
            stack="zero" if area else None,
            scale=alt.Scale(type="symlog") if log else alt.Undefined,
        ),
    }
    tooltip: list[Any] = [alt.Tooltip(f"{x}:T", title="time (UTC)", format="%H:%M:%S")]
    if color:
        scale = (
            alt.Scale(domain=list(domain), range=list(domain.values())) if domain else alt.Undefined
        )
        enc["color"] = alt.Color(f"{color}:N", title=None, scale=scale)
        tooltip.append(alt.Tooltip(f"{color}:N"))
    if dash:
        enc["strokeDash"] = alt.StrokeDash(f"{dash}:N", title=None)
        tooltip.append(alt.Tooltip(f"{dash}:N"))
    tooltip.append(alt.Tooltip(f"{y}:Q", format=y_format, title=y_title))
    mark = base.mark_area(line={"strokeWidth": 2}) if area else base.mark_line()
    lines = mark.encode(**enc)
    hover = alt.selection_point(fields=[x], nearest=True, on="pointerover", empty=False)
    rule = (
        base.mark_rule(color=th.MUTED, strokeWidth=1)
        .encode(
            x=f"{x}:T", opacity=alt.condition(hover, alt.value(0.8), alt.value(0)), tooltip=tooltip
        )
        .add_params(hover)
    )
    layers: list[Any] = [lines, rule]
    if limit is not None:
        lim = pd.DataFrame({"limit": [limit], "label": [f"limit {limit:g}"]})
        layers.append(
            alt.Chart(lim).mark_rule(color=th.STATUS["critical"], strokeWidth=1).encode(y="limit:Q")
        )
        layers.append(
            alt.Chart(lim)
            .mark_text(align="left", dx=4, dy=-6, color=th.STATUS["critical"], fontSize=11)
            .encode(y="limit:Q", text="label:N", x=alt.value(0))
        )
    if markers is not None and not markers.empty:
        layers.append(
            alt.Chart(markers)
            .mark_rule(color=th.INK_2, strokeWidth=1, opacity=0.6)
            .encode(
                x="ts_bucket:T",
                tooltip=[alt.Tooltip("ts_bucket:T", title="model reload"), "model_version:N"],
            )
        )
    return cast(alt.LayerChart, alt.layer(*layers))


def _bars(
    df: pd.DataFrame,
    x: str,
    y: str,
    *,
    x_title: str,
    y_title: str | None = None,
    color: str | None = None,
    domain: dict[str, str] | None = None,
    x_format: str = ",.0f",
    sort: str = "-x",
) -> alt.Chart:
    """Horizontal bars (ranked), one colour per series; tooltips on every bar."""
    enc: dict[str, Any] = {
        "x": alt.X(f"{x}:Q", title=x_title, axis=alt.Axis(format=x_format)),
        "y": alt.Y(f"{y}:N", title=y_title, sort=sort),
        "tooltip": [alt.Tooltip(f"{y}:N"), alt.Tooltip(f"{x}:Q", format=x_format)],
    }
    if color:
        scale = (
            alt.Scale(domain=list(domain), range=list(domain.values())) if domain else alt.Undefined
        )
        enc["color"] = alt.Color(f"{color}:N", title=None, scale=scale)
    else:
        enc["color"] = alt.value(th.SERIES[0])
    return cast(alt.Chart, alt.Chart(df).mark_bar(height={"band": 0.7}).encode(**enc))


def _card(
    label: str,
    k: d.Kpi,
    fmt: Callable[[float], str],
    help: str,
    *,
    higher_is_bad: bool = True,
    chart: str = "line",
) -> None:
    st.metric(
        label,
        "n/a" if k.value is None else fmt(k.value),
        # no change -> no delta badge (a red "up 0.00%" reads as a problem)
        delta=None if not k.delta else fmt(k.delta),
        delta_color="inverse" if higher_is_bad else "normal",
        help=help,
        border=True,
        chart_data=k.trend or None,
        chart_type=chart,  # type: ignore[arg-type]
    )


def _banner(state: str, detail: str) -> None:
    with st.container(border=True):
        c1, c2 = st.columns([1, 5], vertical_alignment="center")
        c1.badge(
            th.STATUS_LABEL[state],
            icon=th.STATUS_ICON[state],
            color=th.BADGE_COLOR[state],
        )
        c2.markdown(detail)


def _ops(ctx: Context, metrics: list[str], name: str = "ops_api") -> pd.DataFrame:
    return ctx.src.query(
        name,
        d.OPS_SERIES_SQL,
        {"service": ctx.api_service, "metrics": metrics, **ctx.window.params()},
    )


def _lag(ctx: Context) -> pd.DataFrame:
    return ctx.src.query(
        "ops_lag",
        d.OPS_SERIES_SQL,
        {
            "service": ctx.mon.consumer_lag.service,
            "metrics": ["consumer_lag"],
            **ctx.window.params(),
        },
    )


def _alerts(ctx: Context) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(firing now, firing episodes) from the alert transitions in the window."""
    raw = ctx.src.query("alerts", d.ALERTS_SQL, ctx.window.params())
    hist = d.alert_history(raw, ctx.now)
    # the query also returns each alert's latest row (so "firing now" is always right);
    # episodes that closed before the window do not belong to it
    if not hist.empty:
        ended = pd.to_datetime(hist["resolved"], utc=True)
        hist = hist[ended.isna() | (ended >= pd.Timestamp(ctx.window.start))].reset_index(drop=True)
    return d.firing_now(raw), hist


def _golden_kpis(ctx: Context, series: pd.DataFrame, lag: pd.DataFrame) -> None:
    req = d.per_bucket(series, "requests")
    err = d.per_bucket(series, "errors")
    rps = d.kpi(d.rate_per_second(req, ctx.bucket_seconds))
    er = d.kpi(d.ratio_series(err, req))
    p99 = d.kpi(d.per_bucket(series, "latency_ms", "p99"))
    live = lag[lag["labels"].map(lambda v: d._label(v, "group")).str.startswith("live")]
    lg = d.kpi(d.per_bucket(live, "consumer_lag", "gauge"))
    c = st.columns(4)
    with c[0]:
        _card(
            "Traffic",
            rps,
            lambda v: f"{v:,.1f} req/s",
            "Answered requests per second (latest 10-second bucket).",
            higher_is_bad=False,
            chart="area",
        )
    with c[1]:
        _card("Errors", er, lambda v: f"{100 * v:.2f}%", "HTTP 5xx share of answered requests.")
    with c[2]:
        limit = _p99_limit(ctx.rules)
        _card(
            "Latency p99",
            p99,
            lambda v: f"{v:,.1f} ms",
            f"Worst worker's handler p99 per bucket; alert above {limit:g} ms for 5 min."
            if limit
            else "Handler p99 per bucket.",
        )
    with c[3]:
        _card(
            "Live consumer lag",
            lg,
            lambda v: f"{v:,.0f}",
            "Events published but not yet scored by the live consumer.",
        )


# ---------------------------------------------------------------------------- pages


def overview(ctx: Context) -> None:
    st.title("Overview")
    firing, hist = _alerts(ctx)
    state = d.health(firing)
    if firing.empty:
        detail = "No alert is firing. All signals are within their limits."
    else:
        names = ", ".join(sorted(set(firing["rule"])))
        detail = f"**{len(firing)} alert(s) firing:** {names}. See the Alerts page."
    _banner(state, detail)

    series = _ops(ctx, OPS_METRICS)
    lag = _lag(ctx)
    _golden_kpis(ctx, series, lag)

    left, right = st.columns([3, 2], gap="medium")
    with left, st.container(border=True):
        st.subheader("Traffic by model version", divider="gray")
        rps = d.requests_per_second(series) if not series.empty else pd.DataFrame()
        if not rps.empty:  # idle stretches really are 0 req/s
            rps = d.break_gaps(
                rps,
                "ts_bucket",
                ["model_version"],
                "requests_per_second",
                ctx.bucket_seconds,
                zero=True,
            )
        if rps.empty:
            _empty("requests")
        else:
            _show(
                _timeseries(
                    rps,
                    "requests_per_second",
                    "req/s",
                    color="model_version",
                    y_format=",.0f",
                    area=True,
                ),
                "Answered requests per second, stacked by the model version that scored them.",
                rps,
                "ov-rps",
                height=220,
            )
    with right, st.container(border=True):
        st.subheader("Serving now", divider="gray")
        serving = ctx.src.query(
            "serving", d.SERVING_SQL, {"canary": f"{ctx.canary_prefix}%", **ctx.window.params()}
        )
        if serving.empty:
            _empty("scored events")
        else:
            st.dataframe(
                serving.assign(ring=serving["ring"].fillna("none")),
                column_config={
                    "events": st.column_config.NumberColumn(format="%d"),
                    "last_seen": st.column_config.DatetimeColumn(
                        "last event (UTC)", format="HH:mm:ss"
                    ),
                },
                hide_index=True,
                width="stretch",
            )
            st.caption("Model version scoring each ring's real traffic in this window.")
        st.subheader("Latest rollout", divider="gray")
        ro = ctx.src.query("rollouts", d.ROLLOUT_SQL, ctx.window.params())
        if ro.empty:
            _empty("rollout transitions")
        else:
            last = ro.sort_values("ts").groupby("file_type").tail(1)
            for r in last.itertuples():
                st.markdown(f"**{r.file_type}** · `{r.to_state}` · {str(r.reason)[:90]}")

    with st.container(border=True):
        st.subheader("Recent alerts", divider="gray")
        if hist.empty:
            st.success("No alerts in this time window.", icon=":material/check_circle:")
        else:
            st.dataframe(hist.head(8), hide_index=True, width="stretch")
            st.caption("Latest firing episodes (open ones run to now).")


def service_health(ctx: Context) -> None:
    st.title("Service Health")
    series = _ops(ctx, OPS_METRICS)
    lag_rows = _lag(ctx)
    _golden_kpis(ctx, series, lag_rows)
    reloads = d.reload_events(series) if not series.empty else None

    c1, c2 = st.columns(2, gap="medium")
    with c1, st.container(border=True):
        st.subheader("Traffic", divider="gray")
        rps = d.requests_per_second(series) if not series.empty else pd.DataFrame()
        if not rps.empty:  # idle stretches really are 0 req/s
            rps = d.break_gaps(
                rps,
                "ts_bucket",
                ["model_version"],
                "requests_per_second",
                ctx.bucket_seconds,
                zero=True,
            )
        if rps.empty:
            _empty("requests")
        else:
            _show(
                _timeseries(
                    rps,
                    "requests_per_second",
                    "req/s",
                    color="model_version",
                    y_format=",.0f",
                    area=True,
                    markers=reloads,
                ),
                "Requests per second by model version; grey rules = model reloads.",
                rps,
                "rps",
            )
    with c2, st.container(border=True):
        st.subheader("Errors", divider="gray")
        er = d.error_rate(series) if not series.empty else pd.DataFrame()
        if not er.empty:
            er = d.break_gaps(
                er, "ts_bucket", ["model_version"], "error_rate", ctx.bucket_seconds, zero=False
            )
        if er.empty:
            _empty("requests")
        else:
            _show(
                _timeseries(
                    er, "error_rate", "5xx share", color="model_version", y_format=".2%", limit=0.01
                ),
                "HTTP 5xx share per 10-second bucket; the error alert fires above 1% for 5 min.",
                er,
                "err",
            )

    c3, c4 = st.columns(2, gap="medium")
    limit = _p99_limit(ctx.rules)
    lat = d.latency(series) if not series.empty else pd.DataFrame()
    if not lat.empty:
        lat = d.break_gaps(
            lat, "ts_bucket", ["model_version", "percentile"], "ms", ctx.bucket_seconds, zero=False
        )
    with c3, st.container(border=True):
        st.subheader("Latency", divider="gray")
        if lat.empty:
            _empty("latency samples")
        else:
            pct = {"p50": th.SERIES[0], "p95": th.SERIES[1], "p99": th.SERIES[2]}
            _show(
                _timeseries(
                    lat,
                    "ms",
                    "handler ms",
                    color="percentile",
                    domain=pct,
                    dash="model_version",
                    y_format=",.0f",
                    limit=limit,
                    markers=reloads,
                    log=True,
                ),
                "Handler time p50/p95/p99 (worst API worker per bucket); red line = p99 "
                "alert limit.",
                lat,
                "lat",
            )
    with c4, st.container(border=True):
        st.subheader("Saturation: consumer lag", divider="gray")
        lag = d.consumer_lag(lag_rows) if not lag_rows.empty else pd.DataFrame()
        if not lag.empty:
            lag = d.break_gaps(lag, "ts_bucket", ["group"], "lag", ctx.bucket_seconds, zero=False)
        if lag.empty:
            _empty("consumer lag readings")
        else:
            _show(
                _timeseries(lag, "lag", "events behind", color="group", y_format=",.0f"),
                "Events published but not yet committed, per consumer group.",
                lag,
                "lag",
            )

    c5, c6 = st.columns(2, gap="medium")
    with c5, st.container(border=True):
        st.subheader("Responses by status", divider="gray")
        mix = d.status_mix(series) if not series.empty else pd.DataFrame()
        if mix.empty:
            _empty("requests")
        else:
            dom = {"2xx": th.SERIES[0], "4xx": th.SERIES[3], "5xx": th.STATUS["critical"]}
            chart = (
                alt.Chart(mix)
                .mark_bar(cornerRadiusEnd=0, stroke=th.SURFACE, strokeWidth=1)
                .encode(
                    x=alt.X("ts_bucket:T", title=None),
                    y=alt.Y("count:Q", title="requests per bucket", stack="zero"),
                    color=alt.Color(
                        "status:N",
                        title=None,
                        scale=alt.Scale(domain=list(dom), range=list(dom.values())),
                    ),
                    tooltip=[
                        alt.Tooltip("ts_bucket:T", format="%H:%M:%S"),
                        "status:N",
                        alt.Tooltip("count:Q", format=","),
                    ],
                )
            )
            _show(chart, "Answered requests per bucket by HTTP status class.", mix, "mix")
    with c6, st.container(border=True):
        st.subheader("Dropped on purpose", divider="gray")
        dr = d.drops(series) if not series.empty else pd.DataFrame()
        if dr.empty:
            st.success(
                "Nothing dropped: every lineage row, metric row and latency sample was kept.",
                icon=":material/check_circle:",
            )
        else:
            dom = {
                "lineage rows": th.SERIES[0],
                "metric rows": th.SERIES[1],
                "latency samples": th.SERIES[2],
            }
            chart = (
                alt.Chart(dr)
                .mark_bar()
                .encode(
                    x=alt.X("ts_bucket:T", title=None),
                    y=alt.Y("count:Q", title="dropped per bucket"),
                    color=alt.Color(
                        "kind:N",
                        title=None,
                        scale=alt.Scale(domain=list(dom), range=list(dom.values())),
                    ),
                    tooltip=[
                        alt.Tooltip("ts_bucket:T", format="%H:%M:%S"),
                        "kind:N",
                        alt.Tooltip("count:Q", format=","),
                    ],
                )
            )
            _show(
                chart,
                "Rows the API dropped instead of slowing a request (database down or buffer full).",
                dr,
                "drops",
            )

    if limit is not None and not lat.empty:
        spikes = d.latency_spikes(lat, limit)
        with st.container(border=True):
            st.subheader("Latency spikes → traces", divider="gray")
            if spikes.empty:
                st.success(
                    f"No bucket with p99 above {limit:g} ms.", icon=":material/check_circle:"
                )
            else:
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
                st.dataframe(
                    spikes[["ts_bucket", "model_version", "ms", "jaeger"]],
                    column_config={
                        "ms": st.column_config.ProgressColumn(
                            "p99 ms",
                            format="%.1f",
                            min_value=0,
                            max_value=float(spikes["ms"].max()),
                        ),
                        "jaeger": st.column_config.LinkColumn(
                            "traces", display_text="open in Jaeger"
                        ),
                    },
                    hide_index=True,
                    width="stretch",
                )
                st.caption(
                    f"{len(spikes)} buckets with p99 above {limit:g} ms, worst first; "
                    "each link opens Jaeger for that service and time window."
                )


def model_live(ctx: Context) -> None:
    st.title("Model Live")
    w = ctx.window
    canary = f"{ctx.canary_prefix}%"
    flags = ctx.src.query("flag_rate", d.FLAG_RATE_SQL, {"canary": canary, **w.params()})
    serving = ctx.src.query("serving", d.SERVING_SQL, {"canary": canary, **w.params()})
    nb_series = _ops(ctx, ["never_break_hits", "canary_requests"], "never_break")
    ov = ctx.src.query(
        "override_rate",
        d.OVERRIDE_RATE_SQL,
        {"canary": canary, "top": ctx.mon.dashboard.top_tenants, **w.params()},
    )

    fr = d.flag_rate(flags) if not flags.empty else pd.DataFrame()
    if not fr.empty:  # break the lines across minutes without events
        fr = d.break_gaps(fr, "minute", ["ring", "model_version"], "flag_rate", 60, zero=False)
    events = int(flags["events"].sum()) if not flags.empty else 0
    flagged = int(flags["model_flags"].sum()) if not flags.empty else 0
    canaries = (
        nb_series[nb_series["metric"] == "canary_requests"] if not nb_series.empty else nb_series
    )
    nb = d.never_break_hits(nb_series, canaries) if not nb_series.empty else pd.DataFrame()
    hits = int(nb["never_break_hits"].sum()) if not nb.empty else 0
    scored = int(nb["canaries_scored"].sum()) if not nb.empty else 0
    over = int(ov["overridden"].sum()) if not ov.empty else 0
    per_min = flags.groupby("minute")[["events", "model_flags"]].sum() if not flags.empty else None
    c = st.columns(4)
    c[0].metric(
        "Events scored",
        f"{events:,}",
        border=True,
        chart_data=None if per_min is None else per_min["events"].tolist(),
        chart_type="area",
        help="Real (non-canary) events in the window, per minute.",
    )
    c[1].metric(
        "Model flag rate",
        "n/a" if not events else f"{100 * flagged / events:.1f}%",
        border=True,
        chart_data=None
        if per_min is None
        else (per_min["model_flags"] / per_min["events"]).tolist(),
        help="Share of events with score above the model's threshold (before overrides).",
    )
    c[2].metric(
        "Overrides (top tenants)",
        f"{over:,}",
        border=True,
        help="Allow/block-list overrides applied for the listed tenants.",
    )
    c[3].metric(
        "Never-break hits",
        f"{hits}",
        border=True,
        delta=f"{scored:,} canaries scored",
        delta_color="off",
        help="Never-break canaries flagged by the model. Zero is the only good value.",
    )

    c1, c2 = st.columns(2, gap="medium")
    with c1, st.container(border=True):
        st.subheader("Flag rate by ring", divider="gray")
        if fr.empty:
            _empty("scored events")
        else:
            _show(
                _timeseries(
                    fr,
                    "flag_rate",
                    "flagged share",
                    x="minute",
                    color="ring",
                    domain=th.RING_COLORS,
                    dash="model_version",
                    y_format=".1%",
                ),
                "Share of real events flagged per minute; rings should track each other "
                "unless a ring serves a different model.",
                fr,
                "flag",
            )
    with c2, st.container(border=True):
        st.subheader("Verdict volume", divider="gray")
        if flags.empty:
            _empty("scored events")
        else:
            vol = flags.groupby("minute", as_index=False)[["events", "model_flags"]].sum()
            vol = pd.DataFrame(
                {
                    "minute": list(vol["minute"]) * 2,
                    "verdict": ["flagged"] * len(vol) + ["cleared"] * len(vol),
                    "files": list(vol["model_flags"]) + list(vol["events"] - vol["model_flags"]),
                }
            )
            dom = {"flagged": th.SERIES[1], "cleared": th.SERIES[0]}
            chart = (
                alt.Chart(vol)
                .mark_bar(cornerRadiusEnd=0, stroke=th.SURFACE, strokeWidth=1)
                .encode(
                    x=alt.X("minute:T", title=None),
                    y=alt.Y("files:Q", title="files per minute", stack="zero"),
                    color=alt.Color(
                        "verdict:N",
                        title=None,
                        scale=alt.Scale(domain=list(dom), range=list(dom.values())),
                    ),
                    tooltip=[
                        alt.Tooltip("minute:T", format="%H:%M"),
                        "verdict:N",
                        alt.Tooltip("files:Q", format=","),
                    ],
                )
            )
            _show(chart, "Model verdicts per minute (before tenant overrides).", vol, "vol")

    c3, c4 = st.columns(2, gap="medium")
    with c3, st.container(border=True):
        st.subheader("Traffic share by ring", divider="gray")
        share = d.ring_share(serving)
        if share.empty:
            _empty("scored events")
        else:
            chart = (
                alt.Chart(share)
                .mark_bar(cornerRadius=4, stroke=th.SURFACE, strokeWidth=2)
                .encode(
                    x=alt.X("share:Q", stack="zero", title=None, axis=alt.Axis(format=".0%")),
                    color=alt.Color(
                        "ring:N",
                        title=None,
                        scale=alt.Scale(
                            domain=list(th.RING_COLORS), range=list(th.RING_COLORS.values())
                        ),
                    ),
                    order=alt.Order("ring:N"),
                    tooltip=[
                        "ring:N",
                        alt.Tooltip("events:Q", format=","),
                        alt.Tooltip("share:Q", format=".1%"),
                    ],
                )
            )
            labels = (
                alt.Chart(share[share["share"] >= 0.08])
                .mark_text(color="#ffffff", fontSize=12, dx=-6, align="right")
                .encode(x=alt.X("cum:Q", stack=None), text="label:N")
                .transform_window(cum="sum(share)", sort=[{"field": "ring"}])
                .transform_calculate(label="datum.ring + ' ' + format(datum.share, '.0%')")
            )
            _show(
                alt.layer(chart, labels),
                "Part of real traffic per rollout ring (stable "
                "tenant hash: ring0 5%, ring1 25%, ring2 the rest).",
                share,
                "share",
                height=90,
            )
    with c4, st.container(border=True):
        st.subheader("Score distribution", divider="gray")
        hist = ctx.src.query(
            "score_hist", d.SCORE_HIST_SQL, {"canary": canary, "bins": SCORE_BINS, **w.params()}
        )
        sh = d.score_histogram(hist, SCORE_BINS)
        if sh.empty:
            _empty("scored events")
        else:
            chart = (
                alt.Chart(sh)
                .mark_bar(cornerRadius=0, stroke=th.SURFACE, strokeWidth=1)
                .encode(
                    x=alt.X(
                        "score_lo:Q",
                        title="model score",
                        scale=alt.Scale(domain=[0, 1]),
                    ),
                    x2="score_hi:Q",
                    y2=alt.datum(0),  # columns from a zero baseline (x/x2 alone = horizontal)
                    y=alt.Y("share:Q", title="share of ring's files", axis=alt.Axis(format=".0%")),
                    color=alt.Color(
                        "ring:N",
                        title=None,
                        legend=None,
                        scale=alt.Scale(
                            domain=list(th.RING_COLORS), range=list(th.RING_COLORS.values())
                        ),
                    ),
                    tooltip=[
                        "ring:N",
                        alt.Tooltip("score_lo:Q", title="from", format=".2f"),
                        alt.Tooltip("score_hi:Q", title="to", format=".2f"),
                        alt.Tooltip("files:Q", format=","),
                        alt.Tooltip("share:Q", format=".1%"),
                    ],
                )
                .properties(height=70)
                .facet(row=alt.Row("ring:N", title=None))
            )
            st.altair_chart(chart, theme=None, width="stretch")
            st.caption(
                "Where scores fall, per ring (20 bins); a healthy detector piles up near 0 and 1."
            )
            with st.expander("data", icon=":material/table:"):
                st.dataframe(sh, hide_index=True, width="stretch")

    c5, c6 = st.columns(2, gap="medium")
    with c5, st.container(border=True):
        st.subheader("Override rate per tenant", divider="gray")
        if ov.empty or not (ov["overridden"] > 0).any():
            st.success(
                "No tenant override was applied in this window.", icon=":material/check_circle:"
            )

        else:
            orr = d.override_rate(ov[ov["overridden"] > 0])
            _show(
                _bars(orr, "override_rate", "tenant_id", x_title="override rate", x_format=".1%"),
                f"Tenants with the most allow/block-list overrides (top "
                f"{ctx.mon.dashboard.top_tenants}).",
                orr,
                "ovr",
                height=320,
            )
    with c6, st.container(border=True):
        st.subheader("Never-break canaries", divider="gray")
        if nb.empty:
            st.info(
                "No canaries were scored in this window (they run during a rollout).",
                icon=":material/info:",
            )
        else:
            long = nb.melt("ts_bucket", var_name="series", value_name="files")
            dom = {"canaries_scored": th.SERIES[0], "never_break_hits": th.STATUS["critical"]}
            _show(
                _timeseries(long, "files", "files", color="series", domain=dom, y_format=",.0f"),
                "Canaries scored vs flagged; a flat zero for hits is the only good value.",
                nb,
                "nb",
            )

    with st.container(border=True):
        st.subheader("Rollout timeline", divider="gray")
        ro = ctx.src.query("rollouts", d.ROLLOUT_SQL, w.params())
        if ro.empty:
            _empty("rollout transitions")
        else:
            seg = d.clip_spans(d.rollout_segments(ro, ctx.now), "start", "end", ctx.window)
            dom = th.ROLLOUT_STATE_COLORS
            chart = (
                alt.Chart(seg)
                .mark_bar(cornerRadius=3, height={"band": 0.6})
                .encode(
                    x=alt.X("start:T", title=None),
                    x2="end:T",
                    y=alt.Y("file_type:N", title=None),
                    color=alt.Color(
                        "state:N",
                        title=None,
                        scale=alt.Scale(domain=list(dom), range=list(dom.values())),
                    ),
                    tooltip=[
                        "file_type:N",
                        "rollout_id:N",
                        "state:N",
                        alt.Tooltip("start:T", format="%Y-%m-%d %H:%M:%S"),
                        alt.Tooltip("end:T", format="%Y-%m-%d %H:%M:%S"),
                    ],
                )
            )
            _show(
                chart,
                "Each rollout's states over time (the last state before the window is included).",
                ro[["ts", "file_type", "rollout_id", "from_state", "to_state", "actor", "reason"]],
                "ro",
                height=60 + 40 * seg["file_type"].nunique(),
            )


def alerts_page(ctx: Context) -> None:
    st.title("Alerts")
    firing, hist = _alerts(ctx)
    stats = d.alert_stats(hist, firing)
    c = st.columns(4)
    c[0].metric(
        "Firing now", stats.firing, border=True, help="Alerts whose latest state is FIRING."
    )
    c[1].metric("Episodes in window", stats.episodes, border=True)
    c[2].metric(
        "Mean time to resolve",
        "n/a"
        if stats.mean_minutes_to_resolve is None
        else f"{stats.mean_minutes_to_resolve:.1f} min",
        border=True,
        help="Average FIRING -> RESOLVED time of closed episodes.",
    )
    c[3].metric(
        "Noisiest rule",
        "n/a" if stats.noisiest_rule is None else f"{stats.episodes_of_noisiest} episodes",
        delta=stats.noisiest_rule,
        delta_color="off",
        delta_arrow="off",
        border=True,
    )

    with st.container(border=True):
        st.subheader("Firing now", divider="gray")
        if firing.empty:
            st.success("No alert is firing.", icon=":material/check_circle:")
        else:
            cols = st.columns(min(len(firing), 3))
            for i, r in enumerate(firing.itertuples()):
                status = th.SEVERITY_STATUS.get(str(r.severity), "serious")
                with cols[i % len(cols)], st.container(border=True):
                    st.badge(
                        f"{r.severity} · {th.STATUS_LABEL[status]}",
                        icon=th.STATUS_ICON[status],
                        color=th.BADGE_COLOR[status],
                    )
                    st.markdown(f"**{r.rule}**")
                    since = pd.Timestamp(cast(Any, r.ts))
                    st.caption(f"since {since:%Y-%m-%d %H:%M:%S} UTC")
                    st.markdown(str(r.message))

    c1, c2 = st.columns([3, 2], gap="medium")
    with c1, st.container(border=True):
        st.subheader("Episode timeline", divider="gray")
        if hist.empty:
            _empty("alerts")
        else:
            tl = hist.assign(
                end=hist["resolved"].where(hist["resolved"].notna(), pd.Timestamp(ctx.now)),
                status=hist["severity"].map(
                    lambda s: th.STATUS_LABEL[th.SEVERITY_STATUS.get(str(s), "serious")]
                ),
                state=hist["resolved"].map(lambda v: "resolved" if pd.notna(v) else "open"),
            )
            tl = d.clip_spans(tl, "fired", "end", ctx.window)  # only the selected window
            dom = {th.STATUS_LABEL[k]: th.STATUS[k] for k in ("critical", "warning", "serious")}
            chart = (
                alt.Chart(tl)
                .mark_bar(cornerRadius=3, height={"band": 0.6})
                .encode(
                    x=alt.X("fired:T", title=None),
                    x2="end:T",
                    y=alt.Y("rule:N", title=None),
                    color=alt.Color(
                        "status:N",
                        title="severity",
                        scale=alt.Scale(domain=list(dom), range=list(dom.values())),
                    ),
                    opacity=alt.condition(alt.datum.state == "open", alt.value(1), alt.value(0.65)),
                    tooltip=[
                        "rule:N",
                        "dedup_key:N",
                        "state:N",
                        alt.Tooltip("fired:T", format="%H:%M:%S"),
                        alt.Tooltip("end:T", format="%H:%M:%S"),
                        alt.Tooltip("minutes:Q", format=".1f"),
                        "message:N",
                    ],
                )
            )
            _show(
                chart,
                "Each FIRING -> RESOLVED episode as a bar (open episodes run to now).",
                hist,
                "tl",
                height=60 + 36 * hist["rule"].nunique(),
            )
    with c2, st.container(border=True):
        st.subheader("Episodes by rule", divider="gray")
        if hist.empty:
            _empty("alerts")
        else:
            by = hist.groupby("rule", as_index=False).agg(
                episodes=("rule", "size"), minutes=("minutes", "sum")
            )
            _show(
                _bars(by, "episodes", "rule", x_title="episodes"),
                "How often each rule fired in the window.",
                by,
                "by",
                height=60 + 36 * len(by),
            )

    with st.container(border=True):
        st.subheader("History", divider="gray")
        if hist.empty:
            _empty("alerts")
        else:
            st.dataframe(
                hist,
                hide_index=True,
                width="stretch",
                column_config={"minutes": st.column_config.NumberColumn(format="%.1f")},
            )
        st.caption("One row per firing episode with its duration.")

    with st.container(border=True):
        st.subheader("Rules", divider="gray")
        st.dataframe(d.rule_table(ctx.rules), hide_index=True, width="stretch")
        st.caption(
            f"configs/alert_rules.yaml, evaluated every {ctx.rules.evaluate_every_seconds:g} s "
            f"on ops_metrics; only state changes are stored (append-only)."
        )
