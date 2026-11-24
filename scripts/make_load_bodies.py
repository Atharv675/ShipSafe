"""Stage 8: request bodies for the load test from real EMBER2024 feature records (no binaries).

    python scripts/make_load_bodies.py --file-type Win64 --week 53 --n 2000

Writes $SENTINEL_DATA_DIR/loadtest/bodies_<type>.jsonl: one POST /v1/score body per line,
seeded random sample of the week's files, spread over `--tenants` load-test tenants (prefix
`loadtest-`, so their lineage rows are easy to tell apart; no event_id, so the gate and the
replay counts never pair them with stream events).
"""

from __future__ import annotations

import argparse

import numpy as np
import orjson

from sentinel.config import Settings, load_replay_config
from sentinel.replay.producer import load_events

TENANT_PREFIX = "loadtest-"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file-type", default="Win64")
    p.add_argument("--week", type=int, default=53)
    p.add_argument("--n", type=int, default=2000)
    p.add_argument("--tenants", type=int, default=100)
    p.add_argument("--seed", type=int, default=20241122)
    args = p.parse_args()
    settings = Settings()
    events = list(
        load_events(settings.splits_dir, args.file_type, [args.week], load_replay_config())
    )
    rng = np.random.default_rng(args.seed)
    pick = rng.choice(len(events), size=min(args.n, len(events)), replace=False)
    out = settings.data_dir / "loadtest" / f"bodies_{args.file_type}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        for k, i in enumerate(sorted(pick)):
            ev = events[int(i)]
            f.write(
                orjson.dumps(
                    {
                        "file_id": ev.file_id,
                        "tenant_id": f"{TENANT_PREFIX}{k % args.tenants:03d}",
                        "features": ev.features.tolist(),
                        "feature_schema_version": ev.feature_schema_version,
                    }
                )
                + b"\n"
            )
    print(
        f"{out}: {len(pick)} bodies from week {args.week} ({len(events)} files), "
        f"{args.tenants} tenants, {out.stat().st_size / 1e6:.1f} MB"
    )


if __name__ == "__main__":
    main()
