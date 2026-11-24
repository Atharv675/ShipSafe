"""Stage 8: score requests against the running API and report what happened.

    python scripts/db_outage_probe.py --phase down   (postgres stopped by db_outage_check.sh)
    python scripts/db_outage_probe.py --phase up      (after postgres is back)

Sends `--n` real request bodies (make load-bodies) under a fresh tenant and prints the HTTP
status counts, /readyz, the API's drop counters and (phase up) how many of the tenant's
lineage rows reached PostgreSQL.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx


def counters(c: httpx.Client) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in c.get("/v1/ops/stats").json():
        if s["metric"] in ("lineage_dropped", "ops_metrics_dropped"):
            key = s["metric"] + ("/" + s["labels"]["reason"] if "reason" in s["labels"] else "")
            out[key] = out.get(key, 0) + s["count"]
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=["down", "up"], required=True)
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--api", default=os.environ.get("SENTINEL_API_URL", "http://api:8000"))
    p.add_argument("--bodies", type=Path, default=Path("/data/loadtest/bodies_Win64.jsonl"))
    args = p.parse_args()
    tenant = f"outage-{args.phase}-{uuid.uuid4().hex[:8]}"
    bodies = [json.loads(b) for b in args.bodies.read_bytes().splitlines()[: args.n]]
    with httpx.Client(base_url=args.api, timeout=30) as c:
        before = counters(c)
        t0 = time.perf_counter()
        status = Counter(
            c.post("/v1/score", json={**b, "tenant_id": tenant}).status_code for b in bodies
        )
        seconds = time.perf_counter() - t0
        ready = c.get("/readyz")
        after = counters(c)
    report: dict[str, object] = {
        "phase": args.phase,
        "tenant": tenant,
        "requests": len(bodies),
        "status_counts": dict(status),
        "seconds": round(seconds, 1),
        "readyz": [ready.status_code, ready.json()],
        "drop_counters_before": before,
        "drop_counters_after": after,
    }
    if args.phase == "up":
        import psycopg

        time.sleep(5)  # > lineage flush interval
        with psycopg.connect(os.environ["SENTINEL_IT_ADMIN_DSN"], connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT count(*) FROM predictions WHERE tenant_id = %s", (tenant,)
            ).fetchone()
        report["lineage_rows_written"] = int(row[0]) if row else 0
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
