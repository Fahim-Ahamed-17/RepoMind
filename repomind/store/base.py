"""Storage protocols -- the seam that keeps a backend swap an implementation
change rather than a rewrite (docs/design.md section 11.3, AD-2).

Structural typing (``Protocol``, not ABC) per docs/conventions.md: adapters
stay independent of the core, and a second implementation never has to
inherit from anything here.

:class:`GraphStore` is RM-012; :class:`VectorStore` is RM-032, once hybrid
search (F-5, AD-8) made its required shape concrete -- declaring it any
earlier would have meant guessing method signatures ahead of the ticket
that actually needed them.

AGENTS.md invariant 7: no raw SQL outside ``store/sqlite/``. Every query
here is bounded (a ``limit``) per docs/design.md section 11.3 -- nothing in
this codebase returns an unbounded result set. ``VectorStore`` inherits
this for free on the vector side: ``sqlite-vec``'s own KNN syntax requires
a ``k`` bound to run at all (confirmed directly -- there is no unbounded
query form to accidentally reach for).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from repomind.model import (
        Chunk,
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

    def all_symbols(self, repo_id: int) -> Sequence[Symbol]:
        """Every symbol in the repo, uncapped.

        RM-021: the one deliberate exception to this file's own "every
        query is bounded" rule, stated as its own method rather than a
        large ``limit`` passed to :meth:`list_symbols` so the exception is
        named and searchable, not a magic number. Exists because heuristic
        resolution genuinely needs the complete table -- a reference
        routinely crosses file boundaries, so there is no page of results
        that would be enough on its own. "Uncapped" is bounded in practice
        by design.md section 11.1's own v1 targets (~50k symbols for a
        ~5k-file repo); this stops being fine only past the scale where
        :class:`GraphStore` itself needs revisiting (section 11.2).
        """
        ...

    def count_symbols_by_kind(self, repo_id: int) -> dict[str, int]:
        """For the index-completion summary (F-1 requirement 6)."""
        ...

    def set_symbol_scip_ids(self, scip_symbol_by_id: Mapping[int, str]) -> None:
        """Backfill ``Symbol.scip_symbol`` on already-persisted rows, keyed
        by ``Symbol.id``.

        RM-022: symbols are always persisted by ``replace_symbols`` *before*
        SCIP ever runs (SCIP resolution needs the whole repo's symbol table
        to exist first, same ordering constraint as the heuristic
        resolver). Updates rows in place rather than going through
        ``replace_symbols`` again, which would delete and reinsert with new
        ids -- silently orphaning any edge already persisted against the
        old ones.
        """
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

    def reverse_dependencies(
        self,
        symbol_id: int,
        max_depth: int,
        tiers: Sequence[Tier] | None = None,
    ) -> Sequence[tuple[Edge, int]]:
        """Everything that transitively depends on ``symbol_id`` -- "what
        calls this", walked backward through the graph -- as
        ``(edge, depth)`` pairs, ``depth`` counting hops from ``symbol_id``
        (a direct caller is depth 1).

        RM-024, built on ``edges_to`` above rather than replacing it: this
        is the multi-hop recursive-CTE traversal design.md section 4.4
        describes and that :class:`GraphStore`'s own module docstring
        named as deliberately out of scope for RM-012. Implements design's
        cycle-safety requirement by construction (a cyclic repo must not
        hang this query) and reports, for each reachable symbol, one edge
        per *kind* on a shortest path to it -- a symbol also reachable by
        a longer route does not additionally appear at that longer depth,
        and a symbol reachable by two different same-kind routes at the
        *same* shortest depth (a diamond: A->B->D and A->C->D both put A
        two hops from D) appears once per kind, not once per route. A
        different kind from the same symbol at the same depth is not
        collapsed -- "A imports B" and "A calls B" are distinct facts even
        at the same distance.

        Every edge kind can appear here, not only ``calls`` -- an
        ``imports``, ``inherits``, or ``references`` edge is just as much
        "this depends on that" for the purpose of "what would this
        change affect". F-8's impact analysis (M8) is built on exactly
        this same primitive with different seeding, not a separate query.

        Deliberately one-directional (reverse only): nothing in F-7 or F-8
        needs the forward direction ("what does this call"), and
        ``edges_from`` already answers that at a single hop, which is all
        either feature specifies. Depth is the caller's responsibility to
        cap sensibly (F-7 requirement 2: default 1, capped at 4) --
        unbounded here would defeat the "no unbounded query" rule this
        file states for everything else.

        A cyclic dependency graph never re-lists ``symbol_id`` itself,
        even though a genuine cycle (A -> B -> C -> A) does mean A
        transitively depends on itself: the seed occupies depth 0, which
        always wins the shortest-path comparison for its own id, so any
        longer cycle-induced path back to it never displaces that. A "what
        calls A" result listing A among the callers would read as a
        confusing implementation artifact, not a fact about the code.
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


class VectorStore(Protocol):
    """Chunk persistence, full-text search, and vector search (F-5, RM-032)
    over one repository's chunks -- ``chunk`` / ``chunk_fts`` / ``chunk_vec``
    in design.md section 4.2.

    A separate protocol from :class:`GraphStore`, per design.md section
    11.3 ("`GraphStore` and `VectorStore` protocols... swapping the backend
    stays an implementation change"), even though today's one implementation
    (:class:`~repomind.store.sqlite.graph.SqliteGraphStore`) satisfies both
    against the same connection -- one SQLite file holding graph, FTS5, and
    vectors together (design.md AD-2) is exactly what makes that possible,
    not a reason to fuse the two interfaces.

    Fusing ``search_fts`` and ``search_vector`` results by Reciprocal Rank
    Fusion (design.md AD-8) is deliberately not this protocol's job: each
    method here returns one retriever's own bounded, ranked results, and
    RM-033's ``retrieve/search.py`` combines them. A store that fused
    scores itself would make "swap sqlite-vec for an ANN index" (section
    11.2's own named future) a change to the fusion algorithm too, not
    just the vector backend.
    """

    def replace_chunks(self, file_id: int, chunks: Iterable[Chunk]) -> Sequence[Chunk]:
        """Delete every existing chunk for ``file_id`` and insert ``chunks``
        in its place, atomically -- the same idempotent-reindex contract
        :meth:`GraphStore.replace_symbols` makes. ``chunk_fts`` stays in
        sync automatically (schema.sql's own AFTER INSERT/DELETE/UPDATE
        triggers on ``chunk``); this method never touches ``chunk_fts`` or
        ``chunk_vec`` directly.
        """
        ...

    def set_chunk_embeddings(self, embeddings: Mapping[int, Sequence[float]]) -> None:
        """Store (or replace) each chunk's vector in ``chunk_vec``, keyed
        by ``Chunk.id``. Separate from :meth:`replace_chunks` because
        embedding is a distinct, potentially-slower step (an
        :class:`~repomind.embed.base.Embedder` call) that can legitimately
        fail on its own (design.md's failure table: embedding failure is
        not a useful partial state) after chunks and their text are
        already safely persisted.
        """
        ...

    def get_chunk(self, chunk_id: int) -> Chunk | None: ...

    def count_chunks(self, repo_id: int) -> int:
        """For the index-completion summary (F-1 requirement 6), matching
        :meth:`GraphStore.count_symbols_by_kind`'s own reporting role."""
        ...

    def search_fts(
        self, repo_id: int, query: str, limit: int = DEFAULT_LIST_LIMIT
    ) -> Sequence[tuple[Chunk, float]]:
        """Full-text search via FTS5 BM25, best match first. The score is
        BM25's own convention (more negative is a better match, per
        SQLite's ``bm25()``), not normalised or comparable across a
        different retriever's scores -- exactly why RRF (AD-8) fuses by
        *rank*, never by combining these numbers directly.
        """
        ...

    def search_vector(
        self, repo_id: int, embedding: Sequence[float], limit: int = DEFAULT_LIST_LIMIT
    ) -> Sequence[tuple[Chunk, float]]:
        """K-nearest-neighbour vector search via ``sqlite-vec``, best match
        first (smallest distance first). ``embedding`` must have the same
        dimensionality the configured :class:`~repomind.embed.base.Embedder`
        produces -- this method does not validate that itself; a mismatch
        is a caller bug, not a runtime condition to degrade from.
        """
        ...
