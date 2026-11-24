"""Rollout controller CLI (sandbox on the compose network; `make rollout ...`).

    python -m sentinel.rollout.run init                       ring aliases := production
    python -m sentinel.rollout.run start --file-type Win64 --candidate 3 [--demo] [--watch]
    python -m sentinel.rollout.run watch --file-type Win64    tick until COMPLETE/ROLLED_BACK
    python -m sentinel.rollout.run status --file-type Win64
    python -m sentinel.rollout.run kill --reason "..."        POST /admin/rollback, timed
    python -m sentinel.rollout.run demo-reset                 undo a completed DEMO rollout
    python -m sentinel.rollout.run timeline --file-type Win64 [--rollout-id ID] [--out FILE]

Needs SENTINEL_ROLLOUT_DSN (the `sentinel_rollout` login); `kill` needs
SENTINEL_ADMIN_API_KEY. Everything is logged as JSON; `timeline` writes Markdown to --out.
"""

from __future__ import annotations

import argparse
import os
import signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sentinel.config import (
    RING_ORDER,
    Settings,
    load_gate_policy,
    load_monitoring_config,
    load_replay_config,
    load_rollout_config,
    load_serving_config,
    load_train_config,
)
from sentinel.logging import configure_logging, get_logger
from sentinel.rollout.controller import TERMINAL, Controller, RolloutError

log = get_logger(__name__)


RingsReading = tuple[str, dict[str, dict[str, str | None]]]  # (API worker id, serving)


def kill_and_time(
    post: Callable[[], dict[str, Any]],
    rings: Callable[[], RingsReading],
    production: dict[str, str],
    timeout: float,
    poll: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    workers: int = 1,
) -> dict[str, Any]:
    """Call the kill switch, then poll until every ring serves production for every file
    type. Returns the seconds it took (the Stage 6 "< 60 s" check) and what was served.

    Stage 8: the API runs several worker processes and each GET /v1/rings is answered by one
    of them (worker id in a response header). Only the worker that took the kill switch
    reloads at once; the others on their next registry poll. "Restored" therefore needs the
    latest reading of at least `workers` distinct workers, all on production."""
    t0 = clock()
    response = post()
    latest: dict[str, dict[str, dict[str, str | None]]] = {}
    while True:
        worker, serving = rings()
        latest[worker] = serving
        ok = len(latest) >= workers and all(
            s.get(r, {}).get(ft) == v
            for s in latest.values()
            for r in RING_ORDER
            for ft, v in production.items()
        )
        elapsed = clock() - t0
        if ok or elapsed > timeout:
            return {
                "restored": ok,
                "seconds": elapsed,
                "workers_seen": len(latest),
                "serving": serving,
                "response": response,
            }
        sleep(poll)


class NoCanaries:
    """CanarySender that sends nothing (`start --demo --no-canaries`)."""

    def send(self, ring: str, file_type: str, candidate_version: str) -> int:
        return 0


def main() -> None:  # pragma: no cover - drives the running stack (`make rollout ...`)
    import httpx

    from sentinel.rollout.store import (
        ApiCanarySender,
        LineageMeter,
        MlflowRegistry,
        PostgresTransitionStore,
        load_canary_set,
        ops_stats_since,
        timeline_markdown,
    )

    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_rollout_config()
    train = load_train_config()
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    # production back to the source of a completed DEMO rollout
    sub.add_parser("demo-reset").add_argument("--file-type", default=cfg.demo.file_type)
    s = sub.add_parser("start")
    s.add_argument("--file-type", required=True)
    s.add_argument("--candidate", required=True)
    s.add_argument("--demo", action="store_true")
    # demo only: no never-break canaries, to show the paired-scoring signal on its own (a
    # rollout without canaries can roll back but never advance: never-break stays waiting)
    s.add_argument("--no-canaries", action="store_true")
    s.add_argument("--watch", action="store_true")
    for name in ("watch", "status"):
        sub.add_parser(name).add_argument("--file-type", required=True)
    k = sub.add_parser("kill")
    k.add_argument("--reason", required=True)
    k.add_argument("--timeout", type=float, default=60.0)
    tl = sub.add_parser("timeline")
    tl.add_argument("--file-type", required=True)
    tl.add_argument("--rollout-id")
    tl.add_argument("--out", type=Path)
    args = p.parse_args()

    registry = MlflowRegistry(settings.tracking_uri)
    file_types = load_serving_config().file_types
    names = {ft: train.mlflow.model_name(ft) for ft in file_types}
    api = httpx.Client(base_url=cfg.api_url, timeout=cfg.api_timeout_seconds)

    if args.cmd == "init":
        for ft, name in names.items():
            have = registry.aliases(name)
            prod = have[cfg.fallback_alias]
            for r in RING_ORDER:
                if cfg.ring_aliases[r] not in have:
                    registry.set_alias(name, cfg.ring_aliases[r], prod)
            log.info("rollout_init", file_type=ft, aliases=registry.aliases(name))
        return

    if args.cmd == "demo-reset":
        name = names[args.file_type]
        have = registry.aliases(name)
        prod = have[cfg.fallback_alias]
        tags = registry.version_tags(name, prod)
        if tags.get(cfg.demo_tag) != "true":
            raise SystemExit(f"{name}/{prod} is not a demo version: nothing to reset")
        source = tags["source_version"]
        for alias in (cfg.fallback_alias, *(cfg.ring_aliases[r] for r in RING_ORDER)):
            registry.set_alias(name, alias, source)
        log.info("demo_reset", model=name, from_version=prod, to_version=source)
        return

    if args.cmd == "kill":
        key = os.environ["SENTINEL_ADMIN_API_KEY"]
        production = {ft: registry.aliases(n)[cfg.fallback_alias] for ft, n in names.items()}

        def post() -> dict[str, Any]:
            r = api.post(
                "/admin/rollback", json={"reason": args.reason}, headers={"X-API-Key": key}
            )
            r.raise_for_status()
            out: dict[str, Any] = r.json()
            return out

        def read_rings() -> RingsReading:
            r = api.get("/v1/rings")
            r.raise_for_status()
            return r.headers.get("X-Sentinel-Worker", "?"), r.json()

        res = kill_and_time(
            post,
            read_rings,
            production,
            args.timeout,
            0.5,
            workers=load_serving_config().workers,
        )
        log.info("kill_switch_timed", **res)
        if not res["restored"]:
            raise SystemExit(f"rings not restored within {args.timeout} s")
        return

    store = PostgresTransitionStore(os.environ["SENTINEL_ROLLOUT_DSN"])
    ft = args.file_type
    if ft not in names:
        raise SystemExit(f"unknown file type {ft!r}; serving covers {file_types}")

    if args.cmd in ("status", "timeline"):
        latest = store.latest(ft)
        if latest is None:
            log.info("rollout_status", file_type=ft, state=None)
            return
        if args.cmd == "status":
            log.info(
                "rollout_status",
                rollout_id=latest.rollout_id,
                state=latest.to_state,
                since=latest.ts.isoformat(),
                reason=latest.reason,
                aliases=registry.aliases(names[ft]),
                serving=api.get("/v1/rings").json(),
            )
            return
        history = store.history(args.rollout_id or latest.rollout_id)
        md = timeline_markdown(history)
        if args.out:
            args.out.write_text(md, encoding="utf-8")
        log.info(
            "rollout_timeline",
            rollout_id=history[0].rollout_id,
            rows=len(history),
            out=str(args.out),
        )
        return

    # Stage 8: health signals from ops_metrics (the rows the alert worker reads), counted
    # from the moment the ring switched, instead of the API's in-memory totals.
    mon = load_monitoring_config()
    serving_name = load_serving_config().service_name

    def fetch_stats(since: datetime) -> list[dict[str, Any]]:
        return ops_stats_since(
            os.environ["SENTINEL_ROLLOUT_DSN"], serving_name, since, mon.metrics.flush_seconds
        )

    rings = load_replay_config().rings
    no_canaries = args.cmd == "start" and args.no_canaries
    if no_canaries and not args.demo:
        raise SystemExit("--no-canaries is only allowed with --demo")
    controller = Controller(
        store,
        registry,
        LineageMeter(os.environ["SENTINEL_ROLLOUT_DSN"], cfg, fetch_stats),
        NoCanaries()
        if no_canaries
        else ApiCanarySender(
            api,
            cfg,
            rings,
            lambda f: load_canary_set(settings.splits_dir, settings.clean_dir, f),
            workers=cfg.canaries.send_workers,
        ),
        cfg,
        load_gate_policy().approved_alias,
    )
    if args.cmd == "start":
        try:
            controller.start(
                ft,
                names[ft],
                str(args.candidate),
                datetime.now(UTC),
                args.demo,
                note="never-break canaries OFF for this demo (paired-scoring signal alone)"
                if no_canaries
                else "",
            )
        except RolloutError as exc:
            raise SystemExit(str(exc)) from exc
        if not args.watch:
            return

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        t, verdict = controller.tick(ft)
        if t.to_state in TERMINAL:
            log.info("rollout_finished", rollout_id=t.rollout_id, state=t.to_state, reason=t.reason)
            return
        if verdict is not None and verdict.action != "hold":
            continue  # moved: measure the new ring straight away
        stop.wait(cfg.check_seconds)


if __name__ == "__main__":  # pragma: no cover
    main()
