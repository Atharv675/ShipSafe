"""Stage 7b: per-file metadata the backtest needs and the Stage 7 export lacks.

Kaggle side (`collect_metadata`, `write_metadata_zip`): read the official EMBER2024 raw
records (already SHA-256 verified by scripts/download_ember.py) and keep, per file:
sha256, project week (numbered exactly as Stage 1 cleaning numbers them: train weeks then
test weeks, in time order, from 1), label, family, family_confidence, and two Authenticode
flags (num_certs, self_signed). Nothing else: no feature vectors, no capa output.

EMBER2024 records carry no signer identity (the Authenticode block is 8 numbers), so a
"top signers" slice is impossible; signature status (unsigned / signed / self-signed) is
the closest honest substitute.

Local side (`import_metadata_zip`): allow-listed names, sha256 check, then unpack.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd

from sentinel.config import DatasetSpec
from sentinel.data.ember import iter_member_records
from sentinel.data.pipeline import ordered_weeks
from sentinel.logging import get_logger

log = get_logger(__name__)

META_FILE = "metadata.parquet"
INFO_FILE = "metadata_info.json"
HASH_FILE = "sha256.json"
ALLOWED = frozenset({META_FILE, INFO_FILE, HASH_FILE})
COLUMNS = ["sha256", "week", "label", "family", "family_confidence", "num_certs", "self_signed"]


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def metadata_row(rec: dict[str, Any], week: int) -> dict[str, Any] | None:
    """One output row from one raw record; None if it has no sha256 (cleaning drops it)."""
    sha = rec.get("sha256")
    if not sha:
        return None
    auth = rec.get("authenticode") or {}
    fam = rec.get("family")
    return {
        "sha256": str(sha),
        "week": week,
        "label": rec.get("label"),
        "family": None if fam in (None, "") else str(fam),
        "family_confidence": _num(rec.get("family_confidence")),
        "num_certs": _num(auth.get("num_certs")),
        "self_signed": _num(auth.get("self_signed")),
    }


def collect_metadata(raw_dir: Path, file_type: str, ds: DatasetSpec) -> pd.DataFrame:
    """All weeks of one file type. Raw files repeat every record (Stage 0); the first
    occurrence per (sha256, week) is kept."""
    rows: list[dict[str, Any]] = []
    for week, (zip_path, wf) in enumerate(ordered_weeks(raw_dir, file_type, ds), start=1):
        seen: set[str] = set()
        n = 0
        for rec in iter_member_records(zip_path, wf.name):
            row = metadata_row(rec, week)
            if row is None or row["sha256"] in seen:
                continue
            seen.add(row["sha256"])
            rows.append(row)
            n += 1
        log.info("metadata_week", week=week, member=wf.name, rows=n)
    df = pd.DataFrame(rows, columns=COLUMNS)
    return df


def write_metadata_zip(df: pd.DataFrame, info: dict[str, Any], out_zip: Path) -> str:
    """Zip metadata + info + their sha256s; returns the zip's sha256."""
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    files = {
        META_FILE: buf.getvalue(),
        INFO_FILE: (json.dumps(info, indent=1, sort_keys=True) + "\n").encode("utf-8"),
    }
    hashes = {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}
    files[HASH_FILE] = (json.dumps(hashes, indent=1, sort_keys=True) + "\n").encode("utf-8")
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), files[name])
    return hashlib.sha256(out_zip.read_bytes()).hexdigest()


def import_metadata_zip(zip_path: Path, dest: Path) -> pd.DataFrame:
    """Verify (allow-list, sha256 of every member) and unpack; returns the metadata."""
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        if names != ALLOWED:
            raise ValueError(f"unexpected members in {zip_path.name}: {sorted(names ^ ALLOWED)}")
        data = {n: zf.read(n) for n in names}
    hashes = json.loads(data[HASH_FILE])
    for name in (META_FILE, INFO_FILE):
        if hashlib.sha256(data[name]).hexdigest() != hashes.get(name):
            raise ValueError(f"sha256 mismatch for {name}")
    dest.mkdir(parents=True, exist_ok=True)
    for name in (META_FILE, INFO_FILE, HASH_FILE):
        (dest / name).write_bytes(data[name])
    df = pd.read_parquet(dest / META_FILE)
    if list(df.columns) != COLUMNS:
        raise ValueError(f"unexpected columns {list(df.columns)}")
    return df
