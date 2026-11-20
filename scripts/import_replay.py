"""Stage 4 (local, sandbox): verify and install a Kaggle replay export.

    python scripts/import_replay.py --zip /in/replay.zip

Installs week files into $SENTINEL_DATA_DIR/splits/<type>/ and never_break.parquet into
$SENTINEL_DATA_DIR/clean/<type>/ only after every check in sentinel.replay.transfer passes.
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

from sentinel.config import REPO_ROOT, Settings
from sentinel.logging import configure_logging
from sentinel.replay.transfer import import_weeks


def main() -> None:
    settings = Settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--zip", type=Path, required=True)
    args = p.parse_args()
    configure_logging(settings.log_level)
    with zipfile.ZipFile(args.zip) as zf:
        ft = json.loads(zf.read("export_info.json"))["file_type"]
    info = import_weeks(
        args.zip,
        settings.splits_dir,
        settings.clean_dir,
        REPO_ROOT / "docs" / "cleaning" / f"{ft}.json",
        settings.data_dir / "_incoming_replay",
    )
    w = info["installed_weeks"]
    print(f"{ft}: verified and installed weeks {w[0]}-{w[-1]} ({len(w)} weeks) + never-break set")


if __name__ == "__main__":
    main()
