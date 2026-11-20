"""Kaggle -> local transfer of verified week files: round trip and every refusal path."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from sentinel.data.pipeline import Layout
from sentinel.data.splits import read_weeks
from sentinel.replay.transfer import export_weeks, import_weeks, local_weeks, sha256_file
from tests.unit.test_train import FT, splits  # noqa: F401  (pytest fixture)


def _export(layout: Layout, out: Path, weeks: list[int] | None = None) -> Path:
    z = out / "replay.zip"
    export_weeks(layout.splits_root, layout.clean_root, FT, weeks or [6, 7], z, {"note": "t"})
    return z


def _rewrite(src: Path, dst: Path, change: dict[str, bytes | None]) -> Path:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            if item.filename in change:
                if change[item.filename] is not None:
                    zout.writestr(item.filename, change[item.filename])  # type: ignore[arg-type]
                continue
            zout.writestr(item, zin.read(item))
        for name, data in change.items():
            if data is not None and name not in zin.namelist():
                zout.writestr(name, data)
    return dst


def test_round_trip_installs_identical_weeks(splits: Layout, tmp_path: Path) -> None:  # noqa: F811
    z = _export(splits, tmp_path)
    dest = tmp_path / "local"
    info = import_weeks(z, dest / "splits", dest / "clean", splits.manifest, dest / "_staging")
    assert info["installed_weeks"] == [6, 7]
    assert info["note"] == "t"
    for w in (6, 7):
        assert sha256_file(dest / "splits" / FT / f"week={w:02d}" / "part.parquet") == (
            sha256_file(splits.week_file(w))
        )
    assert read_weeks(dest / "splits", FT, [6, 7]).equals(
        read_weeks(splits.splits_root, FT, [6, 7])
    )
    assert (dest / "clean" / FT / "never_break.parquet").exists()
    assert local_weeks(dest / "splits", FT) == [6, 7]
    assert not (dest / "_staging").exists()


@pytest.mark.parametrize("attack", ["tampered", "extra_file", "path_escape", "missing_file"])
def test_bad_exports_are_refused_and_nothing_is_installed(
    splits: Layout,  # noqa: F811
    tmp_path: Path,
    attack: str,
) -> None:
    good = _export(splits, tmp_path)
    change: dict[str, bytes | None] = {
        "tampered": {"week=06/part.parquet": b"not the real file"},
        "extra_file": {"run_me.py": b"print(1)"},
        "path_escape": {"../evil.parquet": b"x"},
        "missing_file": {"week=07/part.parquet": None},
    }[attack]
    bad = _rewrite(good, tmp_path / "bad.zip", change)
    dest = tmp_path / "local"
    with pytest.raises(ValueError):
        import_weeks(bad, dest / "splits", dest / "clean", splits.manifest, dest / "_staging")
    assert not (dest / "splits").exists()
    assert not (dest / "_staging").exists()


def test_export_must_match_the_committed_manifest(splits: Layout, tmp_path: Path) -> None:  # noqa: F811
    z = _export(splits, tmp_path)
    other = json.loads(splits.manifest.read_text())
    other["feature_schema_version"] = "something-else"
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(other))
    with pytest.raises(ValueError, match="differs"):
        import_weeks(z, tmp_path / "s", tmp_path / "c", expected, tmp_path / "st")


def test_week_file_must_be_the_one_the_manifest_describes(
    splits: Layout,  # noqa: F811
    tmp_path: Path,
) -> None:
    z = _export(splits, tmp_path)
    # swap week 7's bytes in as week 6 and fix export_info so only the manifest check can catch it
    with zipfile.ZipFile(z) as zf:
        w7 = zf.read("week=07/part.parquet")
        info = json.loads(zf.read("export_info.json"))
    info["sha256"]["week=06/part.parquet"] = info["sha256"]["week=07/part.parquet"]
    bad = _rewrite(
        z,
        tmp_path / "swap.zip",
        {"week=06/part.parquet": w7, "export_info.json": json.dumps(info).encode()},
    )
    with pytest.raises(ValueError, match="manifest describes"):
        import_weeks(bad, tmp_path / "s", tmp_path / "c", splits.manifest, tmp_path / "st")
