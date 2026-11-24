"""Stage 8: summarise load-test runs into docs/LOAD_TEST.md (real numbers only).

    python scripts/load_report.py bench                      # metrics-recorder cost
    python scripts/load_report.py run --run /data/loadtest/<name> --label "before"
    python scripts/load_report.py render --out docs/LOAD_TEST.md

`run` reads Locust's CSVs (client side: what a caller saw, network + JSON included) and the
API's own ops_metrics rows for the same window (server side: handler time, per 10-second
bucket), and stores a JSON summary next to the CSVs. `render` writes the Markdown from every
stored summary plus the bench, in run order.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from sentinel.config import Settings, load_alert_rules, load_monitoring_config
from sentinel.monitoring.metrics import MetricsRecorder

ROOT = Settings().data_dir / "loadtest"
BENCH_FILE = ROOT / "bench.json"


def bench(iterations: int = 200_000) -> dict[str, Any]:
    """What the API's recorder does per request (2 counters + 1 latency sample), timed with
    a writer thread draining every 10 ms (far more often than the real 10 s) for contention."""
    m = MetricsRecorder("api", 10_000, 20_000)
    stop = threading.Event()

    def drainer() -> None:
        while not stop.wait(0.01):
            m.drain()

    th = threading.Thread(target=drainer, daemon=True)
    th.start()
    labels = {"model_version": "sentinel-win64/1", "status": "200"}
    lat = {"model_version": "sentinel-win64/1"}
    per_call = np.empty(iterations)
    for i in range(iterations):
        t0 = time.perf_counter_ns()
        m.inc("requests", labels)
        m.inc("override_changed_verdict", {"tenant_id": "t"})
        m.observe("latency_ms", 1.0 + (i % 7), lat)
        per_call[i] = time.perf_counter_ns() - t0
    stop.set()
    th.join()
    us = per_call / 1000
    return {
        "iterations": iterations,
        "mean_us": float(us.mean()),
        "p50_us": float(np.percentile(us, 50)),
        "p99_us": float(np.percentile(us, 99)),
        "max_us": float(us.max()),
        "python": platform.python_version(),
        "cpus_visible": os.cpu_count(),
    }


def _locust_rows(run: Path) -> tuple[dict[str, str], list[dict[str, str]]]:
    with (run / "locust_stats.csv").open() as f:
        stats = list(csv.DictReader(f))
    with (run / "locust_stats_history.csv").open() as f:
        hist = list(csv.DictReader(f))
    agg = next(r for r in stats if r["Name"] == "Aggregated")
    return agg, hist


def summarise(run: Path, label: str, users: int) -> dict[str, Any]:
    import psycopg

    agg, hist = _locust_rows(run)
    ts = [int(r["Timestamp"]) for r in hist if r["Timestamp"]]
    start, end = datetime.fromtimestamp(min(ts), UTC), datetime.fromtimestamp(max(ts), UTC)
    # steady state: skip the ramp-up (first 30 s) and the last bucket
    steady_from = start + timedelta(seconds=30)
    rps_steady = [
        float(r["Requests/s"])
        for r in hist
        if r["Timestamp"] and datetime.fromtimestamp(int(r["Timestamp"]), UTC) >= steady_from
    ]
    with psycopg.connect(os.environ["SENTINEL_DASHBOARD_DSN"], connect_timeout=5) as conn:
        rows = conn.execute(
            "SELECT ts_bucket, metric, labels, count, p50, p95, p99 FROM ops_metrics "
            "WHERE service = 'api' AND ts_bucket >= %s AND ts_bucket < %s "
            "AND metric IN ('latency_ms', 'requests', 'errors', 'lineage_dropped', "
            "'ops_metrics_dropped', 'metrics_samples_dropped')",
            (steady_from, end),
        ).fetchall()
        alerts = conn.execute(
            "SELECT ts, rule, state, message FROM alerts WHERE ts >= %s AND ts < %s + "
            "interval '6 minutes' ORDER BY id",
            (start, end),
        ).fetchall()
    lat = [r for r in rows if r[1] == "latency_ms"]
    by_bucket: dict[Any, list[tuple[int, float, float]]] = {}
    for r in lat:
        by_bucket.setdefault(r[0], []).append((int(r[3]), float(r[4]), float(r[6])))
    bucket_p99 = [max(p99 for _, _, p99 in v) for v in by_bucket.values()]
    bucket_p50 = [max(p50 for _, p50, _ in v) for v in by_bucket.values()]
    limit = next(r.above for r in load_alert_rules().rules if r.name == "api_p99_latency")
    counts = {
        m: sum(int(r[3]) for r in rows if r[1] == m)
        for m in (
            "requests",
            "errors",
            "lineage_dropped",
            "ops_metrics_dropped",
            "metrics_samples_dropped",
        )
    }
    summary = {
        "label": label,
        "run": run.name,
        "users": users,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "client": {
            "requests": int(agg["Request Count"]),
            "failures": int(agg["Failure Count"]),
            "rps_mean_steady": statistics.fmean(rps_steady) if rps_steady else None,
            "p50_ms": float(agg["50%"]),
            "p95_ms": float(agg["95%"]),
            "p99_ms": float(agg["99%"]),
            "max_ms": float(agg["Max Response Time"]),
        },
        "server": {
            "buckets": len(bucket_p99),
            "p50_ms_median_bucket": statistics.median(bucket_p50) if bucket_p50 else None,
            "p99_ms_median_bucket": statistics.median(bucket_p99) if bucket_p99 else None,
            "p99_ms_worst_bucket": max(bucket_p99) if bucket_p99 else None,
            "share_buckets_p99_over_limit": (
                sum(v > limit for v in bucket_p99) / len(bucket_p99) if bucket_p99 else None
            ),
            "p99_limit_ms": limit,
            **counts,
        },
        "alerts": [[str(a[0]), a[1], a[2], a[3]] for a in alerts],
    }
    (run / "summary.json").write_text(json.dumps(summary, indent=1), "utf-8")
    return summary


def _f(v: Any, nd: int = 1) -> str:
    return "n/a" if v is None else f"{v:,.{nd}f}"


def render(out: Path) -> None:
    runs = sorted(
        (json.loads(p.read_text("utf-8")) for p in ROOT.glob("*/summary.json")),
        key=lambda s: s["start"],
    )
    b = json.loads(BENCH_FILE.read_text("utf-8")) if BENCH_FILE.exists() else None
    mon = load_monitoring_config()
    lines = [
        "# Load test (Stage 8)",
        "",
        "Generated by `scripts/load_report.py` from Locust CSVs and the API's own `ops_metrics`"
        " rows. Target: 500 requests/s for 5 minutes against `POST /v1/score` on the local"
        " compose stack, real Win64 test-week feature records (`make load-bodies`), all rings"
        " on production (no paired scoring).",
        "",
        "- **Client side** (Locust): what a caller saw, including HTTP and JSON (a Win64 body is"
        " ~2,500 numbers).",
        f"- **Server side** (`ops_metrics`, {mon.metrics.flush_seconds:g}-second buckets): the"
        " API's handler time, which the p99 alert watches. Steady state only (first 30 s of"
        " ramp-up skipped).",
        "",
        "| Run | Users | Requests | Failures | Achieved req/s | Client p50 / p95 / p99 (ms) |"
        " Server p50 / p99, median bucket (ms) | Server p99 worst bucket (ms) |"
        " Buckets with p99 > limit |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in runs:
        c, v = s["client"], s["server"]
        share = v["share_buckets_p99_over_limit"]
        lines.append(
            f"| {s['label']} | {s['users']} | {c['requests']:,} | {c['failures']:,} | "
            f"{_f(c['rps_mean_steady'])} | {_f(c['p50_ms'], 0)} / {_f(c['p95_ms'], 0)} / "
            f"{_f(c['p99_ms'], 0)} | {_f(v['p50_ms_median_bucket'], 2)} / "
            f"{_f(v['p99_ms_median_bucket'], 2)} | {_f(v['p99_ms_worst_bucket'], 2)} | "
            f"{'n/a' if share is None else f'{100 * share:.0f}%'} |"
        )
    lines += [
        "",
        "Server-side counters during each run (steady state):",
        "",
        "| Run | Requests (server) | 5xx | Lineage rows dropped | ops_metrics rows dropped |"
        " Latency samples dropped |",
        "|---|---|---|---|---|---|",
    ]
    for s in runs:
        v = s["server"]
        lines.append(
            f"| {s['label']} | {v['requests']:,} | {v['errors']:,} | {v['lineage_dropped']:,} | "
            f"{v['ops_metrics_dropped']:,} | {v['metrics_samples_dropped']:,} |"
        )
    lines += ["", "Alerts recorded during each run (and 6 minutes after):", ""]
    for s in runs:
        got = "; ".join(f"{a[2]} {a[1]} at {a[0][11:19]}" for a in s["alerts"]) or "none"
        lines.append(f"- {s['label']}: {got}")
    if b:
        lines += [
            "",
            "## Metrics recorder cost",
            "",
            f"Per request the API makes 2 counter updates and 1 latency sample. Timed over "
            f"{b['iterations']:,} requests with a writer thread draining every 10 ms (1,000x more"
            f" often than in production) for lock contention: mean **{b['mean_us']:.2f} µs**, "
            f"p50 {b['p50_us']:.2f} µs, p99 {b['p99_us']:.2f} µs, max {b['max_us']:.0f} µs per "
            f"request (Python {b['python']}). Budget: under 1 ms (1,000 µs).",
        ]
    lines += ["", "<!-- notes:begin -->", "<!-- notes:end -->", ""]
    text = "\n".join(lines)
    if out.exists():  # keep hand-written notes between the markers
        old = out.read_text("utf-8")
        if "<!-- notes:begin -->" in old:
            notes = old.split("<!-- notes:begin -->", 1)[1].split("<!-- notes:end -->", 1)[0]
            text = text.replace(
                "<!-- notes:begin -->\n<!-- notes:end -->",
                f"<!-- notes:begin -->{notes}<!-- notes:end -->",
            )
    out.write_text(text, "utf-8")
    print(f"wrote {out} ({len(runs)} runs)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bench")
    r = sub.add_parser("run")
    r.add_argument("--run", type=Path, required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--users", type=int, required=True)
    rd = sub.add_parser("render")
    rd.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    if args.cmd == "bench":
        res = bench()
        BENCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        BENCH_FILE.write_text(json.dumps(res, indent=1), "utf-8")
        print(json.dumps(res, indent=1))
    elif args.cmd == "run":
        print(json.dumps(summarise(args.run, args.label, args.users), indent=1))
    else:
        render(args.out)


if __name__ == "__main__":
    main()
