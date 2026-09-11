"""Tests for ingest/git.py (RM-015): SHA resolution, diffs, content hashing.

Every ``Repo`` here is used via ``with``: GitPython keeps persistent
``git cat-file`` subprocesses per instance and only tears them down in
``close()``. Left to the garbage collector they surface as
ResourceWarnings, which pyproject.toml's ``filterwarnings = ["error",
...]`` turns into a failure attributed to whichever test happened to be
running when GC fired -- the same leak tests/integration/test_incremental.py
documents, confirmed here too.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from git import Repo as GitRepo
from repomind.ingest.git import (
    changed_paths_since,
    commits_behind,
    content_sha,
    current_sha,
    is_git_repo,
)


def test_non_git_directory_is_detected(tmp_path: Path) -> None:
    assert is_git_repo(tmp_path) is False
    assert current_sha(tmp_path) is None


def test_git_repo_with_no_commits_yet_has_no_current_sha(tmp_path: Path) -> None:
    with GitRepo.init(tmp_path):
        assert is_git_repo(tmp_path) is True
        assert current_sha(tmp_path) is None  # unborn HEAD -- F-3 requirement 7


def test_current_sha_matches_the_real_commit(tmp_path: Path) -> None:
    with GitRepo.init(tmp_path) as repo:
        (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
        repo.index.add(["a.py"])
        commit = repo.index.commit("initial")

    assert current_sha(tmp_path) == commit.hexsha


def test_changed_paths_since_reports_only_the_diff(tmp_path: Path) -> None:
    with GitRepo.init(tmp_path) as repo:
        (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
        (tmp_path / "b.py").write_text("y = 1", encoding="utf-8")
        repo.index.add(["a.py", "b.py"])
        first = repo.index.commit("first")

        (tmp_path / "a.py").write_text("x = 2", encoding="utf-8")  # only a.py changes
        repo.index.add(["a.py"])
        repo.index.commit("second")

    assert changed_paths_since(tmp_path, first.hexsha) == {"a.py"}


def test_changed_paths_since_is_none_for_a_non_git_directory(tmp_path: Path) -> None:
    assert changed_paths_since(tmp_path, "deadbeef") is None


def test_changed_paths_since_is_none_when_the_indexed_sha_no_longer_exists(
    tmp_path: Path,
) -> None:
    with GitRepo.init(tmp_path) as repo:
        (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
        repo.index.add(["a.py"])
        repo.index.commit("only commit")

    # Same "history rewritten since this index was built" case
    # commits_behind must not crash on -- RM-034 falls back to a full
    # index rather than raising.
    assert changed_paths_since(tmp_path, "0" * 40) is None


def test_commits_behind_counts_commits_made_since_indexing(tmp_path: Path) -> None:
    with GitRepo.init(tmp_path) as repo:
        (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
        repo.index.add(["a.py"])
        indexed_at = repo.index.commit("indexed here").hexsha

        for i in range(3):
            (tmp_path / "a.py").write_text(f"x = {i}", encoding="utf-8")
            repo.index.add(["a.py"])
            repo.index.commit(f"commit {i}")

    assert commits_behind(tmp_path, indexed_at) == 3


def test_commits_behind_is_zero_when_up_to_date(tmp_path: Path) -> None:
    with GitRepo.init(tmp_path) as repo:
        (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
        repo.index.add(["a.py"])
        head = repo.index.commit("only commit").hexsha

    assert commits_behind(tmp_path, head) == 0


def test_commits_behind_is_none_for_a_non_git_directory(tmp_path: Path) -> None:
    assert commits_behind(tmp_path, "deadbeef") is None


def test_commits_behind_is_none_when_the_indexed_sha_no_longer_exists(tmp_path: Path) -> None:
    with GitRepo.init(tmp_path) as repo:
        (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
        repo.index.add(["a.py"])
        repo.index.commit("only commit")

    # A SHA that was never actually a commit here -- the "history rewritten
    # since this index was built" case status must not crash on.
    assert commits_behind(tmp_path, "0" * 40) is None


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
