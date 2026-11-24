"""Stage 8: render every Operations page against the LIVE database and print what it shows.

    python scripts/dashboard_check.py [--minutes 15]     (make dashboard-check)

Runs each page through Streamlit's AppTest with the real read-only source
(SENTINEL_DASHBOARD_DSN), so it proves the pages draw live data, not just that they load.
"""

from __future__ import annotations

import argparse
import json
import time

from streamlit.testing.v1 import AppTest

SCRIPT = """
import os
from datetime import UTC, datetime
from sentinel.config import (load_alert_rules, load_monitoring_config, load_rollout_config,
                             load_serving_config)
from sentinel.dashboard import data as d
from sentinel.dashboard.ops_pages import Context, {page}
mon = load_monitoring_config()
now = datetime.now(UTC)
ctx = Context(d.PostgresSource(os.environ["SENTINEL_DASHBOARD_DSN"]),
              d.Window.last({minutes}, now, 1), mon, load_alert_rules(),
              load_serving_config().service_name, load_rollout_config().canaries.tenant_prefix,
              now)
{page}(ctx)
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--minutes", type=int, default=15)
    args = p.parse_args()
    report: dict[str, object] = {}
    for page in ("service_health", "model_live", "alerts_page"):
        t0 = time.perf_counter()
        at = AppTest.from_string(SCRIPT.format(page=page, minutes=args.minutes))
        at.run(timeout=120)
        seconds = time.perf_counter() - t0
        if at.exception:
            raise SystemExit(f"{page}: {[e.value for e in at.exception]}")
        report[page] = {
            "render_seconds": round(seconds, 2),
            "sections": [s.value for s in at.subheader],
            "charts": len(at.get("vega_lite_chart")),
            "table_rows": [len(df.value) for df in at.dataframe],
            "metrics": {m.label: m.value for m in at.metric},
            "empty_notes": [i.value for i in at.info],
            "success_notes": [s.value for s in at.success],
        }
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
