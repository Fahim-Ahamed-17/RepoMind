"""Tests for GraphStore.reverse_dependencies (RM-024) -- the recursive-CTE
multi-hop traversal design.md section 4.4 describes.

A separate file from test_store_sqlite.py: building a graph of several
symbols and edges for each case is enough setup on its own that mixing it
with the CRUD-level tests there would make both harder to scan. See
docs/conventions.md "one concept per module", applied to tests as
elsewhere in this suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repomind.model import Edge, EdgeKind, File, Repo, Symbol, SymbolKind, Tier
from repomind.store.sqlite.graph import SqliteGraphStore


@pytest.fixture
def store(tmp_path: Path) -> SqliteGraphStore:
    s = SqliteGraphStore(tmp_path / "index.db")
    yield s
    s.close()


class _Graph:
    """Builds a small named-symbol graph in one store, so a test reads as
    "A calls B" rather than a wall of Symbol/Edge construction.
    """

    def __init__(self, store: SqliteGraphStore) -> None:
        self.store = store
        repo = store.upsert_repo(Repo(root_path="/proj"))
        assert repo.id is not None
        self.repo_id = repo.id
        f = store.upsert_file(File(repo_id=repo.id, path="a.py", lang="python", blob_sha="s"))
        assert f.id is not None
        self.file_id = f.id
        self._by_name: dict[str, int] = {}

    def symbols(self, *names: str) -> None:
        rows = [
            Symbol(
                repo_id=self.repo_id,
                file_id=self.file_id,
                kind=SymbolKind.FUNCTION,
                name=n,
                qualified_name=n,
                start_line=i + 1,
                end_line=i + 1,
            )
            for i, n in enumerate(names)
        ]
        for sym in self.store.replace_symbols(self.file_id, rows):
            assert sym.id is not None
            self._by_name[sym.name] = sym.id

    def id_of(self, name: str) -> int:
        return self._by_name[name]

    def edge(
        self, src: str, dst: str, kind: EdgeKind = EdgeKind.CALLS, tier: Tier = Tier.HEURISTIC
    ) -> None:
        self.store.insert_edges(
            [
                Edge(
                    repo_id=self.repo_id,
                    src_symbol_id=self.id_of(src),
                    dst_symbol_id=self.id_of(dst),
                    kind=kind,
                    tier=tier,
                )
            ]
        )

    def reverse_deps(
        self, of: str, max_depth: int = 4, tiers: tuple[Tier, ...] | None = None
    ) -> list[tuple[str, int, EdgeKind]]:
        """(source symbol name, depth, edge kind) triples, sorted, for
        easy comparison against an expected set.
        """
        id_to_name = {v: k for k, v in self._by_name.items()}
        results = self.store.reverse_dependencies(self.id_of(of), max_depth, tiers)
        return sorted((id_to_name[edge.src_symbol_id], depth, edge.kind) for edge, depth in results)


def test_direct_caller_at_depth_one(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A", "B")
    g.edge("A", "B")
    assert g.reverse_deps("B") == [("A", 1, EdgeKind.CALLS)]


def test_no_callers_yields_empty(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A")
    assert g.reverse_deps("A") == []


def test_transitive_caller_at_depth_two(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A", "B", "C")
    g.edge("A", "B")
    g.edge("B", "C")
    assert g.reverse_deps("C") == [("A", 2, EdgeKind.CALLS), ("B", 1, EdgeKind.CALLS)]


def test_depth_cap_excludes_anything_farther(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A", "B", "C")
    g.edge("A", "B")
    g.edge("B", "C")
    assert g.reverse_deps("C", max_depth=1) == [("B", 1, EdgeKind.CALLS)]


def test_diamond_reports_the_shorter_path_only(store: SqliteGraphStore) -> None:
    # A -> B -> D and A -> C -> D: D's reverse deps include B and C at
    # depth 1, and A at depth 2 (via either route) -- but only once, not
    # twice for the two routes that both reach it at the same depth.
    g = _Graph(store)
    g.symbols("A", "B", "C", "D")
    g.edge("A", "B")
    g.edge("A", "C")
    g.edge("B", "D")
    g.edge("C", "D")
    result = g.reverse_deps("D")
    assert ("B", 1, EdgeKind.CALLS) in result
    assert ("C", 1, EdgeKind.CALLS) in result
    assert result.count(("A", 2, EdgeKind.CALLS)) == 1


def test_shortest_path_wins_over_a_longer_one_to_the_same_symbol(store: SqliteGraphStore) -> None:
    # A -> D directly (depth 1), and also A -> B -> C -> D (depth 3). Only
    # the depth-1 route should appear for A.
    g = _Graph(store)
    g.symbols("A", "B", "C", "D")
    g.edge("A", "D")
    g.edge("A", "B")
    g.edge("B", "C")
    g.edge("C", "D")
    result = g.reverse_deps("D")
    a_rows = [r for r in result if r[0] == "A"]
    assert a_rows == [("A", 1, EdgeKind.CALLS)]


def test_cycle_terminates_and_never_relists_the_seed(store: SqliteGraphStore) -> None:
    """The case design.md section 4.4 and the store method's own docstring
    both call out by name: A -> B -> C -> A. Must terminate (this test
    would hang forever on an unbounded traversal) and must not include A
    among "what calls A", even though the cycle makes that transitively
    true -- see reverse_dependencies' docstring for why.
    """
    g = _Graph(store)
    g.symbols("A", "B", "C", "D")
    g.edge("A", "B")
    g.edge("B", "C")
    g.edge("C", "A")  # closes the cycle
    g.edge("D", "A")  # an ordinary caller, for a non-degenerate result

    result = g.reverse_deps("A", max_depth=10)

    assert ("A", 0, EdgeKind.CALLS) not in result  # depth 0 never appears at all
    assert not any(name == "A" for name, _depth, _kind in result)
    assert ("C", 1, EdgeKind.CALLS) in result
    assert ("D", 1, EdgeKind.CALLS) in result
    assert ("B", 2, EdgeKind.CALLS) in result


def test_self_loop_produces_no_result(store: SqliteGraphStore) -> None:
    """A degenerate one-symbol cycle (A calls A) -- the same
    depth-0-always-wins rule applies at its smallest possible case."""
    g = _Graph(store)
    g.symbols("A")
    g.edge("A", "A")
    assert g.reverse_deps("A") == []


def test_multiple_call_sites_at_the_same_depth_both_appear(store: SqliteGraphStore) -> None:
    # Two distinct edges, both A -> B, of different kinds (as if A both
    # imports and calls B) -- both are genuinely separate evidence and
    # both belong in the result.
    g = _Graph(store)
    g.symbols("A", "B")
    g.edge("A", "B", kind=EdgeKind.CALLS)
    g.edge("A", "B", kind=EdgeKind.IMPORTS)
    result = g.reverse_deps("B")
    assert ("A", 1, EdgeKind.CALLS) in result
    assert ("A", 1, EdgeKind.IMPORTS) in result
    assert len(result) == 2


def test_tier_filter_excludes_edges_of_other_tiers(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A", "B", "C")
    g.edge("A", "B", tier=Tier.HEURISTIC)
    g.edge("C", "B", tier=Tier.RESOLVED)
    assert g.reverse_deps("B", tiers=(Tier.RESOLVED,)) == [("C", 1, EdgeKind.CALLS)]


def test_tier_filter_also_bounds_transitive_hops_not_just_the_first(
    store: SqliteGraphStore,
) -> None:
    # A -> B is heuristic; B -> C is resolved. Restricting to resolved-only
    # must stop the walk at the heuristic hop, not just filter it from the
    # *output* -- so C's reverse deps restricted to resolved contain only
    # B, never A.
    g = _Graph(store)
    g.symbols("A", "B", "C")
    g.edge("A", "B", tier=Tier.HEURISTIC)
    g.edge("B", "C", tier=Tier.RESOLVED)
    result = g.reverse_deps("C", tiers=(Tier.RESOLVED,))
    assert result == [("B", 1, EdgeKind.CALLS)]


def test_default_tiers_none_includes_every_tier(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A", "B", "C")
    g.edge("A", "B", tier=Tier.HEURISTIC)
    g.edge("C", "B", tier=Tier.RESOLVED)
    result = g.reverse_deps("B")  # tiers=None
    assert {r[0] for r in result} == {"A", "C"}


def test_different_edge_kinds_all_count_as_dependencies(store: SqliteGraphStore) -> None:
    """F-8's impact analysis reuses this same primitive -- an INHERITS or
    REFERENCES edge is just as much "this depends on that" as CALLS.
    """
    g = _Graph(store)
    g.symbols("A", "B", "C")
    g.edge("A", "B", kind=EdgeKind.INHERITS)
    g.edge("C", "B", kind=EdgeKind.REFERENCES)
    result = g.reverse_deps("B")
    assert ("A", 1, EdgeKind.INHERITS) in result
    assert ("C", 1, EdgeKind.REFERENCES) in result
