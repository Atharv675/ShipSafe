"""Sentinel Gate Streamlit app (Stage 8: Operations; Stage 9 adds an ML section).

    streamlit run src/sentinel/dashboard/app.py     (compose service `dashboard`, :8501)

Reads PostgreSQL as the read-only `sentinel_dashboard` role (SENTINEL_DASHBOARD_DSN).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import streamlit as st

from sentinel.config import (
    load_alert_rules,
    load_monitoring_config,
    load_rollout_config,
    load_serving_config,
)
from sentinel.dashboard import data as d
from sentinel.dashboard import theme
from sentinel.dashboard.ops_pages import (
    Context,
    alerts_page,
    model_live,
    overview,
    service_health,
)

LOOKBACKS = {
    "15 min": 15,
    "1 hour": 60,
    "6 hours": 360,
    "24 hours": 1440,
    "7 days": 10080,
    "14 days": 20160,
}


class CachedSource:
    """Short-TTL cache in front of a source (st.cache_data, keyed by query and params)."""

    def __init__(self, inner: d.Source, ttl: float) -> None:
        self._inner = inner

        @st.cache_data(ttl=ttl, show_spinner=False)
        def _q(name: str, sql: str, params: tuple[tuple[str, Any], ...]) -> pd.DataFrame:
            # lists were frozen to tuples only to be hashable for the cache; give them back as
            # lists: psycopg sends a tuple as a record, but `= ANY(%s)` needs an array
            return self._inner.query(
                name, sql, {k: list(v) if isinstance(v, tuple) else v for k, v in params}
            )

        self._q = _q

    def query(self, name: str, sql: str, params: Mapping[str, Any]) -> pd.DataFrame:
        frozen = tuple(
            sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in params.items())
        )
        return self._q(name, sql, frozen)


def _page(fn: Callable[[Context], None], ctx: Context) -> Callable[[], None]:
    def run() -> None:
        fn(ctx)

    run.__name__ = fn.__name__  # st.Page identifies callables by name
    return run


def main(source: d.Source | None = None, now: datetime | None = None) -> None:
    st.set_page_config(
        page_title="Sentinel Gate · Operations",
        page_icon=":material/shield:",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    theme.register()
    mon = load_monitoring_config()
    rules = load_alert_rules()
    if source is None:
        dsn = os.environ.get("SENTINEL_DASHBOARD_DSN")
        if not dsn:
            st.error("SENTINEL_DASHBOARD_DSN is not set.")
            st.stop()
        source = CachedSource(d.PostgresSource(dsn), mon.dashboard.cache_ttl_seconds)
    allowed = {k: v for k, v in LOOKBACKS.items() if v <= mon.dashboard.max_lookback_minutes}
    labels = list(allowed)
    default = next(
        (i for i, k in enumerate(labels) if allowed[k] == mon.dashboard.default_lookback_minutes),
        0,
    )
    at = now or datetime.now(UTC)
    with st.sidebar:
        st.markdown("### :material/shield: Sentinel Gate")
        st.caption("Malware-model release gate · operations")
        choice = st.selectbox("Time window", labels, index=default)
        if st.button("Refresh now", icon=":material/refresh:", width="stretch"):
            st.cache_data.clear()
        st.caption(
            f"As of {at:%Y-%m-%d %H:%M:%S} UTC. Cached for {mon.dashboard.cache_ttl_seconds:g} s."
        )
        st.divider()
        st.caption(
            f"[Jaeger traces]({mon.tracing.jaeger_ui_url}) · metrics: PostgreSQL ops_metrics"
        )
    ctx = Context(
        src=source,
        window=d.Window.last(allowed[choice], at, mon.dashboard.cache_ttl_seconds),
        mon=mon,
        rules=rules,
        api_service=load_serving_config().service_name,
        canary_prefix=load_rollout_config().canaries.tenant_prefix,
        now=at,
    )
    nav = st.navigation(
        {
            "Operations": [
                st.Page(
                    _page(overview, ctx),
                    title="Overview",
                    url_path="overview",
                    icon=":material/dashboard:",
                    default=True,
                ),
                st.Page(
                    _page(service_health, ctx),
                    title="Service Health",
                    url_path="service-health",
                    icon=":material/monitor_heart:",
                ),
                st.Page(
                    _page(model_live, ctx),
                    title="Model Live",
                    url_path="model-live",
                    icon=":material/model_training:",
                ),
                st.Page(
                    _page(alerts_page, ctx),
                    title="Alerts",
                    url_path="alerts",
                    icon=":material/notifications_active:",
                ),
            ]
        }
    )
    nav.run()


if __name__ == "__main__":
    main()
