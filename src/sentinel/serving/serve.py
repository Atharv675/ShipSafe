"""Start the scoring API with the worker count from configs/serving.yaml (Stage 8).

    python -m sentinel.serving.serve        (compose service `api`)

Scoring is CPU-bound Python (JSON, validation, LightGBM), so one process is limited by one
GIL; `workers` processes share the port. Each loads its own models and writes its own
ops_metrics rows (aggregated by every reader).
"""

from __future__ import annotations

import uvicorn

from sentinel.config import load_serving_config


def main() -> None:  # pragma: no cover - process entry point
    cfg = load_serving_config()
    uvicorn.run(
        "sentinel.serving.app:build_app",
        factory=True,
        host="0.0.0.0",  # inside the container; compose publishes it on 127.0.0.1 only
        port=8000,
        workers=cfg.workers,
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
