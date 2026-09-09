"""File discovery: which files in a repo get indexed.

RM-014. Implements F-1 requirement 2: respects .gitignore, skips known
vendor/build directories, skips files above a size cap.

Design choice worth stating plainly: **.gitignore compliance is delegated
to git itself** (``git ls-files --cached --others --exclude-standard``) when
the target is a git working tree, rather than reimplementing gitignore
pattern matching (negation, anchoring, ``**`` globs) by hand. That is
strictly more correct than a hand-rolled parser, and it is the common case
by a wide margin. When the target is *not* a git repository -- F-3
requirement 7 explicitly allows indexing one, with incremental updates
disabled -- there is no authoritative .gitignore engine to delegate to, so
discovery falls back to skipping the hardcoded vendor/build directory names
below plus any dot-directory. That fallback is a deliberately bounded,
best-effort scope: it will not honour a custom .gitignore rule in a
non-git directory. Worth knowing, not worth a gitignore-parsing dependency
for a secondary path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from git import Repo as GitRepo

from repomind.ingest.git import is_git_repo
from repomind.workspace import workspace_root

if TYPE_CHECKING:
    from collections.abc import Iterator

#: F-1 requirement 2's default. Overridable per call.
DEFAULT_SIZE_CAP_BYTES = 1_000_000

#: Always skipped, in both the git and non-git discovery paths -- generated
#: and vendored content no one wants indexed even when a sloppy repo forgot
#: to gitignore it. F-1 requirement 2 names node_modules, .venv, dist,
#: build, site-packages explicitly; the rest are the same category of thing.
DEFAULT_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "bower_components",
        ".venv",
        "venv",
        "env",
        "dist",
        "build",
        "site-packages",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    """One file found and cleared for indexing, before parsing."""

    abs_path: Path
    rel_path: str
    """Repo-relative, forward slashes, regardless of host OS -- matches
    :attr:`repomind.model.File.path`."""

    size_bytes: int


def discover_files(
    root: Path,
    *,
    size_cap_bytes: int = DEFAULT_SIZE_CAP_BYTES,
    skip_dir_names: frozenset[str] = DEFAULT_SKIP_DIR_NAMES,
) -> Iterator[DiscoveredFile]:
    """Yield every file under ``root`` cleared for indexing.

    Never yields anything under RepoMind's own workspace root
    (``~/.repomind``) even if ``root`` happens to contain it -- indexing our
    own index would be a strange and unbounded recursion.
    """
    root = root.resolve()
    ignore_prefix = str(workspace_root())

    if is_git_repo(root):
        candidates = _discover_via_git(root)
    else:
        candidates = _discover_via_walk(root, skip_dir_names)

    for rel_path in candidates:
        abs_path = root / rel_path
        if str(abs_path).startswith(ignore_prefix):
            continue
        if not abs_path.is_file() or abs_path.is_symlink():
            continue
        try:
            size = abs_path.stat().st_size
        except OSError:
            continue
        if size > size_cap_bytes:
            continue
        yield DiscoveredFile(abs_path=abs_path, rel_path=rel_path, size_bytes=size)


def _discover_via_git(root: Path) -> Iterator[str]:
    repo = GitRepo(root, search_parent_directories=False)
    # -z: NUL-separated, so filenames containing spaces or newlines survive
    # intact. --cached: tracked files. --others --exclude-standard:
    # untracked files that .gitignore (and friends) would *not* exclude --
    # a file you just created but haven't committed is still real source.
    raw = repo.git.ls_files("--cached", "--others", "--exclude-standard", "-z")
    for entry in raw.split("\0"):
        if entry:
            yield entry.replace("\\", "/")


def _discover_via_walk(root: Path, skip_dir_names: frozenset[str]) -> Iterator[str]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in skip_dir_names and not d.startswith(".")]
        rel_dir = Path(dirpath).relative_to(root)
        for name in filenames:
            rel = (rel_dir / name) if str(rel_dir) != "." else Path(name)
            yield rel.as_posix()


__all__ = [
    "DEFAULT_SIZE_CAP_BYTES",
    "DEFAULT_SKIP_DIR_NAMES",
    "DiscoveredFile",
    "discover_files",
]
