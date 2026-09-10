"""Tests for analyze/reverse.py (RM-024): symbol resolution, depth
clamping, and tier grouping -- the presentation layer over
GraphStore.reverse_dependencies, whose own traversal correctness is
covered by tests/unit/test_traversal.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repomind.analyze.reverse import MAX_DEPTH, find_references, resolve_symbol_ref
from repomind.model import Edge, EdgeKind, File, Repo, Symbol, SymbolKind, Tier
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
    f = store.upsert_file(File(repo_id=repo_id, path="pkg/mod.py", lang="python", blob_sha="s"))
    assert f.id is not None
    return f.id


class _SymbolBuilder:
    """Accumulates symbols for one file and re-persists the full set on
    every call.

    Not a per-symbol ``replace_symbols`` call each time: that method's own
    contract (M1) is "delete every existing symbol for this file, insert
    these in its place" -- calling it once per symbol on the *same*
    ``file_id`` would silently wipe out every symbol added before it,
    exactly the bug this class exists to avoid (caught by every
    multi-symbol test in this file failing with "resolves to nothing"
    before this existed).
    """

    def __init__(self, store: SqliteGraphStore, repo_id: int, file_id: int) -> None:
        self._store = store
        self._repo_id = repo_id
        self._file_id = file_id
        self._pending: list[Symbol] = []
        self._ids: dict[str, int] = {}

    def add(self, qname: str, start_line: int, kind: SymbolKind = SymbolKind.FUNCTION) -> int:
        self._pending.append(
            Symbol(
                repo_id=self._repo_id,
                file_id=self._file_id,
                kind=kind,
                name=qname.rsplit(".", 1)[-1],
                qualified_name=qname,
                start_line=start_line,
                end_line=start_line,
            )
        )
        persisted = self._store.replace_symbols(self._file_id, self._pending)
        self._ids = {s.qualified_name: s.id for s in persisted if s.id is not None}
        return self._ids[qname]


@pytest.fixture
def builder(store: SqliteGraphStore, repo_id: int, file_id: int) -> _SymbolBuilder:
    return _SymbolBuilder(store, repo_id, file_id)


# -- resolve_symbol_ref -----------------------------------------------------


def test_resolves_by_qualified_name(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    sid = builder.add("pkg.mod.f", start_line=5)
    result = resolve_symbol_ref(store, repo_id, "pkg.mod.f")
    assert result is not None and result.id == sid


def test_resolves_by_file_and_line(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    sid = builder.add("pkg.mod.f", start_line=5)
    result = resolve_symbol_ref(store, repo_id, "pkg/mod.py:5")
    assert result is not None and result.id == sid


def test_file_line_inside_a_symbols_span_still_resolves(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    (sym,) = store.replace_symbols(
        file_id,
        [
            Symbol(
                repo_id=repo_id,
                file_id=file_id,
                kind=SymbolKind.FUNCTION,
                name="f",
                qualified_name="pkg.mod.f",
                start_line=5,
                end_line=12,
            )
        ],
    )
    result = resolve_symbol_ref(store, repo_id, "pkg/mod.py:9")  # inside the span, not its start
    assert result is not None and result.id == sym.id


def test_unresolvable_qualified_name_returns_none(store: SqliteGraphStore, repo_id: int) -> None:
    assert resolve_symbol_ref(store, repo_id, "nope.does.not.exist") is None


def test_file_line_for_a_nonexistent_file_falls_back_to_qualified_name_lookup(
    store: SqliteGraphStore, repo_id: int, file_id: int
) -> None:
    # "missing.py:5" isn't a real file -- and isn't a real qualified name
    # either, so this must fail closed (None), not raise.
    assert resolve_symbol_ref(store, repo_id, "missing.py:5") is None


def test_a_qualified_name_that_merely_contains_a_colon_like_string_is_unambiguous(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    # Python identifiers can never contain ":", so "pkg.mod.f" alone (no
    # colon at all) must go straight to qualified-name resolution.
    sid = builder.add("pkg.mod.f", start_line=1)
    result = resolve_symbol_ref(store, repo_id, "pkg.mod.f")
    assert result is not None and result.id == sid


# -- find_references ---------------------------------------------------


def test_unresolvable_ref_returns_none(store: SqliteGraphStore, repo_id: int) -> None:
    assert find_references(store, repo_id, "nope") is None


def test_resolved_symbol_with_no_dependents_returns_an_empty_not_none_result(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    builder.add("pkg.mod.f", start_line=1)
    result = find_references(store, repo_id, "pkg.mod.f")
    assert result is not None
    assert result.hits == []


def test_direct_caller_is_found_with_evidence(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    callee = builder.add("pkg.mod.callee", start_line=1)
    caller = builder.add("pkg.mod.caller", start_line=5)
    store.insert_edges(
        [
            Edge(
                repo_id=repo_id,
                src_symbol_id=caller,
                dst_symbol_id=callee,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
                evidence_file_id=file_id,
                evidence_line=7,
            )
        ]
    )
    result = find_references(store, repo_id, "pkg.mod.callee")
    assert result is not None
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.source.qualified_name == "pkg.mod.caller"
    assert hit.kind == EdgeKind.CALLS
    assert hit.tier == Tier.HEURISTIC
    assert hit.depth == 1
    assert hit.evidence_path == "pkg/mod.py"
    assert hit.evidence_line == 7


def test_edge_with_no_evidence_file_yields_none_path_not_a_crash(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    callee = builder.add("pkg.mod.callee", start_line=1)
    caller = builder.add("pkg.mod.caller", start_line=5)
    store.insert_edges(
        [
            Edge(
                repo_id=repo_id,
                src_symbol_id=caller,
                dst_symbol_id=callee,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
            )
        ]
    )
    result = find_references(store, repo_id, "pkg.mod.callee")
    assert result is not None
    assert result.hits[0].evidence_path is None


def test_depth_defaults_to_one(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    a = builder.add("a", start_line=1)
    b = builder.add("b", start_line=2)
    c = builder.add("c", start_line=3)
    store.insert_edges(
        [
            Edge(
                repo_id=repo_id,
                src_symbol_id=b,
                dst_symbol_id=c,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
            ),
            Edge(
                repo_id=repo_id,
                src_symbol_id=a,
                dst_symbol_id=b,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
            ),
        ]
    )
    result = find_references(store, repo_id, "c")  # depth not passed
    assert result is not None
    assert [h.source.qualified_name for h in result.hits] == ["b"]  # not "a", two hops away


def test_depth_is_clamped_to_the_maximum(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    ids = [builder.add(chr(ord("a") + i), start_line=i + 1) for i in range(6)]
    for i in range(5):
        store.insert_edges(
            [
                Edge(
                    repo_id=repo_id,
                    src_symbol_id=ids[i + 1],
                    dst_symbol_id=ids[i],
                    kind=EdgeKind.CALLS,
                    tier=Tier.HEURISTIC,
                )
            ]
        )
    # a chain of 5 hops from "f" down to "a"; asking for depth=99 must not
    # exceed MAX_DEPTH.
    result = find_references(store, repo_id, "a", depth=99)
    assert result is not None
    assert max(h.depth for h in result.hits) <= MAX_DEPTH
    assert max(h.depth for h in result.hits) == MAX_DEPTH  # actually reaches the cap here


def test_depth_below_one_is_clamped_up_to_one(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    a = builder.add("a", start_line=1)
    b = builder.add("b", start_line=2)
    store.insert_edges(
        [
            Edge(
                repo_id=repo_id,
                src_symbol_id=b,
                dst_symbol_id=a,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
            )
        ]
    )
    result = find_references(store, repo_id, "a", depth=0)
    assert result is not None
    assert len(result.hits) == 1  # depth=0 clamped to 1, not "no traversal at all"


def test_tier_filter_is_passed_through(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    a = builder.add("a", start_line=1)
    b = builder.add("b", start_line=2)
    c = builder.add("c", start_line=3)
    store.insert_edges(
        [
            Edge(
                repo_id=repo_id,
                src_symbol_id=a,
                dst_symbol_id=c,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
            ),
            Edge(
                repo_id=repo_id,
                src_symbol_id=b,
                dst_symbol_id=c,
                kind=EdgeKind.CALLS,
                tier=Tier.RESOLVED,
            ),
        ]
    )
    result = find_references(store, repo_id, "c", tiers=(Tier.RESOLVED,))
    assert result is not None
    assert {h.source.qualified_name for h in result.hits} == {"b"}


def test_by_tier_groups_correctly_even_when_hits_are_not_tier_consecutive(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    """The bug this guards against: hits is sorted depth-first, so the
    same tier's entries are *not* consecutive when they occur at
    different depths. A naive itertools.groupby(key=tier) over that list
    would silently produce more than one group per tier and, if collapsed
    into a dict, drop all but the last -- this must not happen.
    """
    seed = builder.add("seed", start_line=1)
    near_heuristic = builder.add("near_h", start_line=2)
    near_resolved = builder.add("near_r", start_line=3)
    far_heuristic = builder.add("far_h", start_line=4)

    store.insert_edges(
        [
            # depth 1: one heuristic, one resolved
            Edge(
                repo_id=repo_id,
                src_symbol_id=near_heuristic,
                dst_symbol_id=seed,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
            ),
            Edge(
                repo_id=repo_id,
                src_symbol_id=near_resolved,
                dst_symbol_id=seed,
                kind=EdgeKind.CALLS,
                tier=Tier.RESOLVED,
            ),
            # depth 2: another heuristic, sorted *after* the depth-1
            # resolved entry in the flat list -- so "heuristic" is split
            # across two non-adjacent positions.
            Edge(
                repo_id=repo_id,
                src_symbol_id=far_heuristic,
                dst_symbol_id=near_heuristic,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
            ),
        ]
    )
    result = find_references(store, repo_id, "seed", depth=2)
    assert result is not None

    grouped = result.by_tier()
    heuristic_names = {h.source.qualified_name for h in grouped[Tier.HEURISTIC]}
    resolved_names = {h.source.qualified_name for h in grouped[Tier.RESOLVED]}
    assert heuristic_names == {"near_h", "far_h"}  # both present, not just the last-seen one
    assert resolved_names == {"near_r"}


def test_hits_are_sorted_by_depth_then_tier_then_name(
    store: SqliteGraphStore, repo_id: int, file_id: int, builder: _SymbolBuilder
) -> None:
    seed = builder.add("seed", start_line=1)
    z_near = builder.add("z_near", start_line=2)
    a_near = builder.add("a_near", start_line=3)
    far = builder.add("far", start_line=4)
    store.insert_edges(
        [
            Edge(
                repo_id=repo_id,
                src_symbol_id=z_near,
                dst_symbol_id=seed,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
            ),
            Edge(
                repo_id=repo_id,
                src_symbol_id=a_near,
                dst_symbol_id=seed,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
            ),
            Edge(
                repo_id=repo_id,
                src_symbol_id=far,
                dst_symbol_id=z_near,
                kind=EdgeKind.CALLS,
                tier=Tier.HEURISTIC,
            ),
        ]
    )
    result = find_references(store, repo_id, "seed", depth=2)
    assert result is not None
    names_in_order = [h.source.qualified_name for h in result.hits]
    assert names_in_order == ["a_near", "z_near", "far"]  # depth 1 (alpha), then depth 2
