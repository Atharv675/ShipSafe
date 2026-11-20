"""Move verified weekly splits from a Kaggle rebuild to the local data directory.

Export (Kaggle): manifest.json + chosen week files + never_break.parquet + export_info.json
(sha256 of every file). Import (local): accept ONLY those names, then verify
1. every file's sha256 equals export_info.json,
2. every week file's sha256 equals the rebuilt manifest's entry for that week,
3. the rebuilt manifest has the same CONTENT as the committed docs/cleaning/<type>.json.
Anything else stops the import; nothing partial is left in place.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from sentinel.data.splits import compare_manifests, load_manifest
from sentinel.logging import get_logger

log = get_logger(__name__)

EXPORT_INFO = "export_info.json"
MANIFEST = "manifest.json"
NEVER_BREAK = "never_break.parquet"
_WEEK_RE = re.compile(r"^week=(\d{2})/part\.parquet$")
_CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def export_weeks(
    splits_root: Path,
    clean_root: Path,
    file_type: str,
    weeks: list[int],
    out_zip: Path,
    extra_info: dict[str, Any],
) -> dict[str, Any]:
    files = {MANIFEST: splits_root / file_type / MANIFEST}
    for w in sorted(weeks):
        files[f"week={w:02d}/part.parquet"] = (
            splits_root / file_type / f"week={w:02d}" / ("part.parquet")
        )
    files[NEVER_BREAK] = clean_root / file_type / NEVER_BREAK
    info = {
        "file_type": file_type,
        "weeks": sorted(weeks),
        **extra_info,
        "sha256": {name: sha256_file(path) for name, path in files.items()},
    }
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_STORED) as zf:  # parquet: stored
        zf.writestr(EXPORT_INFO, json.dumps(info, indent=1) + "\n")
        for name, path in files.items():
            zf.write(path, name)
    return info


def _allowed(name: str) -> bool:
    return name in {EXPORT_INFO, MANIFEST, NEVER_BREAK} or bool(_WEEK_RE.match(name))


def import_weeks(
    zip_path: Path,
    splits_root: Path,
    clean_root: Path,
    expected_manifest: Path,
    staging: Path,
) -> dict[str, Any]:
    """Verify and install an export. Returns export_info (+ 'installed_weeks')."""
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for item in zf.infolist():
                if item.is_dir():
                    continue
                name = item.filename
                if not _allowed(name) or ".." in PurePosixPath(name).parts:
                    raise ValueError(f"unexpected file in export: {name}")
                dest = staging / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(item) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst, _CHUNK)
        info: dict[str, Any] = json.loads((staging / EXPORT_INFO).read_text(encoding="utf-8"))
        ft = str(info["file_type"])
        listed = dict(info["sha256"])
        present = {
            p.relative_to(staging).as_posix()
            for p in staging.rglob("*")
            if p.is_file() and p.name != EXPORT_INFO
        }
        if present != set(listed):
            raise ValueError(f"files do not match export_info: {sorted(present ^ set(listed))}")
        for name, digest in listed.items():
            if sha256_file(staging / name) != digest:
                raise ValueError(f"{name}: sha256 does not match export_info")
        manifest = json.loads((staging / MANIFEST).read_text(encoding="utf-8"))
        if manifest.get("file_type") != ft:
            raise ValueError("manifest file_type does not match export_info")
        by_week = {int(w["week"]): w for w in manifest["weeks"]}
        weeks = sorted(int(m.group(1)) for n in listed if (m := _WEEK_RE.match(n)))
        for w in weeks:
            if listed[f"week={w:02d}/part.parquet"] != by_week[w]["sha256"]:
                raise ValueError(f"week {w}: file is not the one the manifest describes")
        diffs = compare_manifests(json.loads(expected_manifest.read_text("utf-8")), manifest)
        if diffs:
            raise ValueError(f"export differs from {expected_manifest.name}: {diffs}")

        # verified: install (replace only what the export carries)
        target = splits_root / ft
        target.mkdir(parents=True, exist_ok=True)
        for w in weeks:
            d = target / f"week={w:02d}"
            d.mkdir(exist_ok=True)
            shutil.move(str(staging / f"week={w:02d}" / "part.parquet"), d / "part.parquet")
        shutil.move(str(staging / MANIFEST), target / MANIFEST)
        (clean_root / ft).mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging / NEVER_BREAK), clean_root / ft / NEVER_BREAK)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    info["installed_weeks"] = weeks
    log.info("replay_weeks_imported", file_type=ft, weeks=weeks)
    return info


def local_weeks(splits_root: Path, file_type: str) -> list[int]:
    """Weeks whose Parquet file is present locally (a partial import is normal)."""
    manifest = load_manifest(splits_root, file_type)
    return [
        int(w["week"])
        for w in manifest["weeks"]
        if (splits_root / file_type / f"week={int(w['week']):02d}" / "part.parquet").exists()
    ]
