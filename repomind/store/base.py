"""Storage protocols -- the seam that keeps a backend swap an implementation
change rather than a rewrite (docs/design.md section 11.3, AD-2).

Structural typing (``Protocol``, not ABC) per docs/conventions.md: adapters
stay independent of the core, and a second implementation never has to
inherit from anything here.

Only :class:`GraphStore` is defined in RM-012. ``VectorStore`` lands in
RM-032 (M3) once hybrid search (F-5, AD-8) makes its required shape
concrete -- declaring it now would mean guessing method signatures ahead of
the ticket that actually needs them.

AGENTS.md invariant 7: no raw SQL outside ``store/sqlite/``. Every query
here is bounded (a ``limit``) per docs/design.md section 11.3 -- nothing in
this codebase returns an unbounded result set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from repomind.model import (
        Edge,
        EdgeKind,
        File,
        IndexRun,
        IndexRunStatus,
        Repo,
        ScipStatus,
        Symbol,
        Tier,
    )

#: Default cap for any list-returning query that doesn't take an explicit
#: limit from the caller. Chosen to comfortably cover a single file's worth
#: of symbols or edges without being unbounded.
DEFAULT_LIST_LIMIT = 1000


class GraphStore(Protocol):
    """CRUD over one repository's symbol graph, plus index-run bookkeeping.

    One store instance owns one repo's SQLite file. Multi-hop traversal
    (the recursive CTE in docs/design.md section 4.4) is deliberately not
    part of this protocol -- it is RM-024's ticket, built on top of the
    single-hop primitives here (``edges_from`` / ``edges_to``).
    """

    def close(self) -> None:
        """Release the underlying connection. Idempotent."""
        ...

    # -- repo ----------------------------------------------------------

    def upsert_repo(self, repo: Repo) -> Repo:
        """Insert or update by ``root_path``. Returns the row with ``id`` set."""
        ...

    def get_repo(self, repo_id: int) -> Repo | None: ...

    def get_repo_by_path(self, root_path: str) -> Repo | None: ...

    def set_repo_indexed_sha(
        self, repo_id: int, sha: str | None, scip_status: ScipStatus | None
    ) -> None: ...

    def delete_repo(self, repo_id: int) -> None:
        """Cascades to every file, symbol, edge, chunk, and index_run row."""
        ...

    # -- file ------------------------------------------------------------

    def upsert_file(self, file: File) -> File:
        """Insert or update by ``(repo_id, path)``. Returns the row with
        ``id`` set. Callers use the returned ``blob_sha`` comparison (before
        calling this) to decide whether re-parsing is needed at all -- this
        method itself does not skip unchanged files, F-3's invalidation
        policy is index/incremental.py's job (RM-034), not the store's."""
        ...

    def get_file(self, file_id: int) -> File | None: ...

    def get_file_by_path(self, repo_id: int, path: str) -> File | None: ...

    def list_files(self, repo_id: int, limit: int = DEFAULT_LIST_LIMIT) -> Sequence[File]: ...

    def delete_file(self, file_id: int) -> None:
        """Cascades to that file's symbols, edges, and chunks."""
        ...

    # -- symbol ------------------------------------------------------------

    def replace_symbols(self, file_id: int, symbols: Iterable[Symbol]) -> Sequence[Symbol]:
        """Delete every existing symbol for ``file_id`` and insert ``symbols``
        in its place, atomically. Re-indexing a file is idempotent by
        construction: there is no "diff the old symbol set" step, because a
        changed file's symbol *identities* (line spans, signatures) can shift
        arbitrarily and are not worth reconciling row-by-row.
        """
        ...

    def get_symbol(self, symbol_id: int) -> Symbol | None: ...

    def find_symbol_by_qualified_name(self, repo_id: int, qualified_name: str) -> Symbol | None: ...

    def find_symbol_at_location(self, file_id: int, line: int) -> Symbol | None:
        """The innermost symbol whose span contains ``line``, if any."""
        ...

    def list_symbols(
        self, repo_id: int, file_id: int | None = None, limit: int = DEFAULT_LIST_LIMIT
    ) -> Sequence[Symbol]: ...

    def count_symbols_by_kind(self, repo_id: int) -> dict[str, int]:
        """For the index-completion summary (F-1 requirement 6)."""
        ...

    # -- edge ----------------------------------------------------------

    def insert_edges(self, edges: Iterable[Edge]) -> Sequence[Edge]:
        """Insert, ignoring an edge that already exists at the same
        ``(src, dst, kind, tier)`` -- see the UNIQUE constraint in schema.sql.
        A resolver re-emitting the same edge on re-index is not an error.
        """
        ...

    def delete_edges_from_file(self, evidence_file_id: int) -> None:
        """Remove every edge whose evidence points into this file, so a
        re-indexed file's edges don't accumulate duplicates or go stale.
        """
        ...

    def edges_from(
        self,
        symbol_id: int,
        kind: EdgeKind | None = None,
        tiers: Sequence[Tier] | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> Sequence[Edge]:
        """Outgoing edges (this symbol depends on / references / etc.)."""
        ...

    def edges_to(
        self,
        symbol_id: int,
        kind: EdgeKind | None = None,
        tiers: Sequence[Tier] | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> Sequence[Edge]:
        """Incoming edges (what depends on / references this symbol).
        The hot path for "what calls this" -- backed by ``idx_edge_dst``.
        """
        ...

    def count_edges_by_tier(self, repo_id: int) -> dict[str, int]:
        """For the index-completion summary and ``repomind status``
        (F-1 requirement 6, F-2 requirement 5)."""
        ...

    # -- index_run -------------------------------------------------------

    def start_index_run(
        self,
        repo_id: int,
        from_sha: str | None,
        to_sha: str | None,
        files_total: int | None,
    ) -> IndexRun:
        """Record the start of an indexing attempt (F-1 requirement 9)."""
        ...

    def update_index_run_progress(self, run_id: int, files_done: int) -> None:
        """Checkpoint, so an interrupted run resumes from here rather than
        restarting -- design.md section 5.1 and section 12."""
        ...

    def finish_index_run(
        self, run_id: int, status: IndexRunStatus, error: str | None = None
    ) -> None: ...

    def get_latest_index_run(self, repo_id: int) -> IndexRun | None: ...
