"""End-to-end proof that the real pipeline -- discover, tree-sitter parse,
resolve, persist, traverse -- handles genuinely circular Python source
without hanging or producing a nonsensical graph.

tests/unit/test_traversal.py already proves the store-level recursive CTE
terminates on a synthetic, hand-built cyclic graph; this file proves the
same property holds when that graph comes from real source through the
whole pipeline, which is a different risk (the parser or resolver could
in principle fail to even construct a cycle correctly, independent of
whether the store's own traversal is sound).
"""

from __future__ import annotations

from pathlib import Path

from repomind.analyze.reverse import find_references
from repomind.index.pipeline import index_repository
from repomind.model import EdgeKind
from repomind.store.sqlite.graph import SqliteGraphStore
from repomind.workspace import index_db_path, normalize_repo_path


def test_indexing_a_circular_import_repo_completes(
    isolated_workspace: Path, cyclic_fixture_repo: Path
) -> None:
    """The bare minimum: this must return at all. A hang here would time
    out the test suite rather than fail it cleanly, which is exactly why
    this is worth asserting explicitly rather than trusting it implicitly.
    """
    result = index_repository(cyclic_fixture_repo)
    assert result.files_indexed == 4  # __init__, a, b, animals
    assert result.edge_counts["heuristic"] > 0


def test_reverse_deps_of_a_through_the_real_cycle_terminates_and_excludes_a(
    isolated_workspace: Path, cyclic_fixture_repo: Path
) -> None:
    """a.py and b.py mutually import and call each other, so walking
    backward from A's class symbol re-encounters A itself within
    MAX_DEPTH -- the real-source equivalent of
    test_traversal.py::test_cycle_terminates_and_never_relists_the_seed.
    Must terminate, and A must never appear among its own dependents.
    """
    index_repository(cyclic_fixture_repo)

    root_path = normalize_repo_path(cyclic_fixture_repo)
    store = SqliteGraphStore(index_db_path(root_path))
    try:
        repo = store.get_repo_by_path(root_path)
        assert repo is not None and repo.id is not None

        # No "cyclic." prefix: the fixture directory itself is the indexed
        # root, so a.py's own qualified name is "a", not "cyclic.a" -- see
        # tests/fixtures/cyclic/__init__.py.
        result = find_references(store, repo.id, "a.A", depth=4)
        assert result is not None
        assert not any(h.source.qualified_name == "a.A" for h in result.hits)

        # B.use_a genuinely calls and references A -- confirms the cycle
        # is real, not just "terminates because there was nothing there".
        names = {h.source.qualified_name for h in result.hits}
        assert "b.B.use_a" in names
    finally:
        store.close()


def test_inheritance_chain_is_traversed_across_multiple_hops(
    isolated_workspace: Path, cyclic_fixture_repo: Path
) -> None:
    """Animal <- Dog <- Puppy: Puppy must be reachable from Animal at
    depth 2 via INHERITS, not just Dog at depth 1.
    """
    index_repository(cyclic_fixture_repo)

    root_path = normalize_repo_path(cyclic_fixture_repo)
    store = SqliteGraphStore(index_db_path(root_path))
    try:
        repo = store.get_repo_by_path(root_path)
        assert repo is not None and repo.id is not None

        result = find_references(store, repo.id, "animals.Animal", depth=2)
        assert result is not None
        inherits_hits = {
            h.source.qualified_name: h.depth for h in result.hits if h.kind == EdgeKind.INHERITS
        }
        assert inherits_hits.get("animals.Dog") == 1
        assert inherits_hits.get("animals.Puppy") == 2
    finally:
        store.close()
