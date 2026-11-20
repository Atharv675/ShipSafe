import json
from pathlib import Path

from sentinel.provenance import git_commit

SHA = "0123456789abcdef0123456789abcdef01234567"


def _git(tmp_path: Path, head: str) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(head)
    return tmp_path


def test_no_repo(tmp_path: Path) -> None:
    assert git_commit(tmp_path) == "uncommitted"


def test_branch_without_commits(tmp_path: Path) -> None:
    assert git_commit(_git(tmp_path, "ref: refs/heads/main\n")) == "uncommitted"


def test_loose_ref(tmp_path: Path) -> None:
    root = _git(tmp_path, "ref: refs/heads/main\n")
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "refs" / "heads" / "main").write_text(SHA + "\n")
    assert git_commit(root) == SHA


def test_packed_ref(tmp_path: Path) -> None:
    root = _git(tmp_path, "ref: refs/heads/main\n")
    (root / ".git" / "packed-refs").write_text(f"# pack-refs\n{SHA} refs/heads/main\n")
    assert git_commit(root) == SHA


def test_bundle_info_fallback(tmp_path: Path) -> None:
    (tmp_path / "BUNDLE_INFO.json").write_text(json.dumps({"git_commit": SHA}))
    assert git_commit(tmp_path) == f"{SHA} (code bundle)"


def test_detached_head(tmp_path: Path) -> None:
    assert git_commit(_git(tmp_path, SHA + "\n")) == SHA


def test_bundle_info_pins_the_shipped_files(tmp_path: Path) -> None:
    files = {"src/a.py": "11", "configs/b.yaml": "22"}
    (tmp_path / "BUNDLE_INFO.json").write_text(json.dumps({"git_commit": SHA, "files": files}))
    first = git_commit(tmp_path)
    assert first.startswith(f"{SHA} (code bundle, files sha256 ")
    (tmp_path / "BUNDLE_INFO.json").write_text(
        json.dumps({"git_commit": SHA, "files": {**files, "src/a.py": "33"}})
    )
    assert git_commit(tmp_path) != first  # same commit, different code -> different lineage
