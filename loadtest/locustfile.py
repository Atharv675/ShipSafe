"""Stage 8 load test: POST /v1/score at a fixed rate with real feature records.

    make load-test [USERS=500 DURATION=5m]      (sandbox image, on the compose network)

Each simulated client sends at most one request per second (constant_throughput(1)), so
USERS = target requests per second; if the API answers too slowly the achieved rate drops,
which the report shows. Bodies come from `make load-bodies` and are serialised once up front
so the generator's own CPU does not distort the numbers.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

from locust import FastHttpUser, constant_throughput, task

BODIES_FILE = Path(os.environ.get("SENTINEL_LOAD_BODIES", "/data/loadtest/bodies_Win64.jsonl"))
BODIES = [line for line in BODIES_FILE.read_bytes().splitlines() if line.strip()]
if not BODIES:
    raise SystemExit(f"no bodies in {BODIES_FILE}: run `make load-bodies` first")
HEADERS = {"content-type": "application/json"}
RNG = random.Random(20241122)


class Scorer(FastHttpUser):
    wait_time = constant_throughput(1.0)

    @task
    def score(self) -> None:
        with self.client.post(
            "/v1/score",
            data=RNG.choice(BODIES),
            headers=HEADERS,
            name="/v1/score",
            catch_response=True,
        ) as r:
            if r.status_code != 200:
                r.failure(f"HTTP {r.status_code}")
