"""Git integration: repo detection, SHA resolution, diffs, content hashing.

RM-015. Used by ``discover.py`` (to delegate .gitignore semantics to git
itself rather than reimplementing gitignore pattern matching) and by
``index/pipeline.py`` (to record the indexed SHA, F-1 requirement 5).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError
from git import Repo as GitRepo


def is_git_repo(root: Path) -> bool:
    try:
        GitRepo(root, search_parent_directories=False)
    except (InvalidGitRepositoryError, NoSuchPathError):
        return False
    return True


def current_sha(root: Path) -> str | None:
    """The commit HEAD points at, or ``None`` if this isn't a git repo, or
    is one with no commits yet (unborn HEAD) -- F-3 requirement 7 requires
    both cases to degrade to "index anyway, no incremental updates" rather
    than fail.
    """
    try:
        repo = GitRepo(root, search_parent_directories=False)
        return repo.head.commit.hexsha
    except (InvalidGitRepositoryError, NoSuchPathError, ValueError):
        return None


def commits_behind(root: Path, from_sha: str, to_sha: str = "HEAD") -> int | None:
    """How many commits ``to_sha`` is ahead of ``from_sha`` -- the "SHA
    drift" ``repomind status`` reports (F-15 requirement 2).

    ``None``, not an exception, whenever this can't be answered: not a git
    repo, or ``from_sha`` is no longer reachable at all (a
    rebase or history rewrite since that index was built) -- "unknown drift"
    is the honest answer in both cases, not a crash or a wrong number.
    """
    try:
        repo = GitRepo(root, search_parent_directories=False)
        output = repo.git.rev_list("--count", f"{from_sha}..{to_sha}")
        return int(output.strip())
    except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError, ValueError):
        return None


def changed_paths_since(root: Path, from_sha: str, to_sha: str = "HEAD") -> set[str]:
    """Repo-relative paths that differ between two commits. The primitive
    RM-034's incremental invalidation (M3) builds on; not exercised by M1's
    pipeline, which always does a full index.
    """
    repo = GitRepo(root, search_parent_directories=False)
    diff_output = repo.git.diff("--name-only", f"{from_sha}..{to_sha}")
    return {line for line in diff_output.splitlines() if line}


def content_sha(path: Path) -> str:
    """A plain SHA-256 of the file's raw bytes.

    Deliberately *not* git's own blob-hash algorithm (``"blob "
    + len + NUL + content``, SHA-1 by default). The ``File.blob_sha`` field
    only needs content-hash semantics for F-3's invalidation -- "did this
    file's content change" -- not byte-for-byte compatibility with git's
    object store, which also varies with line-ending filters and, on newer
    repos, may use SHA-256 instead of SHA-1. A plain content hash is simpler
    and identical whether or not the file lives in a git repo at all.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()
