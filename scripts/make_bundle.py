"""Build dist/sentinel_gate_code.zip: the code needed to run on Kaggle/Colab.

Allow-list only (no data, no .env, no .git, no caches), and prints every file it includes
so the owner can review what would be uploaded.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from sentinel.config import REPO_ROOT
from sentinel.provenance import BUNDLE_INFO, git_commit

INCLUDE_FILES = ["pyproject.toml", "uv.lock"]
INCLUDE_DIRS = ["src", "scripts", "configs"]
EXCLUDE_PARTS = {"__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
OUT = REPO_ROOT / "dist" / "sentinel_gate_code.zip"


def collect() -> list[Path]:
    files = [REPO_ROOT / f for f in INCLUDE_FILES]
    for d in INCLUDE_DIRS:
        files += [
            p
            for p in sorted((REPO_ROOT / d).rglob("*"))
            if p.is_file() and not (EXCLUDE_PARTS & set(p.parts)) and p.suffix != ".pyc"
        ]
    for p in files:
        if p.name.startswith(".env"):
            raise SystemExit(f"refusing to bundle secret-like file {p}")
    return files


def main() -> None:
    files = collect()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    info_json = json.dumps(
        {
            "git_commit": git_commit(REPO_ROOT),
            "files": {
                p.relative_to(REPO_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in files
            },
        },
        indent=1,
        sort_keys=True,
    )
    with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo(BUNDLE_INFO, date_time=(2026, 1, 1, 0, 0, 0)), info_json)
        print("  +", BUNDLE_INFO)
        for p in files:
            # fixed timestamp => byte-identical zip for identical code
            info = zipfile.ZipInfo(
                p.relative_to(REPO_ROOT).as_posix(), date_time=(2026, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, p.read_bytes())
            print("  +", info.filename)
    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
    print(f"wrote {OUT.relative_to(REPO_ROOT)} ({len(files)} files) sha256={digest}")


if __name__ == "__main__":
    main()
