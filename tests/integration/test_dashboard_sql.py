"""Stage 10 review fix: every dashboard query (Operations + ML) runs on the real database as
the read-only `sentinel_dashboard` role, with the parameters the pages send (lists included).

Unit tests serve canned frames; this catches SQL, type and grant errors (the Stage 8
"malformed array literal" bug was exactly that kind and only showed on the live stack)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from sentinel.dashboard import data as d
from sentinel.dashboard import ml_data as m
from sentinel.dashboard.app import CachedSource

pytestmark = pytest.mark.integration

DASHBOARD_DSN = os.environ.get("SENTINEL_IT_DASHBOARD_DSN", "")
if not DASHBOARD_DSN:
    pytest.skip("stack not configured (run via make test-integration)", allow_module_level=True)

NOW = datetime.now(UTC)
WINDOW = {"start": NOW - timedelta(days=14), "end": NOW}
FILTERS = m.Filters().params()
QUERIES: list[tuple[str, str, dict[str, Any]]] = [
    ("ops", d.OPS_SERIES_SQL, {"service": "api", "metrics": ["requests", "latency_ms"], **WINDOW}),
    ("flag_rate", d.FLAG_RATE_SQL, {"canary": "canary-%", **WINDOW}),
    ("override_rate", d.OVERRIDE_RATE_SQL, {"canary": "canary-%", "top": 5, **WINDOW}),
    ("rollouts", d.ROLLOUT_SQL, WINDOW),
    ("alerts", d.ALERTS_SQL, WINDOW),
    ("serving", d.SERVING_SQL, {"canary": "canary-%", **WINDOW}),
    ("score_hist", d.SCORE_HIST_SQL, {"canary": "canary-%", "bins": 20, **WINDOW}),
    ("filter_values", m.FILTER_VALUES_SQL, {"canary": "canary-%", "top": 50, **WINDOW}),
    ("flag_hourly", m.FLAG_HOURLY_SQL, {"canary": "canary-%", **FILTERS, **WINDOW}),
    (
        "flag_hourly_filtered",
        m.FLAG_HOURLY_SQL,
        {"canary": "canary-%", "model": "x/1", "ring": "ring0", "tenant": "t", **WINDOW},
    ),
    ("gates", m.GATES_SQL, {}),
    ("gate_bundle", m.GATE_BUNDLE_SQL, {"decision_id": "none"}),
    ("transitions", m.TRANSITIONS_SQL, {}),
    ("ring_flag", m.RING_FLAG_SQL, {"canary": "canary-%", "tenant": None, **WINDOW}),
    ("lookup", m.LOOKUP_SQL, {"q": "no-such-file", "limit": 5}),
    ("shadow_lookup", m.SHADOW_LOOKUP_SQL, {"q": "no-such-file", "limit": 5}),
]


@pytest.mark.parametrize(("name", "sql", "params"), QUERIES, ids=[q[0] for q in QUERIES])
def test_query_runs_as_the_dashboard_role(name: str, sql: str, params: dict[str, Any]) -> None:
    src = d.PostgresSource(DASHBOARD_DSN)
    df = src.query(name, sql, params)
    assert df is not None  # columns came back; an error would have raised


def test_the_cached_path_sends_lists_as_arrays() -> None:
    """The real app goes through CachedSource (st.cache_data); lists must stay arrays."""
    name, sql, params = QUERIES[0]
    df = CachedSource(d.PostgresSource(DASHBOARD_DSN), 1).query(name, sql, params)
    assert df is not None


def test_dashboard_role_cannot_write() -> None:
    with (
        psycopg.connect(DASHBOARD_DSN) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("DELETE FROM gate_decisions WHERE decision_id = 'none'")
