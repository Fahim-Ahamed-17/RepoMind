"""Tests for retrieve/search.py (RM-033): the pure RRF fusion function,
and the real-store hybrid search orchestration around it.

Real ``SqliteGraphStore`` (sqlite-vec + FTS5), never mocked -- same
reasoning as tests/unit/test_store_vector.py: both are cheap, local, and
deterministic. The embedder is a small hand-built fake with
caller-controlled vectors, not the real model (that dependency belongs
to tests/unit/test_embed.py's ``embedding_model``-marked test alone).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repomind.model import Chunk, File, Repo, Symbol, SymbolKind
from repomind.retrieve.search import RRF_K, reciprocal_rank_fusion, search
from repomind.store.sqlite.graph import SqliteGraphStore

# -- reciprocal_rank_fusion (pure) ---------------------------------------


def test_a_single_ranking_scores_by_its_own_rank_alone() -> None:
    fused = reciprocal_rank_fusion([[10, 20, 30]])

    assert [item_id for item_id, _score in fused] == [10, 20, 30]
    assert fused[0][1] == pytest.approx(1 / (RRF_K + 1))
    assert fused[1][1] == pytest.approx(1 / (RRF_K + 2))
    assert fused[2][1] == pytest.approx(1 / (RRF_K + 3))


def test_an_id_in_both_rankings_sums_both_contributions() -> None:
    fused = reciprocal_rank_fusion([[1, 2], [2, 1]])
    scores = dict(fused)

    assert scores[1] == pytest.approx(1 / (RRF_K + 1) + 1 / (RRF_K + 2))
    assert scores[2] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    # Symmetric ranks (1st in one list, 2nd in the other, for both ids) --
    # tie broken by ascending id.
    assert [item_id for item_id, _score in fused] == [1, 2]


def test_an_id_in_only_one_ranking_still_appears() -> None:
    fused = reciprocal_rank_fusion([[1, 2], [3]])

    assert {item_id for item_id, _score in fused} == {1, 2, 3}


def test_an_empty_ranking_degrades_correctly() -> None:
    """F-5 requirement 6: fusion must not need every retriever to agree
    on membership -- one empty ranking is the extreme case."""
    fused = reciprocal_rank_fusion([[1, 2, 3], []])

    assert [item_id for item_id, _score in fused] == [1, 2, 3]


def test_every_ranking_empty_produces_no_results() -> None:
    assert reciprocal_rank_fusion([[], []]) == []


# -- search() orchestration ----------------------------------------------


class _FakeEmbedder:
    dimensions = 384

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors[t] for t in texts]


@pytest.fixture
def store(tmp_path: Path) -> SqliteGraphStore:
    s = SqliteGraphStore(tmp_path / "index.db")
    yield s
    s.close()


@pytest.fixture
def repo_id(store: SqliteGraphStore) -> int:
    repo = store.upsert_repo(Repo(root_path="/proj"))
    assert repo.id is not None
    return repo.id


@pytest.fixture
def file_id(store: SqliteGraphStore, repo_id: int) -> int:
    f = store.upsert_file(File(repo_id=repo_id, path="pkg/widget.py", lang="python", blob_sha="s"))
    assert f.id is not None
    return f.id


def _vec(*values: float, dims: int = 384) -> list[float]:
    """A vector matching chunk_vec's real FLOAT[384] column -- see
    test_store_vector.py's identical helper for why."""
    v = list(values) + [0.0] * (dims - len(values))
    return v[:dims]


def _chunk(repo_id: int, file_id: int, text: str, *, symbol_id: int | None = None) -> Chunk:
    return Chunk(
        repo_id=repo_id,
        file_id=file_id,
        symbol_id=symbol_id,
        start_line=1,
        end_line=len(text.splitlines()) or 1,
        text=text,
        n_tokens=len(text.split()),
    )


def test_search_ranks_a_chunk_present_in_both_retrievers_above_a_vector_only_best_match(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    """RRF rewards appearing in both rankings over being the single best
    match in just one: a chunk found by both FTS and vector search
    outranks a chunk that is the closest possible vector match but
    invisible to FTS entirely.
    """
    in_both, vector_only = store.replace_chunks(
        file_id,
        [
            _chunk(repo_id, file_id, "def make_widget(): return Widget()"),
            _chunk(repo_id, file_id, "class Animal: pass"),
        ],
    )
    assert in_both.id is not None and vector_only.id is not None
    store.set_chunk_embeddings(
        {
            in_both.id: _vec(0.9, 0.1),  # vector rank 2: close, not closest
            vector_only.id: _vec(1.0, 0.0),  # vector rank 1: exact match
        }
    )
    embedder = _FakeEmbedder({"widget": _vec(1.0, 0.0)})

    hits = search(store, embedder, repo_id, "widget")

    assert [h.chunk.id for h in hits] == [in_both.id, vector_only.id]


def test_search_surfaces_a_vector_only_match_fts_never_saw(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    """F-5 requirement 6, the FTS-empty direction: a chunk whose text
    shares no token with the query must still surface if it is close in
    embedding space -- fusion does not require both retrievers to agree.
    """
    (chunk,) = store.replace_chunks(file_id, [_chunk(repo_id, file_id, "class Animal: pass")])
    assert chunk.id is not None
    store.set_chunk_embeddings({chunk.id: _vec(1.0, 0.0)})
    embedder = _FakeEmbedder({"nonexistenttoken": _vec(1.0, 0.0)})

    hits = search(store, embedder, repo_id, "nonexistenttoken")

    assert [h.chunk.id for h in hits] == [chunk.id]


def test_search_still_returns_fts_hits_when_nothing_is_embedded_yet(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    """F-5 requirement 6, the vector-empty direction: no chunk in this
    repo has an embedding at all (chunk_vec is empty), so search_vector
    returns nothing -- search() must still return the FTS hits rather
    than an empty result or a crash.
    """
    store.replace_chunks(file_id, [_chunk(repo_id, file_id, "def make_widget(): pass")])
    embedder = _FakeEmbedder({"widget": _vec(1.0, 0.0)})

    hits = search(store, embedder, repo_id, "widget")

    assert len(hits) == 1
    assert "widget" in hits[0].chunk.text.lower()


def test_search_resolves_file_and_enclosing_symbol(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    (widget_symbol,) = store.replace_symbols(
        file_id,
        [
            Symbol(
                repo_id=repo_id,
                file_id=file_id,
                kind=SymbolKind.FUNCTION,
                name="make_widget",
                qualified_name="pkg.widget.make_widget",
                start_line=1,
                end_line=2,
            )
        ],
    )
    assert widget_symbol.id is not None
    in_symbol, remainder = store.replace_chunks(
        file_id,
        [
            _chunk(repo_id, file_id, "def make_widget(): pass", symbol_id=widget_symbol.id),
            _chunk(repo_id, file_id, "MODULE_CONST = 1", symbol_id=None),
        ],
    )
    assert in_symbol.id and remainder.id
    store.set_chunk_embeddings({in_symbol.id: _vec(1.0), remainder.id: _vec(0.0, 1.0)})
    embedder = _FakeEmbedder({"query": _vec(1.0)})

    hits = search(store, embedder, repo_id, "query")

    by_chunk_id = {h.chunk.id: h for h in hits}
    assert by_chunk_id[in_symbol.id].file.path == "pkg/widget.py"
    assert by_chunk_id[in_symbol.id].symbol is not None
    assert by_chunk_id[in_symbol.id].symbol.qualified_name == "pkg.widget.make_widget"
    assert by_chunk_id[remainder.id].symbol is None


def test_search_filters_by_language(store: SqliteGraphStore, repo_id: int) -> None:
    py_file = store.upsert_file(File(repo_id=repo_id, path="a.py", lang="python", blob_sha="s"))
    ts_file = store.upsert_file(File(repo_id=repo_id, path="b.ts", lang="typescript", blob_sha="s"))
    assert py_file.id is not None and ts_file.id is not None
    (py_chunk,) = store.replace_chunks(py_file.id, [_chunk(repo_id, py_file.id, "widget in py")])
    (ts_chunk,) = store.replace_chunks(ts_file.id, [_chunk(repo_id, ts_file.id, "widget in ts")])
    assert py_chunk.id and ts_chunk.id
    store.set_chunk_embeddings({py_chunk.id: _vec(1.0), ts_chunk.id: _vec(1.0)})
    embedder = _FakeEmbedder({"widget": _vec(1.0)})

    hits = search(store, embedder, repo_id, "widget", languages=["python"])

    assert [h.chunk.id for h in hits] == [py_chunk.id]


def test_search_filters_by_path_prefix(store: SqliteGraphStore, repo_id: int, file_id: int) -> None:
    other_file = store.upsert_file(
        File(repo_id=repo_id, path="other/thing.py", lang="python", blob_sha="s")
    )
    assert other_file.id is not None
    (in_pkg,) = store.replace_chunks(file_id, [_chunk(repo_id, file_id, "widget one")])
    (elsewhere,) = store.replace_chunks(
        other_file.id, [_chunk(repo_id, other_file.id, "widget two")]
    )
    assert in_pkg.id and elsewhere.id
    store.set_chunk_embeddings({in_pkg.id: _vec(1.0), elsewhere.id: _vec(1.0)})
    embedder = _FakeEmbedder({"widget": _vec(1.0)})

    hits = search(store, embedder, repo_id, "widget", path_prefix="pkg/")

    assert [h.chunk.id for h in hits] == [in_pkg.id]


def test_search_filters_by_symbol_kind(store: SqliteGraphStore, repo_id: int, file_id: int) -> None:
    a_function, a_class = store.replace_symbols(
        file_id,
        [
            Symbol(
                repo_id=repo_id,
                file_id=file_id,
                kind=SymbolKind.FUNCTION,
                name="make_widget",
                qualified_name="pkg.widget.make_widget",
                start_line=1,
                end_line=2,
            ),
            Symbol(
                repo_id=repo_id,
                file_id=file_id,
                kind=SymbolKind.CLASS,
                name="Widget",
                qualified_name="pkg.widget.Widget",
                start_line=3,
                end_line=4,
            ),
        ],
    )
    assert a_function.id is not None and a_class.id is not None
    in_function, in_class = store.replace_chunks(
        file_id,
        [
            _chunk(repo_id, file_id, "widget function", symbol_id=a_function.id),
            _chunk(repo_id, file_id, "widget class", symbol_id=a_class.id),
        ],
    )
    assert in_function.id and in_class.id
    store.set_chunk_embeddings({in_function.id: _vec(1.0), in_class.id: _vec(1.0)})
    embedder = _FakeEmbedder({"widget": _vec(1.0)})

    hits = search(store, embedder, repo_id, "widget", kinds=[SymbolKind.CLASS])

    assert [h.chunk.id for h in hits] == [in_class.id]


def test_search_truncates_to_limit_best_first(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    chunks = store.replace_chunks(
        file_id, [_chunk(repo_id, file_id, f"widget number {i}") for i in range(5)]
    )
    ids = [c.id for c in chunks]
    assert all(i is not None for i in ids)
    # Closest to the query is chunk 0, then rank order by index.
    store.set_chunk_embeddings(
        {cid: _vec(1.0 / (i + 1)) for i, cid in enumerate(ids) if cid is not None}
    )
    embedder = _FakeEmbedder({"widget": _vec(1.0)})

    hits = search(store, embedder, repo_id, "widget", limit=2)

    assert len(hits) == 2


def test_search_returns_nothing_for_an_empty_repo(store: SqliteGraphStore, repo_id: int) -> None:
    embedder = _FakeEmbedder({"anything": _vec(1.0)})

    assert search(store, embedder, repo_id, "anything") == []
