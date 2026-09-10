"""Tests for RM-025's ``repomind list`` / ``status`` reporting logic
(F-15). Pure assembly over a real, small ``SqliteGraphStore`` -- no CLI,
no filesystem/git facts, which the caller (``cli/main.py``) gathers and
passes in already computed. See ``tests/unit/test_workspace.py`` and
``tests/unit/test_git.py`` for the pieces this module is assembled from.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repomind.analyze.registry import repo_listing, repo_status
from repomind.model import IndexRunStatus, Repo, ScipStatus
from repomind.store.sqlite.graph import SqliteGraphStore


@pytest.fixture
def store(tmp_path: Path) -> SqliteGraphStore:
    s = SqliteGraphStore(tmp_path / "index.db")
    yield s
    s.close()


def test_repo_listing_for_a_never_indexed_repo(store: SqliteGraphStore) -> None:
    """A repo can be registered (workspace.py's own registry) without ever
    having actually been indexed into *this particular* store -- e.g. the
    workspace directory exists but index.db has no repo row yet. Every
    field beyond what the caller supplies directly reads back as
    ``None``, not a crash on a missing row.
    """
    listing = repo_listing(store, "/some/repo", exists=True, size_bytes=0)

    assert listing.root_path == "/some/repo"
    assert listing.exists is True
    assert listing.indexed_sha is None
    assert listing.indexed_at is None
    assert listing.scip_status is None
    assert listing.size_bytes == 0


def test_repo_listing_reads_back_a_real_repo_row(store: SqliteGraphStore) -> None:
    repo = store.upsert_repo(Repo(root_path="/some/repo"))
    assert repo.id is not None
    store.set_repo_indexed_sha(repo.id, "abc123", ScipStatus.OK)

    listing = repo_listing(store, "/some/repo", exists=False, size_bytes=4096)

    assert listing.indexed_sha == "abc123"
    assert listing.indexed_at is not None
    assert listing.scip_status == ScipStatus.OK
    assert listing.exists is False  # passed straight through from the caller
    assert listing.size_bytes == 4096


def test_repo_status_returns_none_for_an_id_that_does_not_exist(store: SqliteGraphStore) -> None:
    assert (
        repo_status(store, 999, "/some/repo", current_sha=None, commits_behind=None, size_bytes=0)
        is None
    )


def test_repo_status_reports_counts_and_drift(store: SqliteGraphStore) -> None:
    repo = store.upsert_repo(Repo(root_path="/some/repo"))
    assert repo.id is not None
    store.set_repo_indexed_sha(repo.id, "abc123", ScipStatus.DEGRADED)

    report = repo_status(
        store,
        repo.id,
        "/some/repo",
        current_sha="def456",
        commits_behind=3,
        size_bytes=2048,
    )

    assert report is not None
    assert report.indexed_sha == "abc123"
    assert report.current_sha == "def456"
    assert report.commits_behind == 3
    assert report.scip_status == ScipStatus.DEGRADED
    assert report.symbol_counts  # prefilled with every kind at 0 -- never empty
    assert report.edge_counts
    assert report.size_bytes == 2048
    assert report.last_run_status is None  # no index_run row created in this test
    assert report.last_run_duration_seconds is None


def test_repo_status_computes_last_run_duration_from_real_timestamps(
    store: SqliteGraphStore,
) -> None:
    repo = store.upsert_repo(Repo(root_path="/some/repo"))
    assert repo.id is not None
    run = store.start_index_run(repo.id, from_sha=None, to_sha="abc123", files_total=5)
    assert run.id is not None
    store.finish_index_run(run.id, IndexRunStatus.OK)

    report = repo_status(
        store, repo.id, "/some/repo", current_sha=None, commits_behind=None, size_bytes=0
    )

    assert report is not None
    assert report.last_run_status == IndexRunStatus.OK
    assert report.last_run_duration_seconds is not None
    assert report.last_run_duration_seconds >= 0.0
