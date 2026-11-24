"""Stage 8 ops_metrics retention (daily, in the alert worker, as sentinel_monitor).

1. Rows finer than `downsample_bucket_seconds` and older than `downsample_after_hours`
   (whole target buckets only) are moved, in one statement, into one row per
   (1-minute bucket, service, metric, labels):
     count, sum      exact sums;
     p50             count-weighted mean of the 10-second p50s (an approximation);
     p95, p99        the maximum of the 10-second values (an upper bound: a latency spike
                     inside the minute stays visible).
   Percentiles of the merged samples cannot be recovered from summaries; see DECISIONS.md.
2. Rows older than `delete_after_days` are deleted.
"""

from __future__ import annotations

from dataclasses import dataclass

from sentinel.config import RetentionConfig

DOWNSAMPLE_SQL = """
WITH moved AS (
    DELETE FROM ops_metrics
    WHERE bucket_seconds < %(bucket)s
      AND ts_bucket < date_bin(%(bucket)s * interval '1 second',
                               now() - %(after_hours)s * interval '1 hour',
                               TIMESTAMPTZ 'epoch')
    RETURNING ts_bucket, service, metric, labels, count, sum, p50, p95, p99
)
INSERT INTO ops_metrics (ts_bucket, bucket_seconds, service, metric, labels, count, sum,
                         p50, p95, p99)
SELECT date_bin(%(bucket)s * interval '1 second', ts_bucket, TIMESTAMPTZ 'epoch'),
       %(bucket)s, service, metric, labels, sum(count), sum(sum),
       sum(p50 * count) FILTER (WHERE p50 IS NOT NULL)
           / NULLIF(sum(count) FILTER (WHERE p50 IS NOT NULL), 0),
       max(p95), max(p99)
FROM moved
GROUP BY 1, service, metric, labels
"""

DELETE_SQL = "DELETE FROM ops_metrics WHERE ts_bucket < now() - %(days)s * interval '1 day'"


@dataclass(frozen=True)
class RetentionResult:
    downsampled_rows_written: int
    deleted_rows: int


def run_retention(conn: object, cfg: RetentionConfig) -> RetentionResult:
    """Both steps in one transaction on an open psycopg connection."""
    import psycopg

    assert isinstance(conn, psycopg.Connection)
    params = {
        "bucket": cfg.downsample_bucket_seconds,
        "after_hours": cfg.downsample_after_hours,
        "days": cfg.delete_after_days,
    }
    with conn.transaction():
        down = conn.execute(DOWNSAMPLE_SQL, params).rowcount
        deleted = conn.execute(DELETE_SQL, params).rowcount
    return RetentionResult(max(down, 0), max(deleted, 0))
