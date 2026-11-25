"""Stages 8-9: render every page against the LIVE stack and print what it shows.

    python scripts/dashboard_check.py [--minutes 15] [--lookup <file_id>]  (make dashboard-check)

Runs each page through Streamlit's AppTest with the real read-only sources (PostgreSQL via
SENTINEL_DASHBOARD_DSN, the MLflow registry via SENTINEL_MLFLOW_URI, the results folder), so
it proves the pages draw live data, not just that they load. Each page is rendered twice and
the second (warm-cache) time is reported against the 3-second budget.
"""

from __future__ import annotations

import argparse
import json
import time

from streamlit.testing.v1 import AppTest

OPS_PAGES = ("overview", "service_health", "model_live", "alerts_page")
ML_PAGES = (
    "ml_overview",
    "model_registry",
    "gate_viewer",
    "head_to_head_page",
    "drift_page",
    "rollout_timeline",
    "experiment_results",
    "prediction_lookup",
)
BUDGET_SECONDS = 3.0

SCRIPT = """
import os
from datetime import UTC, datetime
from sentinel.config import (Settings, load_alert_rules, load_gate_policy,
                             load_monitoring_config, load_rollout_config, load_serving_config)
from sentinel.dashboard import data as d
from sentinel.dashboard import ml_data as m
from sentinel.dashboard import theme
from sentinel.dashboard.app import CachedSource
from sentinel.dashboard.ml_pages import MLContext
from sentinel.dashboard.ops_pages import Context
from sentinel.dashboard import ops_pages, ml_pages
theme.register()
mon = load_monitoring_config()
ro = load_rollout_config()
now = datetime.now(UTC)
ctx = Context(CachedSource(d.PostgresSource(os.environ["SENTINEL_DASHBOARD_DSN"]), 10),
              d.Window.last({minutes}, now, 1), mon, load_alert_rules(),
              load_serving_config().service_name, ro.canaries.tenant_prefix, now)
if "{page}" in {ml}:
    ml_ctx = MLContext(ctx, m.MlflowRegistry(os.environ["SENTINEL_MLFLOW_URI"]),
                       m.ResultsDir(Settings().results_dir), mon.dashboard.ml, m.Filters(),
                       {{str(k): v for k, v in ro.ring_aliases.items()}}, ro.fallback_alias,
                       load_gate_policy().operating_fpr)
    getattr(ml_pages, "{page}")(ml_ctx)
else:
    getattr(ops_pages, "{page}")(ctx)
"""


def _render(page: str, minutes: int, lookup: str | None) -> tuple[AppTest, float]:
    at = AppTest.from_string(SCRIPT.format(page=page, minutes=minutes, ml=repr(ML_PAGES)))
    t0 = time.perf_counter()
    at.run(timeout=180)
    if lookup and page == "prediction_lookup" and not at.exception:
        at.text_input[0].input(lookup)
        t0 = time.perf_counter()
        at.run(timeout=180)
    return at, time.perf_counter() - t0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--minutes", type=int, default=15)
    p.add_argument("--lookup", default=None, help="file_id for the Prediction Lookup page")
    args = p.parse_args()
    report: dict[str, object] = {}
    slow = []
    for page in OPS_PAGES + ML_PAGES:
        cold_at, cold = _render(page, args.minutes, args.lookup)
        if cold_at.exception:
            raise SystemExit(f"{page}: {[e.value for e in cold_at.exception]}")
        at, warm = _render(page, args.minutes, args.lookup)  # st.cache_data is warm now
        if warm > BUDGET_SECONDS:
            slow.append(page)
        report[page] = {
            "render_seconds_cold": round(cold, 2),
            "render_seconds_warm": round(warm, 2),
            "sections": [s.value for s in at.subheader],
            "charts": len(at.get("vega_lite_chart")),
            "table_rows": [len(df.value) for df in at.dataframe],
            "metrics": {m.label: m.value for m in at.metric},
            "empty_notes": [i.value for i in at.info],
            "warnings": [w.value for w in at.warning],
        }
    report["pages_over_budget"] = slow
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
