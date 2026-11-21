"""Stage 4 checks (sandbox on the compose network): live vs shadow coverage of replayed weeks.

    python scripts/replay_counts.py snapshot --weeks 53 54 [--file-type Win32]
    python scripts/replay_counts.py wait --weeks 53 54 --tables live shadow --timeout 900

For the given weeks it reads the expected event ids from the local week files and counts,
per table, how many of them have a row, how many rows exist in total (duplicates would make
rows > events) and which model versions wrote them. `wait` exits non-zero on timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import psycopg
import pyarrow.parquet as pq

from sentinel.config import Settings, load_replay_config
from sentinel.data.schema import ID_COLUMN
from sentinel.replay.events import make_event_id

TABLES = {"live": "predictions", "shadow": "shadow_predictions"}


def expected_ids(file_type: str, weeks: list[int]) -> list[str]:
    settings = Settings()
    ids: list[str] = []
    for w in weeks:
        t = pq.read_table(
            settings.splits_dir / file_type / f"week={w:02d}" / "part.parquet",
            columns=[ID_COLUMN],
        )
        ids += [make_event_id(file_type, str(v)) for v in t.column(ID_COLUMN).to_pylist()]
    return ids


def counts(conn: psycopg.Connection[Any], ids: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"expected_events": len(ids)}
    for name, table in TABLES.items():
        row = conn.execute(
            f"SELECT count(DISTINCT event_id), count(*), "  # table name from the fixed TABLES map
            f"coalesce(array_agg(DISTINCT model_version), '{{}}') "
            f"FROM {table} WHERE event_id = ANY(%s)",
            (ids,),
        ).fetchone()
        assert row is not None
        out[name] = {"events": row[0], "rows": row[1], "model_versions": sorted(row[2])}
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["snapshot", "wait"])
    file_types = load_replay_config().file_types
    p.add_argument("--weeks", type=int, nargs="+", required=True)
    p.add_argument("--file-type", default=file_types[0], choices=file_types)
    p.add_argument("--tables", nargs="+", choices=list(TABLES), default=list(TABLES))
    p.add_argument("--timeout", type=float, default=900)
    args = p.parse_args()
    ids = expected_ids(args.file_type, args.weeks)
    deadline = time.monotonic() + args.timeout
    with psycopg.connect(os.environ["SENTINEL_IT_ADMIN_DSN"], autocommit=True) as conn:
        while True:
            c = counts(conn, ids)
            done = all(c[t]["events"] == len(ids) for t in args.tables)
            if args.mode == "snapshot" or done or time.monotonic() > deadline:
                break
            time.sleep(2)
    c["file_type"], c["weeks"] = args.file_type, args.weeks
    print(json.dumps(c))
    if args.mode == "wait" and not done:
        sys.exit(f"timeout: not every event of weeks {args.weeks} reached {args.tables}")


if __name__ == "__main__":
    main()
