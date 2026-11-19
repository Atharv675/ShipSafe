"""Provenance helpers. Reads git metadata files directly (read-only; never runs git)."""

from __future__ import annotations

import json
from pathlib import Path

BUNDLE_INFO = "BUNDLE_INFO.json"


def git_commit(repo_root: Path) -> str:
    """Return the checked-out commit hash, or 'uncommitted' if there is none yet.

    Outside a git checkout (e.g. the code bundle on Kaggle/Colab) fall back to the commit
    recorded in BUNDLE_INFO.json by scripts/make_bundle.py.
    """
    git_dir = repo_root / ".git"
    head_file = git_dir / "HEAD"
    if not head_file.is_file():
        info = repo_root / BUNDLE_INFO
        if info.is_file():
            data = json.loads(info.read_text(encoding="utf-8"))
            return f"{data.get('git_commit', 'uncommitted')} (code bundle)"
        return "uncommitted"
    head = head_file.read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        return head  # detached HEAD holds the hash itself
    ref = head.removeprefix("ref: ").strip()
    loose = git_dir / ref
    if loose.is_file():
        return loose.read_text(encoding="utf-8").strip()
    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return "uncommitted"
