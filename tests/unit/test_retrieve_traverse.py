"""Tests for retrieve/traverse.py (RM-035): bounded, bidirectional graph
expansion from a seed set of symbols.

Same ``_Graph`` builder style as tests/unit/test_traversal.py (RM-024's
own recursive-CTE traversal), kept local to this file rather than
shared -- docs/conventions.md "one concept per module", the same reason
test_traversal.py itself gives for not merging into test_store_sqlite.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repomind.model import Edge, EdgeKind, File, Repo, Symbol, SymbolKind, Tier
from repomind.retrieve.traverse import expand
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

    def expand_from(
        self, *names: str, max_hops: int = 2, tiers: tuple[Tier, ...] | None = None
    ) -> tuple[dict[str, int], list[tuple[str, str, int]]]:
        """Runs expand() from the named seeds and translates the result
        back to names: ``(expanded_name -> hop, [(src_name, dst_name,
        hop), ...])`` for easy comparison against an expected set.
        """
        id_to_name = {v: k for k, v in self._by_name.items()}
        result = expand(self.store, (self.id_of(n) for n in names), max_hops=max_hops, tiers=tiers)
        expanded = {id_to_name[sid]: es.hop for sid, es in result.expanded.items()}
        steps = [
            (id_to_name[step.edge.src_symbol_id], id_to_name[step.edge.dst_symbol_id], step.hop)
            for step in result.steps
        ]
        return expanded, steps


def test_a_direct_callee_is_found_at_hop_one(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A", "B")
    g.edge("A", "B")

    expanded, steps = g.expand_from("A")

    assert expanded == {"B": 1}
    assert steps == [("A", "B", 1)]


def test_a_direct_caller_is_found_too_expansion_is_bidirectional(
    store: SqliteGraphStore,
) -> None:
    g = _Graph(store)
    g.symbols("A", "B")
    g.edge("A", "B")  # A calls B

    expanded, _steps = g.expand_from("B")  # expand from the callee

    assert expanded == {"A": 1}  # its caller, found via edges_to


def test_two_hops_reaches_a_transitive_neighbour(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A", "B", "C")
    g.edge("A", "B")
    g.edge("B", "C")

    expanded, _steps = g.expand_from("A", max_hops=2)

    assert expanded == {"B": 1, "C": 2}


def test_max_hops_caps_how_far_expansion_reaches(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A", "B", "C")
    g.edge("A", "B")
    g.edge("B", "C")

    expanded, _steps = g.expand_from("A", max_hops=1)

    assert expanded == {"B": 1}


def test_a_symbol_with_no_edges_expands_to_nothing(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A")

    expanded, steps = g.expand_from("A")

    assert expanded == {}
    assert steps == []


def test_multiple_seeds_expand_independently(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A", "B", "C", "D")
    g.edge("A", "B")
    g.edge("C", "D")

    expanded, _steps = g.expand_from("A", "C")

    assert expanded == {"B": 1, "D": 1}


def test_a_two_way_cycle_terminates_and_does_not_relabel_a_seed(
    store: SqliteGraphStore,
) -> None:
    """F-7's cycle-safety requirement, applied here too: A and B call each
    other. A is a seed; B must be found once, at hop 1 -- not looped over,
    and never listed among ``expanded`` if it were the seed instead.
    """
    g = _Graph(store)
    g.symbols("A", "B")
    g.edge("A", "B")
    g.edge("B", "A")

    expanded, steps = g.expand_from("A", max_hops=2)

    assert expanded == {"B": 1}  # not relabeled at hop 2 via the back-edge
    assert sorted(steps) == sorted([("A", "B", 1), ("B", "A", 1)])


def test_an_edge_between_two_seeds_is_still_reported_once(store: SqliteGraphStore) -> None:
    """A and B are both seeds already, and A calls B: nothing new is
    *discovered*, but the edge is real structure worth surfacing (F-6
    requirement 4's traversal path) -- and must appear exactly once, not
    twice from being visible via both A's outgoing and B's incoming scan.
    """
    g = _Graph(store)
    g.symbols("A", "B")
    g.edge("A", "B")

    expanded, steps = g.expand_from("A", "B")

    assert expanded == {}  # both already seeds, nothing new
    assert steps == [("A", "B", 1)]


def test_tiers_filters_out_edges_at_other_tiers(store: SqliteGraphStore) -> None:
    g = _Graph(store)
    g.symbols("A", "B", "C")
    g.edge("A", "B", tier=Tier.HEURISTIC)
    g.edge("A", "C", tier=Tier.RESOLVED)

    expanded, _steps = g.expand_from("A", tiers=(Tier.RESOLVED,))

    assert expanded == {"C": 1}


def test_diamond_shaped_graph_reports_the_shorter_hop_only(store: SqliteGraphStore) -> None:
    # A -> B -> D and A -> C -> D: D is reachable at hop 2 via either
    # route, but must be recorded once, at hop 2 -- not twice.
    g = _Graph(store)
    g.symbols("A", "B", "C", "D")
    g.edge("A", "B")
    g.edge("A", "C")
    g.edge("B", "D")
    g.edge("C", "D")

    expanded, _steps = g.expand_from("A", max_hops=2)

    assert expanded == {"B": 1, "C": 1, "D": 2}
