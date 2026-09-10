from __future__ import annotations

from pathlib import Path

import pytest
from repomind.errors import SchemaVersionError
from repomind.model import (
    Edge,
    EdgeKind,
    File,
    IndexRunStatus,
    Repo,
    Symbol,
    SymbolKind,
    Tier,
)
from repomind.store.sqlite.graph import CURRENT_SCHEMA_VERSION, SqliteGraphStore


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


def test_upsert_repo_is_keyed_by_root_path(store: SqliteGraphStore) -> None:
    first = store.upsert_repo(Repo(root_path="/proj"))
    second = store.upsert_repo(Repo(root_path="/proj", indexed_sha="abc"))
    assert first.id == second.id
    assert store.get_repo(first.id).indexed_sha == "abc"  # type: ignore[union-attr]


def test_upsert_file_is_keyed_by_repo_and_path(store: SqliteGraphStore, repo_id: int) -> None:
    first = store.upsert_file(File(repo_id=repo_id, path="a.py", lang="python", blob_sha="s1"))
    second = store.upsert_file(File(repo_id=repo_id, path="a.py", lang="python", blob_sha="s2"))
    assert first.id == second.id
    assert store.get_file(first.id).blob_sha == "s2"  # type: ignore[union-attr]


def test_replace_symbols_deletes_previous_set(store: SqliteGraphStore, repo_id: int) -> None:
    f = store.upsert_file(File(repo_id=repo_id, path="a.py", lang="python", blob_sha="s"))
    assert f.id is not None

    store.replace_symbols(
        f.id,
        [
            Symbol(
                repo_id=repo_id,
                file_id=f.id,
                kind=SymbolKind.FUNCTION,
                name="old",
                qualified_name="a.old",
                start_line=1,
                end_line=2,
            )
        ],
    )
    store.replace_symbols(
        f.id,
        [
            Symbol(
                repo_id=repo_id,
                file_id=f.id,
                kind=SymbolKind.FUNCTION,
                name="new",
                qualified_name="a.new",
                start_line=1,
                end_line=2,
            )
        ],
    )

    names = {s.qualified_name for s in store.list_symbols(repo_id, file_id=f.id)}
    assert names == {"a.new"}


def test_insert_edges_deduplicates_on_natural_key(store: SqliteGraphStore, repo_id: int) -> None:
    f = store.upsert_file(File(repo_id=repo_id, path="a.py", lang="python", blob_sha="s"))
    assert f.id is not None
    syms = store.replace_symbols(
        f.id,
        [
            Symbol(
                repo_id=repo_id,
                file_id=f.id,
                kind=SymbolKind.FUNCTION,
                name="a",
                qualified_name="a",
                start_line=1,
                end_line=1,
            ),
            Symbol(
                repo_id=repo_id,
                file_id=f.id,
                kind=SymbolKind.FUNCTION,
                name="b",
                qualified_name="b",
                start_line=2,
                end_line=2,
            ),
        ],
    )
    edge = Edge(
        repo_id=repo_id,
        src_symbol_id=syms[1].id,
        dst_symbol_id=syms[0].id,
        kind=EdgeKind.CALLS,
        tier=Tier.HEURISTIC,
    )
    first = store.insert_edges([edge])
    second = store.insert_edges([edge])  # same natural key again

    assert first[0].id == second[0].id
    assert store.count_edges_by_tier(repo_id)["heuristic"] == 1


def test_edges_of_different_tiers_do_not_collide(store: SqliteGraphStore, repo_id: int) -> None:
    """The UNIQUE constraint is (src, dst, kind, tier) -- the same
    relationship discovered at two different tiers must coexist as two
    distinct edges, never merged (AGENTS.md invariant 3).
    """
    f = store.upsert_file(File(repo_id=repo_id, path="a.py", lang="python", blob_sha="s"))
    assert f.id is not None
    syms = store.replace_symbols(
        f.id,
        [
            Symbol(
                repo_id=repo_id,
                file_id=f.id,
                kind=SymbolKind.FUNCTION,
                name="a",
                qualified_name="a",
                start_line=1,
                end_line=1,
            ),
            Symbol(
                repo_id=repo_id,
                file_id=f.id,
                kind=SymbolKind.FUNCTION,
                name="b",
                qualified_name="b",
                start_line=2,
                end_line=2,
            ),
        ],
    )
    store.insert_edges(
        [
            Edge(
                repo_id=repo_id,
                src_symbol_id=syms[1].id,
                dst_symbol_id=syms[0].id,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
            ),
            Edge(
                repo_id=repo_id,
                src_symbol_id=syms[1].id,
                dst_symbol_id=syms[0].id,
                kind=EdgeKind.CALLS,
                tier=Tier.RESOLVED,
            ),
        ]
    )
    counts = store.count_edges_by_tier(repo_id)
    assert counts["heuristic"] == 1
    assert counts["resolved"] == 1


def test_count_symbols_by_kind_prefills_zero_for_absent_kinds(
    store: SqliteGraphStore, repo_id: int
) -> None:
    """AGENTS.md invariant 3's mechanism: an absent tier/kind is reported
    as 0, explicitly, never simply missing from the output.
    """
    counts = store.count_symbols_by_kind(repo_id)
    assert counts == {k.value: 0 for k in SymbolKind}


def test_count_edges_by_tier_prefills_zero_for_absent_tiers(
    store: SqliteGraphStore, repo_id: int
) -> None:
    counts = store.count_edges_by_tier(repo_id)
    assert counts == {t.value: 0 for t in Tier}


def test_set_symbol_scip_ids_backfills_in_place_without_disturbing_ids(
    store: SqliteGraphStore, repo_id: int
) -> None:
    """RM-022: backfilling scip_symbol must update the existing row, not
    delete-and-reinsert (as replace_symbols does) -- an edge already
    persisted against this symbol's id would otherwise be silently
    orphaned. Asserted by checking the id is unchanged, not just that
    scip_symbol ends up set.
    """
    f = store.upsert_file(File(repo_id=repo_id, path="a.py", lang="python", blob_sha="s"))
    assert f.id is not None
    (persisted,) = store.replace_symbols(
        f.id,
        [
            Symbol(
                repo_id=repo_id,
                file_id=f.id,
                kind=SymbolKind.FUNCTION,
                name="f",
                qualified_name="a.f",
                start_line=1,
                end_line=1,
            )
        ],
    )
    assert persisted.id is not None

    store.set_symbol_scip_ids({persisted.id: "scip-python python . . a/f()."})

    reloaded = store.get_symbol(persisted.id)
    assert reloaded is not None
    assert reloaded.id == persisted.id
    assert reloaded.scip_symbol == "scip-python python . . a/f()."


def test_set_symbol_scip_ids_with_empty_mapping_does_nothing(
    store: SqliteGraphStore, repo_id: int
) -> None:
    store.set_symbol_scip_ids({})  # must not raise


def test_cascade_delete_file_removes_its_symbols(store: SqliteGraphStore, repo_id: int) -> None:
    f = store.upsert_file(File(repo_id=repo_id, path="a.py", lang="python", blob_sha="s"))
    assert f.id is not None
    store.replace_symbols(
        f.id,
        [
            Symbol(
                repo_id=repo_id,
                file_id=f.id,
                kind=SymbolKind.FUNCTION,
                name="a",
                qualified_name="a",
                start_line=1,
                end_line=1,
            )
        ],
    )
    store.delete_file(f.id)
    assert store.list_symbols(repo_id) == []


def test_index_run_lifecycle(store: SqliteGraphStore, repo_id: int) -> None:
    run = store.start_index_run(repo_id, from_sha=None, to_sha="deadbeef", files_total=3)
    assert run.id is not None
    assert run.status == IndexRunStatus.RUNNING

    store.update_index_run_progress(run.id, 2)
    latest = store.get_latest_index_run(repo_id)
    assert latest is not None
    assert latest.files_done == 2

    store.finish_index_run(run.id, IndexRunStatus.OK)
    latest = store.get_latest_index_run(repo_id)
    assert latest is not None
    assert latest.status == IndexRunStatus.OK
    assert latest.finished_at is not None


def test_reopening_a_current_schema_database_does_not_raise(tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    SqliteGraphStore(db).close()
    SqliteGraphStore(db).close()  # must not raise


def test_mismatched_schema_version_raises(tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    SqliteGraphStore(db).close()

    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    conn.close()

    with pytest.raises(SchemaVersionError):
        SqliteGraphStore(db)
