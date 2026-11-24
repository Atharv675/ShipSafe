"""Stage 8 Streamlit operations pages: transforms + every page rendered with AppTest."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from sentinel.config import load_alert_rules
from sentinel.dashboard import data as d
from tests.dashboard_support import NOW, frames

PAGE_SCRIPT = """
from tests.dashboard_support import NOW, FakeSource
from sentinel.config import (load_alert_rules, load_monitoring_config, load_rollout_config,
                             load_serving_config)
from sentinel.dashboard import data as d
from sentinel.dashboard import theme
from sentinel.dashboard.ops_pages import Context, {page}
theme.register()
mon = load_monitoring_config()
src = FakeSource(empty={empty})
ctx = Context(src, d.Window.last(60, NOW, 10), mon, load_alert_rules(),
              load_serving_config().service_name, load_rollout_config().canaries.tenant_prefix,
              NOW)
{page}(ctx)
"""


def _run(page: str, empty: bool = False) -> AppTest:
    at = AppTest.from_string(PAGE_SCRIPT.format(page=page, empty=empty))
    at.run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    return at


def _text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [h.value for h in at.header] + [s.value for s in at.subheader]
    parts += [t.value for t in at.title]
    return "\n".join(str(p) for p in parts)


# ------------------------------------------------------------------ transforms


def test_window_is_aligned_so_cached_queries_hit() -> None:
    w = d.Window.last(60, datetime(2026, 11, 23, 12, 0, 7, tzinfo=UTC), 10)
    assert w.end == datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
    assert w.end - w.start == timedelta(minutes=60)


def test_rates_from_ops_series() -> None:
    api = frames()["ops_api"]
    rps = d.requests_per_second(api)
    first = rps[rps["ts_bucket"] == rps["ts_bucket"].min()].set_index("model_version")
    assert first.loc["sentinel-win64/1", "requests_per_second"] == pytest.approx(95.0)
    assert first.loc["sentinel-win64/5", "requests_per_second"] == pytest.approx(5.1)
    er = d.error_rate(api).set_index(["ts_bucket", "model_version"])
    assert er["error_rate"].xs("sentinel-win64/5", level=1).iloc[0] == pytest.approx(1 / 51)
    assert er["error_rate"].xs("sentinel-win64/1", level=1).iloc[0] == 0
    lat = d.latency(api)
    assert set(lat["percentile"]) == {"p50", "p95", "p99"}
    spikes = d.latency_spikes(lat, 20.0)
    assert list(spikes["ms"]) == [31.0]
    assert list(d.reload_events(api)["model_version"]) == ["sentinel-win64/5"]
    lag = d.consumer_lag(frames()["ops_lag"])
    assert lag[lag["group"] == "sentinel-live"]["lag"].max() == 45.0


def test_never_break_series_show_zero_hits_next_to_canaries_scored() -> None:
    nb = frames()["never_break"]
    out = d.never_break_hits(nb, nb[nb["metric"] == "canary_requests"])
    assert out["canaries_scored"].sum() == 500 and out["never_break_hits"].sum() == 0


def test_jaeger_link_targets_service_and_window() -> None:
    start = datetime(2026, 11, 23, 11, 0, tzinfo=UTC)
    url = d.jaeger_url(
        "http://127.0.0.1:16686/", "sentinel-api", start, start + timedelta(seconds=10), 30
    )
    u = urlparse(url)
    q = parse_qs(u.query)
    assert (u.netloc, u.path) == ("127.0.0.1:16686", "/search")
    assert q["service"] == ["sentinel-api"] and q["lookback"] == ["custom"]
    assert int(q["start"][0]) == int((start.timestamp() - 30) * 1e6)
    assert int(q["end"][0]) == int((start.timestamp() + 40) * 1e6)


def test_alert_history_pairs_episodes_and_keeps_open_ones() -> None:
    alerts = frames()["alerts"]
    hist = d.alert_history(alerts, NOW)
    assert len(hist) == 2
    closed = hist[hist["rule"] == "api_p99_latency"].iloc[0]
    assert closed["minutes"] == 10.0 and closed["resolved"] is not None
    still = hist[hist["rule"] == "never_break_hit"].iloc[0]
    assert pd.isna(still["resolved"]) and still["minutes"] == 5.0
    firing = d.firing_now(alerts)
    assert list(firing["rule"]) == ["never_break_hit"]


def test_rule_table_lists_every_configured_rule() -> None:
    t = d.rule_table(load_alert_rules())
    assert list(t["rule"]) == [r.name for r in load_alert_rules().rules]
    assert t.set_index("rule").loc["never_break_hit", "for"] == "at once"


def test_kpis_sparklines_and_breakdowns() -> None:
    api = frames()["ops_api"]
    req = d.per_bucket(api, "requests")
    assert req.iloc[0] == 1001  # 950 + 50 + 1 answered in the first bucket
    k = d.kpi(d.rate_per_second(req, 10))
    assert k.value == pytest.approx(100.1) and len(k.trend) == 30 and k.delta == 0
    assert d.kpi(pd.Series(dtype="float64")) == d.Kpi(None, None, [])
    p99 = d.per_bucket(api, "latency_ms", "p99")
    assert p99.max() == 31.0
    assert d.ratio_series(d.per_bucket(api, "errors"), req).iloc[0] == pytest.approx(1 / 1001)
    mix = d.status_mix(api).groupby("status")["count"].sum()
    assert dict(mix) == {"2xx": 30_000, "5xx": 30}
    dr = d.drops(api)
    assert list(dr["kind"]) == ["lineage rows"] and dr["count"].sum() == 40


def test_ring_share_score_histogram_and_rollout_segments() -> None:
    f = frames()
    share = d.ring_share(f["serving"]).set_index("ring")["share"]
    assert share.sum() == pytest.approx(1.0) and share["ring0"] == pytest.approx(0.05)
    h = d.score_histogram(f["score_hist"], 20)
    ring0 = h[h["ring"] == "ring0"]
    assert ring0["share"].sum() == pytest.approx(1.0)
    assert (ring0["score_lo"].min(), ring0["score_hi"].max()) == (0.0, 1.0)
    seg = d.rollout_segments(f["rollouts"], NOW)
    assert list(seg["state"]) == ["APPROVED", "RING0", "ROLLED_BACK"]
    assert (seg["end"].iloc[:-1].values == seg["start"].iloc[1:].values).all()  # contiguous
    assert seg["end"].iloc[-1] == NOW


def test_alert_stats_and_health_state() -> None:
    alerts = frames()["alerts"]
    hist = d.alert_history(alerts, NOW)
    firing = d.firing_now(alerts)
    s = d.alert_stats(hist, firing)
    assert (s.firing, s.episodes, s.mean_minutes_to_resolve) == (1, 2, 10.0)
    assert s.episodes_of_noisiest == 1
    assert d.health(firing) == "critical"  # a page-severity alert is firing
    assert d.health(firing.assign(severity="ticket")) == "warning"
    assert d.health(firing.iloc[0:0]) == "good"


# ------------------------------------------------------------------ pages (AppTest)


def test_overview_page_leads_with_health_and_golden_signal_cards() -> None:
    at = _run("overview")
    text = _text(at)
    assert "Overview" in text and "1 alert(s) firing" in text
    labels = [m.label for m in at.metric]
    assert labels == ["Traffic", "Errors", "Latency p99", "Live consumer lag"]
    assert at.metric[0].value == "100.1 req/s"
    for title in ("Traffic by model version", "Serving now", "Latest rollout", "Recent alerts"):
        assert title in text


def test_service_health_page_renders_golden_signals_and_jaeger_links() -> None:
    at = _run("service_health")
    text = _text(at)
    for title in (
        "Traffic",
        "Errors",
        "Latency",
        "Saturation: consumer lag",
        "Responses by status",
        "Dropped on purpose",
        "Latency spikes",
    ):
        assert title in text
    assert len(at.get("vega_lite_chart")) == 6
    assert len(at.expander) == 6  # a table view behind every chart
    [spikes] = [df for df in at.dataframe if "jaeger" in df.value.columns]
    link = spikes.value["jaeger"].iloc[0]
    assert link.startswith("http://127.0.0.1:16686/search?") and "service=sentinel-api" in link


def test_model_live_page_renders() -> None:
    at = _run("model_live")
    text = _text(at)
    for title in (
        "Flag rate by ring",
        "Verdict volume",
        "Traffic share by ring",
        "Score distribution",
        "Override rate per tenant",
        "Never-break canaries",
        "Rollout timeline",
    ):
        assert title in text
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Never-break hits"] == "0"
    assert metrics["Events scored"] == "15,000"
    assert len(at.get("vega_lite_chart")) == 7


def test_alerts_page_renders_cards_timeline_history_and_rules() -> None:
    at = _run("alerts_page")
    text = _text(at)
    assert "never_break_hit" in text  # the firing card
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Firing now"] == "1" and metrics["Mean time to resolve"] == "10.0 min"
    assert len(at.get("vega_lite_chart")) == 2  # timeline + by rule
    rules = at.dataframe[-1].value
    assert len(rules) == len(load_alert_rules().rules)


@pytest.mark.parametrize("page", ["overview", "service_health", "model_live", "alerts_page"])
def test_pages_render_on_an_empty_database(page: str) -> None:
    at = _run(page, empty=True)
    assert at.info or at.success  # an explicit "nothing yet" instead of a broken chart


def test_app_entry_renders_overview_by_default_with_sidebar() -> None:
    at = AppTest.from_string(
        "from tests.dashboard_support import NOW, FakeSource\n"
        "from sentinel.dashboard.app import main\n"
        "main(FakeSource(), now=NOW)\n"
    )
    at.run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    assert at.sidebar.selectbox[0].value == "1 hour"
    assert "Overview" in _text(at)
    at.sidebar.selectbox[0].set_value("24 hours").run(timeout=60)
    assert not at.exception


def test_cached_source_hands_lists_back_to_the_database_as_lists() -> None:
    """Regression (seen live 2026-11-24): the cache froze list params to tuples and psycopg
    sent them as a record, so `metric = ANY(%s)` failed on every page of the real app."""
    from sentinel.dashboard.app import CachedSource

    seen: list[dict[str, object]] = []

    class Recorder:
        def query(self, name: str, sql: str, params: Mapping[str, object]) -> pd.DataFrame:
            seen.append(dict(params))
            return pd.DataFrame({"x": [1]})

    src = CachedSource(Recorder(), ttl=60)
    out = src.query("q-regression", "SELECT 1", {"metrics": ["requests", "errors"], "n": 3})
    assert list(out["x"]) == [1]
    assert seen == [{"metrics": ["requests", "errors"], "n": 3}]
    assert isinstance(seen[0]["metrics"], list)


def test_gaps_are_shown_not_bridged_and_timelines_clip_to_the_window() -> None:
    t0 = NOW - timedelta(minutes=30)
    df = pd.DataFrame(
        {
            "ts_bucket": [t0, t0 + timedelta(seconds=10), t0 + timedelta(minutes=20)],
            "v": [5.0, 6.0, 7.0],
            "g": ["a", "a", "a"],
        }
    )
    zeros = d.break_gaps(df, "ts_bucket", ["g"], "v", 10, zero=True)
    assert list(zeros["v"]) == [5.0, 6.0, 0.0, 0.0, 7.0]  # idle stretch drops to zero
    broken = d.break_gaps(df, "ts_bucket", ["g"], "v", 10, zero=False)
    assert len(broken) == 4 and broken["v"].isna().sum() == 1  # one break, no fake zeros
    w = d.Window(NOW - timedelta(hours=1), NOW)
    spans = pd.DataFrame(
        {
            "start": [NOW - timedelta(days=2), NOW - timedelta(days=3)],
            "end": [NOW - timedelta(minutes=5), NOW - timedelta(days=2)],
        }
    )
    clipped = d.clip_spans(spans, "start", "end", w)
    assert len(clipped) == 1 and clipped["start"].iloc[0] == pd.Timestamp(w.start)
