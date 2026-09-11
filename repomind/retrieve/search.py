"""Hybrid code search: vector similarity and FTS5 BM25, fused by
Reciprocal Rank Fusion (F-5, RM-033, design.md AD-8).

This is step 1 ("seed") of the bounded answering loop (design.md section
5.2) and the library's own ``repo.search(...)`` entry point (section
6.1) -- graph expansion (RM-035), budget packing, and synthesis are
later steps this module deliberately does not touch. Requires no LLM
(F-5 requirement 4): the only model involved is the local
:class:`~repomind.embed.base.Embedder`, used to embed the query text,
never to reason about it.

:func:`reciprocal_rank_fusion` is a pure function over ranked ID lists,
independent of storage or embeddings -- unit-tested directly with plain
lists per docs/implementation-plan.md section 11 ("Pure functions:
chunker rules, RRF, ..."). :func:`search` is the I/O-heavy orchestration
around it: fetch a candidate pool from each retriever, fuse, resolve
each surviving chunk's file and enclosing symbol, and filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from repomind.embed.base import Embedder
    from repomind.model import Chunk, File, Symbol, SymbolKind
    from repomind.store.base import GraphStore, VectorStore

    class _Store(GraphStore, VectorStore, Protocol):
        """Structural intersection: search needs VectorStore's two
        retrievers plus GraphStore's ``get_file``/``get_symbol`` to
        resolve each hit's file and enclosing symbol. Same technique
        ``analyze/registry.py``'s own ``_Store`` uses (RM-032).
        """


#: design.md AD-8's own formula: score = sum(1 / (k + rank)), 1-indexed rank.
RRF_K = 60

#: How many best-match results to pull from *each* retriever before fusion.
#: Wide enough that RRF has real overlap to find between two independent
#: rankings; small enough to stay well inside F-5 requirement 5's 500ms p95
#: even on a large repo, since both retrievers already return best-match-first
#: over an indexed, bounded query (FTS5 BM25 / sqlite-vec KNN).
CANDIDATE_POOL_SIZE = 25

#: Default number of fused hits :func:`search` returns. A future CLI/MCP
#: caller (RM-045, RM-052) may expose its own override; this is the library
#: API's own default, not a hard cap -- unlike ``CANDIDATE_POOL_SIZE``.
DEFAULT_RESULT_LIMIT = 10


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ranked result (F-5 requirement 1: file, line span, enclosing
    symbol -- all present here via ``chunk``/``file``/``symbol`` rather
    than flattened into bare strings, the same shape
    ``analyze/reverse.py``'s ``ReferenceHit`` uses for its own source
    symbol).
    """

    chunk: Chunk
    file: File
    symbol: Symbol | None
    """The chunk's enclosing symbol (``Chunk.symbol_id`` resolved), or
    ``None`` for a file-level remainder chunk outside any extracted
    symbol -- mirrors ``Chunk.symbol_id``'s own docstring."""

    score: float
    """The fused RRF score (higher is better) -- not BM25 or cosine
    distance, and not comparable across two different queries or repos.
    """


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[int]], *, k: int = RRF_K
) -> list[tuple[int, float]]:
    """Fuse any number of ranked ID lists into one, by Reciprocal Rank
    Fusion: an id's score is the sum of ``1 / (k + rank)`` (1-indexed)
    over every ranking it appears in. An id absent from a ranking simply
    contributes nothing from it -- this is what makes RRF degrade
    correctly when one retriever returns nothing (F-5 requirement 6)
    rather than needing every list to agree on membership, and why AD-8
    picked it over score blending: no normalisation, no retuning.

    Returns ``(id, score)`` pairs sorted by descending score, ties broken
    by ascending id for a deterministic order (docs/conventions.md:
    never depend on dict ordering).
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def _resolve_hit(
    store: _Store,
    chunk: Chunk,
    score: float,
    file_cache: dict[int, File | None],
    symbol_cache: dict[int, Symbol | None],
) -> SearchHit | None:
    """``None`` only for a chunk whose file row is somehow gone -- should
    not happen for a well-formed graph (``ON DELETE CASCADE`` removes a
    file's chunks with it); skip rather than raise, same reasoning
    ``find_references`` applies to a dangling edge source.
    """
    if chunk.id is None:
        return None
    if chunk.file_id not in file_cache:
        file_cache[chunk.file_id] = store.get_file(chunk.file_id)
    file = file_cache[chunk.file_id]
    if file is None:
        return None

    symbol: Symbol | None = None
    if chunk.symbol_id is not None:
        if chunk.symbol_id not in symbol_cache:
            symbol_cache[chunk.symbol_id] = store.get_symbol(chunk.symbol_id)
        symbol = symbol_cache[chunk.symbol_id]

    return SearchHit(chunk=chunk, file=file, symbol=symbol, score=score)


def _matches_filters(
    hit: SearchHit,
    languages: Sequence[str] | None,
    path_prefix: str | None,
    kinds: Sequence[SymbolKind] | None,
) -> bool:
    if languages is not None and hit.file.lang not in languages:
        return False
    if path_prefix is not None and not hit.file.path.startswith(path_prefix):
        return False
    return kinds is None or (hit.symbol is not None and hit.symbol.kind in kinds)


def search(
    store: _Store,
    embedder: Embedder,
    repo_id: int,
    query: str,
    *,
    limit: int = DEFAULT_RESULT_LIMIT,
    languages: Sequence[str] | None = None,
    path_prefix: str | None = None,
    kinds: Sequence[SymbolKind] | None = None,
) -> list[SearchHit]:
    """F-5's main entry point: hybrid search over one repo's chunks.

    ``embedder`` is a caller-supplied, already-loaded
    :class:`~repomind.embed.base.Embedder` rather than constructed here --
    unlike indexing's one-shot ``LocalEmbedder()`` per run
    (``index/pipeline.py``'s ``_embed_chunks``), a search call happens on
    every query, and reloading the model from disk each time would blow
    F-5 requirement 5's 500ms p95 on its own before a single row is
    queried.

    ``languages``/``path_prefix``/``kinds`` (F-5 requirement 3) filter
    the *fused* candidate pool, not the underlying SQL -- there is no
    store-level filtering by these yet, only by ``repo_id``. A filter
    that excludes most of ``CANDIDATE_POOL_SIZE``'s candidates can
    therefore return fewer than ``limit`` hits even when more matches
    exist further down either retriever's own ranking; widening the pool
    or pushing filters into the store is future work once a real caller
    (RM-045's CLI flags, most likely) makes that cost worth paying.
    """
    fts_hits = store.search_fts(repo_id, query, limit=CANDIDATE_POOL_SIZE)
    (query_vector,) = embedder.embed([query])
    vector_hits = store.search_vector(repo_id, query_vector, limit=CANDIDATE_POOL_SIZE)

    chunks_by_id: dict[int, Chunk] = {}
    for chunk, _score in (*fts_hits, *vector_hits):
        if chunk.id is not None:
            chunks_by_id[chunk.id] = chunk

    fts_ranking = [chunk.id for chunk, _score in fts_hits if chunk.id is not None]
    vector_ranking = [chunk.id for chunk, _score in vector_hits if chunk.id is not None]
    fused = reciprocal_rank_fusion([fts_ranking, vector_ranking])

    file_cache: dict[int, File | None] = {}
    symbol_cache: dict[int, Symbol | None] = {}
    hits: list[SearchHit] = []
    for chunk_id, score in fused:
        hit = _resolve_hit(store, chunks_by_id[chunk_id], score, file_cache, symbol_cache)
        if hit is None or not _matches_filters(hit, languages, path_prefix, kinds):
            continue
        hits.append(hit)
        if len(hits) == limit:
            break
    return hits
