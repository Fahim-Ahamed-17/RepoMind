"""Repository registry and status reporting for ``repomind list`` /
``status`` (F-15).

Same shape as ``reverse.py``: a thin, storage-agnostic layer over an
already-open :class:`~repomind.store.base.GraphStore` plus whatever
filesystem and git facts the caller (``cli/main.py``) has already
gathered (``workspace.list_registered_repos``, ``workspace.
workspace_dir_size``, ``ingest.git.commits_behind``) -- this module never
opens a store connection or touches the filesystem itself, and requires
no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repomind.model import IndexRunStatus, ScipStatus
    from repomind.store.base import GraphStore


@dataclass(frozen=True, slots=True)
class RepoListing:
    """One row of ``repomind list`` (F-15 requirement 1)."""

    root_path: str
    exists: bool
    """The *source* repository is still there on disk -- see
    ``workspace.RegistryEntry.exists``. Independent of whether our own
    index data for it still exists (``size_bytes`` answers that)."""

    indexed_sha: str | None
    indexed_at: str | None
    scip_status: ScipStatus | None
    size_bytes: int


def repo_listing(
    store: GraphStore, root_path: str, *, exists: bool, size_bytes: int
) -> RepoListing:
    """Build one ``RepoListing`` from ``store`` -- already open against
    this specific repo's own ``index.db`` (design.md AD-5: one file per
    repo, so there is no ``repo_id`` to look up first, unlike every other
    ``GraphStore`` method).
    """
    repo = store.get_repo_by_path(root_path)
    return RepoListing(
        root_path=root_path,
        exists=exists,
        indexed_sha=repo.indexed_sha if repo is not None else None,
        indexed_at=repo.indexed_at if repo is not None else None,
        scip_status=repo.scip_status if repo is not None else None,
        size_bytes=size_bytes,
    )


@dataclass(frozen=True, slots=True)
class RepoStatusReport:
    """``repomind status [PATH]`` (F-15 requirement 2)."""

    root_path: str
    indexed_sha: str | None
    current_sha: str | None
    commits_behind: int | None
    """``None`` means "unknown", not "zero" -- see
    ``ingest.git.commits_behind`` for when that happens (not a git repo,
    or ``indexed_sha`` is no longer reachable at all)."""

    scip_status: ScipStatus | None
    symbol_counts: dict[str, int]
    edge_counts: dict[str, int]
    last_run_status: IndexRunStatus | None
    last_run_duration_seconds: float | None
    size_bytes: int


def repo_status(
    store: GraphStore,
    repo_id: int,
    root_path: str,
    *,
    current_sha: str | None,
    commits_behind: int | None,
    size_bytes: int,
) -> RepoStatusReport | None:
    """``None`` only when ``repo_id`` no longer resolves to a row --
    should not happen for an ``id`` the caller just read from the same
    store, but a store is still a store (RM-023: no silent behaviour is
    preferable to a possible ``AttributeError`` on ``None`` deeper in).
    """
    repo = store.get_repo(repo_id)
    if repo is None:
        return None

    last_run = store.get_latest_index_run(repo_id)
    duration = None
    if last_run is not None and last_run.finished_at is not None:
        duration = (
            datetime.fromisoformat(last_run.finished_at)
            - datetime.fromisoformat(last_run.started_at)
        ).total_seconds()

    return RepoStatusReport(
        root_path=root_path,
        indexed_sha=repo.indexed_sha,
        current_sha=current_sha,
        commits_behind=commits_behind,
        scip_status=repo.scip_status,
        symbol_counts=store.count_symbols_by_kind(repo_id),
        edge_counts=store.count_edges_by_tier(repo_id),
        last_run_status=last_run.status if last_run is not None else None,
        last_run_duration_seconds=duration,
        size_bytes=size_bytes,
    )
