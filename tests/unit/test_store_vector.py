"""Tests for RM-032's :class:`~repomind.store.base.VectorStore` half of
``SqliteGraphStore``: chunk persistence, FTS5 sync, and sqlite-vec KNN
search.

Real ``sqlite-vec``/FTS5, never mocked -- both are cheap, deterministic,
and local (no model, no network), unlike RM-031's embedding tests, so
there is no reason not to exercise the genuine extension and virtual
tables here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repomind.model import Chunk, File, Repo
from repomind.store.sqlite.graph import SqliteGraphStore


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
    f = store.upsert_file(File(repo_id=repo_id, path="a.py", lang="python", blob_sha="s"))
    assert f.id is not None
    return f.id


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


def _vec(*values: float, dims: int = 384) -> list[float]:
    """A vector matching chunk_vec's real FLOAT[384] column (design.md
    section 4.2: bge-small-en-v1.5) -- sqlite-vec enforces this width
    directly and raises on a mismatch, confirmed directly rather than
    assumed to be lenient about it.
    """
    v = list(values) + [0.0] * (dims - len(values))
    return v[:dims]


# -- replace_chunks -----------------------------------------------------


def test_replace_chunks_persists_and_assigns_ids(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    chunks = [_chunk(repo_id, file_id, "def f(): pass"), _chunk(repo_id, file_id, "def g(): pass")]

    persisted = store.replace_chunks(file_id, chunks)

    assert len(persisted) == 2
    assert all(c.id is not None for c in persisted)
    assert store.count_chunks(repo_id) == 2


def test_replace_chunks_is_idempotent_on_reindex(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    store.replace_chunks(file_id, [_chunk(repo_id, file_id, "def f(): pass")])
    store.replace_chunks(file_id, [_chunk(repo_id, file_id, "def f(): pass, changed")])

    assert store.count_chunks(repo_id) == 1


def test_replace_chunks_allows_a_null_symbol_id_for_remainder_chunks(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    (persisted,) = store.replace_chunks(
        file_id, [_chunk(repo_id, file_id, "MODULE_CONST = 1", symbol_id=None)]
    )
    assert persisted.symbol_id is None


def test_get_chunk_returns_none_for_an_unknown_id(store: SqliteGraphStore) -> None:
    assert store.get_chunk(999) is None


def test_count_chunks_is_zero_for_a_repo_with_none(store: SqliteGraphStore, repo_id: int) -> None:
    assert store.count_chunks(repo_id) == 0


# -- FTS5 -----------------------------------------------------------------


def test_search_fts_finds_a_real_text_match(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    store.replace_chunks(
        file_id,
        [
            _chunk(repo_id, file_id, "def make_widget(name): return Widget(name)"),
            _chunk(repo_id, file_id, "class Animal: pass"),
        ],
    )

    hits = store.search_fts(repo_id, "widget")

    assert len(hits) == 1
    chunk, score = hits[0]
    assert "widget" in chunk.text.lower()
    assert isinstance(score, float)


def test_search_fts_respects_the_repo_id_filter(store: SqliteGraphStore) -> None:
    repo_a = store.upsert_repo(Repo(root_path="/a"))
    repo_b = store.upsert_repo(Repo(root_path="/b"))
    assert repo_a.id is not None and repo_b.id is not None
    file_a = store.upsert_file(File(repo_id=repo_a.id, path="a.py", lang="python", blob_sha="s"))
    file_b = store.upsert_file(File(repo_id=repo_b.id, path="b.py", lang="python", blob_sha="s"))
    assert file_a.id is not None and file_b.id is not None

    store.replace_chunks(file_a.id, [_chunk(repo_a.id, file_a.id, "widget in repo a")])
    store.replace_chunks(file_b.id, [_chunk(repo_b.id, file_b.id, "widget in repo b")])

    hits = store.search_fts(repo_a.id, "widget")

    assert len(hits) == 1
    assert "repo a" in hits[0][0].text


def test_search_fts_respects_limit(store: SqliteGraphStore, repo_id: int, file_id: int) -> None:
    store.replace_chunks(
        file_id, [_chunk(repo_id, file_id, f"widget number {i}") for i in range(5)]
    )

    assert len(store.search_fts(repo_id, "widget", limit=2)) == 2


def test_search_fts_finds_nothing_for_an_unmatched_query(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    store.replace_chunks(file_id, [_chunk(repo_id, file_id, "class Animal: pass")])
    assert store.search_fts(repo_id, "nonexistenttoken") == []


# -- sqlite-vec -------------------------------------------------------------


def test_search_vector_orders_by_distance_nearest_first(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    close, far = store.replace_chunks(
        file_id,
        [
            _chunk(repo_id, file_id, "close to the query"),
            _chunk(repo_id, file_id, "far from the query"),
        ],
    )
    assert close.id is not None and far.id is not None
    store.set_chunk_embeddings(
        {close.id: _vec(1.0, 0.0), far.id: _vec(0.0, 1.0)},
    )

    hits = store.search_vector(repo_id, _vec(0.9, 0.1))

    assert [chunk.id for chunk, _score in hits] == [close.id, far.id]
    assert hits[0][1] < hits[1][1]  # nearest first: smaller distance


def test_search_vector_respects_the_repo_id_filter(store: SqliteGraphStore) -> None:
    repo_a = store.upsert_repo(Repo(root_path="/a"))
    repo_b = store.upsert_repo(Repo(root_path="/b"))
    assert repo_a.id is not None and repo_b.id is not None
    file_a = store.upsert_file(File(repo_id=repo_a.id, path="a.py", lang="python", blob_sha="s"))
    file_b = store.upsert_file(File(repo_id=repo_b.id, path="b.py", lang="python", blob_sha="s"))
    assert file_a.id is not None and file_b.id is not None

    (chunk_a,) = store.replace_chunks(file_a.id, [_chunk(repo_a.id, file_a.id, "in repo a")])
    (chunk_b,) = store.replace_chunks(file_b.id, [_chunk(repo_b.id, file_b.id, "in repo b")])
    assert chunk_a.id is not None and chunk_b.id is not None
    store.set_chunk_embeddings({chunk_a.id: _vec(1.0, 0.0), chunk_b.id: _vec(1.0, 0.0)})

    hits = store.search_vector(repo_a.id, _vec(1.0, 0.0))

    assert [chunk.id for chunk, _score in hits] == [chunk_a.id]


def test_search_vector_respects_limit_as_k(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    chunks = store.replace_chunks(
        file_id, [_chunk(repo_id, file_id, f"chunk {i}") for i in range(5)]
    )
    store.set_chunk_embeddings({c.id: _vec(float(i)) for i, c in enumerate(chunks) if c.id})

    hits = store.search_vector(repo_id, _vec(0.0), limit=2)

    assert len(hits) == 2


def test_set_chunk_embeddings_can_overwrite_an_existing_vector(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    (chunk,) = store.replace_chunks(file_id, [_chunk(repo_id, file_id, "x")])
    assert chunk.id is not None

    store.set_chunk_embeddings({chunk.id: _vec(1.0, 0.0)})
    store.set_chunk_embeddings({chunk.id: _vec(0.0, 1.0)})  # must not raise (PK conflict)

    hits = store.search_vector(repo_id, _vec(0.0, 1.0))
    assert hits[0][1] == pytest.approx(0.0, abs=1e-6)


# -- cascade cleanup ----------------------------------------------------


def test_deleting_a_file_cleans_up_fts_and_vector_entries_too(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    (chunk,) = store.replace_chunks(file_id, [_chunk(repo_id, file_id, "widget factory")])
    assert chunk.id is not None
    store.set_chunk_embeddings({chunk.id: _vec(1.0, 0.0)})

    store.delete_file(file_id)

    assert store.count_chunks(repo_id) == 0
    assert store.search_fts(repo_id, "widget") == []
    assert store.search_vector(repo_id, _vec(1.0, 0.0)) == []
