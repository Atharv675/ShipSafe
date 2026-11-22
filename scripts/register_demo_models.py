"""Stage 6 (local, sandbox): register the two rollout-demo versions of production.

    python scripts/register_demo_models.py [--file-type Win32] [--kinds good bad]

Both are new versions of sentinel-<demo type> built from the production version's own
booster, calibrator and metadata (no retraining), tagged demo=true so only they can use
`rollout start --demo` (the release gate is skipped for them, and the rollout says so):
  GOOD: an identical copy of production;
  BAD:  the same model with the raw threshold set to `demo.bad_threshold_raw` (0.5).
No alias is set; production is not touched.
"""

from __future__ import annotations

import argparse

from mlflow import MlflowClient

from sentinel.config import Settings, load_rollout_config, load_train_config
from sentinel.logging import configure_logging
from sentinel.models.registry import register_derived_version


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_rollout_config()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file-type", default=cfg.demo.file_type)
    p.add_argument("--kinds", nargs="+", choices=["good", "bad"], default=["good", "bad"])
    args = p.parse_args()
    name = load_train_config().mlflow.model_name(args.file_type)
    client = MlflowClient(tracking_uri=settings.tracking_uri, registry_uri=settings.tracking_uri)
    source = str(client.get_model_version_by_alias(name, cfg.demo.source_alias).version)
    work = settings.data_dir / "models" / args.file_type / "rollout_demo"
    versions: dict[str, str] = {}
    thresholds = {"good": None, "bad": cfg.demo.bad_threshold_raw}
    for kind in args.kinds:
        thr = thresholds[kind]
        versions[kind] = register_derived_version(
            settings.tracking_uri,
            name,
            source,
            work / kind,
            settings.mlflow_dir / "artifacts",  # served artifacts must live in the registry
            {cfg.demo_tag: "true", "demo_kind": kind, "kind": "rollout-demo"},
            threshold_raw=thr,
        )
    shown = ", ".join(f"demo {k.upper()} = v{v}" for k, v in versions.items())
    print(f"{name}: production v{source}; {shown}")  # scripts may print; src/ logs only


if __name__ == "__main__":
    main()
