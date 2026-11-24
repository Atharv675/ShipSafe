"""Stage 8 Streamlit operations pages: transforms + every page rendered with AppTest."""

from __future__ import annotations

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
from sentinel.dashboard.ops_pages import Context, {page}
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


# ------------------------------------------------------------------ pages (AppTest)


def test_service_health_page_renders_charts_and_jaeger_links() -> None:
    at = _run("service_health")
    text = _text(at)
    for title in ("Requests per second", "Error rate", "Latency", "Consumer lag"):
        assert title in text
    assert "Latency spikes" in text
    assert len(at.caption) >= 5  # one caption per chart
    [spikes] = [df for df in at.dataframe if "jaeger" in df.value.columns]
    link = spikes.value["jaeger"].iloc[0]
    assert link.startswith("http://127.0.0.1:16686/search?") and "service=sentinel-api" in link


def test_model_live_page_renders() -> None:
    at = _run("model_live")
    text = _text(at)
    for title in ("Flag rate", "Override rate", "Never-break hits", "Rollout state"):
        assert title in text
    metrics = {m.label: m.value for m in at.metric}
    assert metrics == {"never-break hits": "0", "canaries scored": "500"}


def test_alerts_page_renders_firing_history_and_rules() -> None:
    at = _run("alerts_page")
    assert "Firing now" in _text(at)
    firing = at.dataframe[0].value
    assert list(firing["rule"]) == ["never_break_hit"]
    rules = at.dataframe[-1].value
    assert len(rules) == len(load_alert_rules().rules)


@pytest.mark.parametrize("page", ["service_health", "model_live", "alerts_page"])
def test_pages_render_on_an_empty_database(page: str) -> None:
    at = _run(page, empty=True)
    assert at.info or at.success  # an explicit "nothing yet" instead of a broken chart


def test_app_entry_renders_the_default_page_with_sidebar() -> None:
    at = AppTest.from_string(
        "from tests.dashboard_support import NOW, FakeSource\n"
        "from sentinel.dashboard.app import main\n"
        "main(FakeSource(), now=NOW)\n"
    )
    at.run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    assert at.sidebar.selectbox[0].value == "1 hour"
    assert "Service Health" in _text(at)
    at.sidebar.selectbox[0].set_value("24 hours").run(timeout=60)
    assert not at.exception
