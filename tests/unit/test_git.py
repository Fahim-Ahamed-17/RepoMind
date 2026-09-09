from __future__ import annotations

import hashlib
from pathlib import Path

from repomind.ingest.git import changed_paths_since, content_sha, current_sha, is_git_repo


def test_non_git_directory_is_detected(tmp_path: Path) -> None:
    assert is_git_repo(tmp_path) is False
    assert current_sha(tmp_path) is None


def test_git_repo_with_no_commits_yet_has_no_current_sha(tmp_path: Path) -> None:
    from git import Repo as GitRepo

    GitRepo.init(tmp_path)
    assert is_git_repo(tmp_path) is True
    assert current_sha(tmp_path) is None  # unborn HEAD -- F-3 requirement 7


def test_current_sha_matches_the_real_commit(tmp_path: Path) -> None:
    from git import Repo as GitRepo

    repo = GitRepo.init(tmp_path)
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    repo.index.add(["a.py"])
    commit = repo.index.commit("initial")

    assert current_sha(tmp_path) == commit.hexsha


def test_changed_paths_since_reports_only_the_diff(tmp_path: Path) -> None:
    from git import Repo as GitRepo

    repo = GitRepo.init(tmp_path)
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 1", encoding="utf-8")
    repo.index.add(["a.py", "b.py"])
    first = repo.index.commit("first")

    (tmp_path / "a.py").write_text("x = 2", encoding="utf-8")  # only a.py changes
    repo.index.add(["a.py"])
    repo.index.commit("second")

    assert changed_paths_since(tmp_path, first.hexsha) == {"a.py"}


def test_content_sha_is_a_plain_sha256_of_the_bytes(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_bytes(b"hello world")
    assert content_sha(f) == hashlib.sha256(b"hello world").hexdigest()


def test_content_sha_changes_when_content_changes(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a", encoding="utf-8")
    first = content_sha(f)
    f.write_text("b", encoding="utf-8")
    second = content_sha(f)
    assert first != second
