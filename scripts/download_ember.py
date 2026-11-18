"""Download EMBER2024 feature-record zips, following the official thrember instructions.

Official source: https://github.com/FutureComputing4AI/EMBER2024 (README + src/thrember/download.py).
thrember.download_dataset() calls
    hf_hub_download(repo_id="joyce8/EMBER2024", filename=f"{file_type}_{split}.zip",
                    repo_type="dataset")
and then extracts the zip. We use the same call, but keep the zips (our readers stream from
them) and verify every file's SHA-256 against the LFS hash published by HuggingFace.

These are JSON feature records only. No binaries are downloaded or executed.

Usage:
    uv run python scripts/download_ember.py --file-types PDF Win64 --splits train test
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

from sentinel.config import Settings, load_data_config
from sentinel.logging import configure_logging, get_logger

FORBIDDEN_REPOS = {"joyce8/EMBER2024-capa"}  # contains raw function bytes from malware


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    cfg = load_data_config()
    settings = Settings()
    configure_logging(settings.log_level)
    log = get_logger("download_ember")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--file-types", nargs="+", default=cfg.dataset.file_types,
                    choices=cfg.dataset.file_types)
    ap.add_argument("--splits", nargs="+", default=cfg.dataset.splits, choices=cfg.dataset.splits)
    ap.add_argument("--dest", type=Path, default=settings.raw_dir)
    args = ap.parse_args()

    repo = cfg.dataset.hf_repo_id
    if repo in FORBIDDEN_REPOS:
        raise SystemExit(f"Refusing to download {repo}: it contains raw malware bytes.")
    dest: Path = args.dest
    dest.mkdir(parents=True, exist_ok=True)

    names = [cfg.dataset.zip_name(ft, sp) for ft in args.file_types for sp in args.splits]
    infos = HfApi().get_paths_info(repo, names, repo_type=cfg.dataset.hf_repo_type)
    expected = {i.path: (i.lfs.sha256 if i.lfs else None, i.size) for i in infos}

    manifest_path = dest / "download_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    for name in names:
        if name not in expected:
            raise SystemExit(f"{name} not found in {repo}; refusing to guess.")
        want_sha, size = expected[name]
        target = dest / name
        if target.exists() and manifest.get(name, {}).get("sha256") == want_sha:
            log.info("already_present", file=name)
            continue
        log.info("downloading", file=name, bytes=size)
        # local_dir writes straight into dest (no duplicate copy in the global HF cache).
        target = Path(hf_hub_download(repo_id=repo, filename=name,
                                      repo_type=cfg.dataset.hf_repo_type, local_dir=dest))
        got = sha256_of(target)
        if want_sha and got != want_sha:
            target.unlink()
            raise SystemExit(f"SHA-256 mismatch for {name}: got {got}, want {want_sha}")
        manifest[name] = {"sha256": got, "bytes": target.stat().st_size,
                          "repo": repo, "downloaded_at": datetime.now(UTC).isoformat()}
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        log.info("verified", file=name, sha256=got)


if __name__ == "__main__":
    main()
