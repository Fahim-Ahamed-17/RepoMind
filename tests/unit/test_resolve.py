"""Tests for RM-021's heuristic resolver.

Builds small, explicit symbol tables and reference lists by hand rather
than going through the full parser+pipeline -- this module tests the
*matching rules* in isolation, at the boundary the rest of the system
depends on: given references from N conceptual files, produce exactly the
heuristic edges the rules in resolve.py's own docstring promise, and
nothing else. Cross-file resolution through a real parse is covered
separately by tests/integration/test_pipeline.py.
"""

from __future__ import annotations

from repomind.index.resolve import (
    defines_edges_for_file,
    resolve_heuristic_edges,
    resolve_scip_edges,
)
from repomind.languages.python.scip import ScipIndex
from repomind.model import (
    EdgeKind,
    ParsedReference,
    ParsedSymbol,
    Symbol,
    SymbolKind,
    Tier,
)


def _sym(
    id_: int, qname: str, kind: SymbolKind = SymbolKind.FUNCTION, start_line: int = 1
) -> Symbol:
    return Symbol(
        id=id_,
        repo_id=1,
        file_id=1,
        kind=kind,
        name=qname.rsplit(".", 1)[-1],
        qualified_name=qname,
        start_line=start_line,
        end_line=start_line,
    )


def _ref(kind: EdgeKind, src: str, target: str, line: int = 1) -> ParsedReference:
    return ParsedReference(
        kind=kind, src_qualified_name=src, target_text=target, evidence_line=line
    )


def _resolve(symbols: list[Symbol], refs: list[ParsedReference], file_id: int = 1):
    return resolve_heuristic_edges(
        repo_id=1, all_symbols=symbols, file_references=[(file_id, refs)]
    )


def _edge_pairs(edges) -> set[tuple[int, int, EdgeKind]]:
    return {(e.src_symbol_id, e.dst_symbol_id, e.kind) for e in edges}


# -- step 1: exact qualified-name match -----------------------------------


def test_exact_qualified_name_match() -> None:
    symbols = [_sym(1, "pkg.mod.f"), _sym(2, "pkg.other.g")]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.f", "pkg.other.g")])
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.CALLS)}


def test_all_edges_are_heuristic_tier_with_evidence() -> None:
    symbols = [_sym(1, "pkg.mod.f"), _sym(2, "pkg.other.g")]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.f", "pkg.other.g", line=42)])
    assert len(edges) == 1
    e = edges[0]
    assert e.tier is Tier.HEURISTIC
    assert e.evidence_file_id == 1
    assert e.evidence_line == 42
    assert e.repo_id == 1


# -- step 2: ancestor-scope walk (bare names only) -------------------------


def test_bare_name_resolves_within_the_same_module() -> None:
    symbols = [_sym(1, "pkg.mod.f"), _sym(2, "pkg.mod.g")]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.f", "g")])
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.CALLS)}


def test_bare_name_resolves_within_the_same_class_over_a_farther_module_match() -> None:
    # Both pkg.mod.C.helper and pkg.mod.helper exist; a call from inside a
    # method of C should prefer its own class's member (nearer ancestor).
    symbols = [
        _sym(1, "pkg.mod.C.m"),
        _sym(2, "pkg.mod.C.helper"),
        _sym(3, "pkg.mod.helper"),
        _sym(4, "pkg.mod.C", kind=SymbolKind.CLASS),
    ]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.C.m", "helper")])
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.CALLS)}


def test_nested_function_resolves_against_its_own_scope_before_the_outer_ones() -> None:
    symbols = [
        _sym(1, "pkg.mod.outer.inner"),
        _sym(2, "pkg.mod.outer.inner.target"),
        _sym(3, "pkg.mod.outer.target"),
    ]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.outer.inner", "target")])
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.CALLS)}


# -- step 3: same-file imports -----------------------------------------


def test_bare_name_resolves_via_a_same_file_import() -> None:
    symbols = [_sym(1, "pkg.mod.f"), _sym(2, "other.make_widget")]
    refs = [
        _ref(EdgeKind.IMPORTS, "pkg.mod", "other.make_widget"),
        _ref(EdgeKind.CALLS, "pkg.mod.f", "make_widget"),
    ]
    edges = _resolve(symbols, refs)
    assert (1, 2, EdgeKind.CALLS) in _edge_pairs(edges)


def test_import_from_a_different_file_does_not_leak_into_this_ones_resolution() -> None:
    # file_id=2's import of "other.make_widget" must not help file_id=1's
    # bare `make_widget` call resolve -- same-file-import is per file. A
    # second same-bare-name candidate is required here so step 4
    # (unique-by-bare-name, a deliberately *repo-wide* fallback -- see
    # test_unique_by_bare_name_across_the_repo_is_used_as_a_last_resort)
    # cannot itself resolve this and mask what step 3 does.
    symbols = [
        _sym(1, "pkg.a.f"),
        _sym(2, "other.make_widget"),
        _sym(3, "another.make_widget"),
    ]
    refs_file_1 = [_ref(EdgeKind.CALLS, "pkg.a.f", "make_widget")]
    refs_file_2 = [_ref(EdgeKind.IMPORTS, "pkg.b", "other.make_widget")]
    edges = resolve_heuristic_edges(
        repo_id=1,
        all_symbols=symbols,
        file_references=[(1, refs_file_1), (2, refs_file_2)],
    )
    assert edges == []


# -- step 4: unique-by-bare-name fallback, and refusing to guess -----------


def test_unique_by_bare_name_across_the_repo_is_used_as_a_last_resort() -> None:
    symbols = [_sym(1, "pkg.a.f"), _sym(2, "pkg.unrelated.only_one_named_this")]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.a.f", "only_one_named_this")])
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.CALLS)}


def test_ambiguous_bare_name_across_the_repo_resolves_to_nothing() -> None:
    symbols = [
        _sym(1, "pkg.a.f"),
        _sym(2, "pkg.x.dup"),
        _sym(3, "pkg.y.dup"),
    ]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.a.f", "dup")])
    assert edges == []


def test_unresolvable_reference_produces_no_edge() -> None:
    symbols = [_sym(1, "pkg.a.f")]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.a.f", "nothing_matches_this")])
    assert edges == []


# -- dotted names: exact match only, never the fallback chain -------------


def test_dotted_target_that_matches_exactly_resolves() -> None:
    symbols = [_sym(1, "pkg.a.f"), _sym(2, "pkg.b.g")]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.a.f", "pkg.b.g")])
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.CALLS)}


def test_dotted_target_that_fails_exact_match_does_not_fall_back_to_bare_name() -> None:
    """The false-positive this rule exists to prevent: `mod.func()` should
    never resolve to some unrelated same-scope `func`, even though a plain
    bare `func()` call would legitimately reach it via the ancestor walk.
    """
    symbols = [_sym(1, "pkg.a.f"), _sym(2, "pkg.a.func")]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.a.f", "unrelated_mod.func")])
    assert edges == []


def test_dotted_target_does_not_fall_back_to_same_file_import_either() -> None:
    symbols = [_sym(1, "pkg.a.f"), _sym(2, "other.make_widget")]
    refs = [
        _ref(EdgeKind.IMPORTS, "pkg.a", "other.make_widget"),
        _ref(EdgeKind.CALLS, "pkg.a.f", "somewhere.make_widget"),
    ]
    edges = _resolve(symbols, refs)
    assert edges == []


# -- self / cls -----------------------------------------------------------


def test_self_dot_method_resolves_against_the_enclosing_class() -> None:
    symbols = [
        _sym(1, "pkg.mod.C", kind=SymbolKind.CLASS),
        _sym(2, "pkg.mod.C.m", kind=SymbolKind.METHOD),
        _sym(3, "pkg.mod.C.helper", kind=SymbolKind.METHOD),
    ]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.C.m", "self.helper")])
    assert _edge_pairs(edges) == {(2, 3, EdgeKind.CALLS)}


def test_cls_dot_method_resolves_the_same_way_as_self() -> None:
    symbols = [
        _sym(1, "pkg.mod.C", kind=SymbolKind.CLASS),
        _sym(2, "pkg.mod.C.m", kind=SymbolKind.METHOD),
        _sym(3, "pkg.mod.C.factory", kind=SymbolKind.METHOD),
    ]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.C.m", "cls.factory")])
    assert _edge_pairs(edges) == {(2, 3, EdgeKind.CALLS)}


def test_self_dot_method_from_a_nested_function_still_finds_the_class() -> None:
    symbols = [
        _sym(1, "pkg.mod.C", kind=SymbolKind.CLASS),
        _sym(2, "pkg.mod.C.m.inner", kind=SymbolKind.FUNCTION),
        _sym(3, "pkg.mod.C.helper", kind=SymbolKind.METHOD),
    ]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.C.m.inner", "self.helper")])
    assert _edge_pairs(edges) == {(2, 3, EdgeKind.CALLS)}


def test_self_dot_method_with_no_matching_member_produces_no_edge() -> None:
    symbols = [_sym(1, "pkg.mod.C", kind=SymbolKind.CLASS), _sym(2, "pkg.mod.C.m")]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.C.m", "self.nonexistent")])
    assert edges == []


def test_self_with_more_than_one_attribute_hop_is_not_resolved() -> None:
    symbols = [
        _sym(1, "pkg.mod.C", kind=SymbolKind.CLASS),
        _sym(2, "pkg.mod.C.m"),
        _sym(3, "pkg.mod.C.thing.call"),  # would require type inference either way
    ]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.C.m", "self.thing.call")])
    assert edges == []


# -- kind filtering ---------------------------------------------------------


def test_inherits_only_matches_a_class_not_a_same_named_function() -> None:
    symbols = [
        _sym(1, "pkg.mod.Foo", kind=SymbolKind.CLASS),
        _sym(2, "pkg.other.Base", kind=SymbolKind.FUNCTION),  # same bare name, wrong kind
    ]
    edges = _resolve(symbols, [_ref(EdgeKind.INHERITS, "pkg.mod.Foo", "Base")])
    assert edges == []


def test_inherits_matches_a_class_with_the_same_bare_name() -> None:
    symbols = [
        _sym(1, "pkg.mod.Foo", kind=SymbolKind.CLASS),
        _sym(2, "pkg.other.Base", kind=SymbolKind.CLASS),
    ]
    edges = _resolve(symbols, [_ref(EdgeKind.INHERITS, "pkg.mod.Foo", "Base")])
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.INHERITS)}


def test_calls_does_not_resolve_to_a_bare_variable() -> None:
    symbols = [_sym(1, "pkg.mod.f"), _sym(2, "pkg.mod.thing", kind=SymbolKind.VARIABLE)]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.f", "thing")])
    assert edges == []


def test_references_has_no_kind_restriction() -> None:
    symbols = [_sym(1, "pkg.mod.f"), _sym(2, "pkg.mod.SomeVar", kind=SymbolKind.VARIABLE)]
    edges = _resolve(symbols, [_ref(EdgeKind.REFERENCES, "pkg.mod.f", "SomeVar")])
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.REFERENCES)}


# -- imports ----------------------------------------------------------------


def test_imports_resolves_by_exact_match_only() -> None:
    symbols = [
        _sym(1, "pkg.a", kind=SymbolKind.MODULE),
        _sym(2, "pkg.b.Widget", kind=SymbolKind.CLASS),
    ]
    edges = _resolve(symbols, [_ref(EdgeKind.IMPORTS, "pkg.a", "pkg.b.Widget")])
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.IMPORTS)}


def test_imports_of_an_external_package_produces_no_edge() -> None:
    symbols = [_sym(1, "pkg.a", kind=SymbolKind.MODULE)]
    edges = _resolve(symbols, [_ref(EdgeKind.IMPORTS, "pkg.a", "numpy")])
    assert edges == []


# -- misc ---------------------------------------------------------------


def test_self_reference_produces_no_edge() -> None:
    # Degenerate but possible input (e.g. a class referencing itself in an
    # annotation) -- a symbol should never get an edge to itself.
    symbols = [_sym(1, "pkg.mod.f")]
    edges = _resolve(symbols, [_ref(EdgeKind.REFERENCES, "pkg.mod.f", "pkg.mod.f")])
    assert edges == []


def test_unknown_src_qualified_name_is_skipped_without_raising() -> None:
    symbols = [_sym(2, "pkg.mod.g")]
    edges = _resolve(symbols, [_ref(EdgeKind.CALLS, "pkg.mod.f_never_persisted", "g")])
    assert edges == []


def test_multiple_files_each_contribute_their_own_edges() -> None:
    symbols = [_sym(1, "pkg.a.f"), _sym(2, "pkg.a.g"), _sym(3, "pkg.b.h"), _sym(4, "pkg.b.i")]
    edges = resolve_heuristic_edges(
        repo_id=1,
        all_symbols=symbols,
        file_references=[
            (10, [_ref(EdgeKind.CALLS, "pkg.a.f", "g")]),
            (20, [_ref(EdgeKind.CALLS, "pkg.b.h", "i")]),
        ],
    )
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.CALLS), (3, 4, EdgeKind.CALLS)}
    file_ids = {e.evidence_file_id for e in edges}
    assert file_ids == {10, 20}


# -- defines ----------------------------------------------------------------


def _parsed_sym(
    qname: str, parent: str | None, kind: SymbolKind = SymbolKind.FUNCTION
) -> ParsedSymbol:
    return ParsedSymbol(
        kind=kind,
        name=qname.rsplit(".", 1)[-1],
        qualified_name=qname,
        start_line=3,
        end_line=5,
        parent_qualified_name=parent,
    )


def test_defines_edge_from_parent_to_child() -> None:
    parsed = [_parsed_sym("m", None, kind=SymbolKind.FILE), _parsed_sym("m.f", "m")]
    persisted = [_sym(10, "m", kind=SymbolKind.FILE), _sym(11, "m.f")]
    edges = defines_edges_for_file(repo_id=1, file_id=7, parsed_symbols=parsed, persisted=persisted)
    assert len(edges) == 1
    e = edges[0]
    assert (e.src_symbol_id, e.dst_symbol_id, e.kind) == (10, 11, EdgeKind.DEFINES)
    assert e.tier is Tier.HEURISTIC
    assert e.evidence_file_id == 7


def test_defines_edge_uses_the_childs_own_start_line_as_evidence() -> None:
    parsed = [_parsed_sym("m", None, kind=SymbolKind.FILE), _parsed_sym("m.f", "m")]
    persisted = [_sym(10, "m", kind=SymbolKind.FILE, start_line=1), _sym(11, "m.f", start_line=9)]
    edges = defines_edges_for_file(repo_id=1, file_id=7, parsed_symbols=parsed, persisted=persisted)
    assert edges[0].evidence_line == 9  # the child's own line, not the parent's or a fixed one


def test_defines_edges_cover_a_multi_level_hierarchy() -> None:
    # file -> class -> method, two separate defines edges, not one skipping
    # the class.
    parsed = [
        _parsed_sym("m", None, kind=SymbolKind.FILE),
        _parsed_sym("m.C", "m", kind=SymbolKind.CLASS),
        _parsed_sym("m.C.meth", "m.C", kind=SymbolKind.METHOD),
    ]
    persisted = [
        _sym(1, "m", kind=SymbolKind.FILE),
        _sym(2, "m.C", kind=SymbolKind.CLASS),
        _sym(3, "m.C.meth", kind=SymbolKind.METHOD),
    ]
    edges = defines_edges_for_file(repo_id=1, file_id=7, parsed_symbols=parsed, persisted=persisted)
    pairs = {(e.src_symbol_id, e.dst_symbol_id) for e in edges}
    assert pairs == {(1, 2), (2, 3)}


def test_file_symbol_itself_has_no_defines_edge_since_it_has_no_parent() -> None:
    parsed = [_parsed_sym("m", None, kind=SymbolKind.FILE)]
    persisted = [_sym(1, "m", kind=SymbolKind.FILE)]
    edges = defines_edges_for_file(repo_id=1, file_id=7, parsed_symbols=parsed, persisted=persisted)
    assert edges == []


# -- RM-022: resolve_scip_edges --------------------------------------------
#
# Unlike resolve_heuristic_edges, matching here is entirely by (path, line)
# lookup through a ScipIndex, never by name -- so these tests deliberately
# use target_text that would resolve *differently* (or not at all) under
# the heuristic rules, to prove the SCIP path is really not falling back to
# name matching under the hood.


def _scip_resolve(
    symbols: list[Symbol],
    refs: list[ParsedReference],
    scip_index: ScipIndex,
    scip_symbol_to_our_id: dict[str, int],
    file_id: int = 1,
    rel_path: str = "a.py",
):
    return resolve_scip_edges(
        repo_id=1,
        all_symbols=symbols,
        file_references=[(file_id, refs)],
        file_id_to_rel_path={file_id: rel_path},
        scip_index=scip_index,
        scip_symbol_to_our_id=scip_symbol_to_our_id,
    )


def test_scip_resolves_by_location_ignoring_what_the_name_would_suggest() -> None:
    # target_text "Wrong" would, under the heuristic rules, either fail to
    # resolve or resolve to the wrong same-named symbol -- SCIP's own
    # location-keyed answer is what actually wins here.
    symbols = [_sym(1, "pkg.mod.f"), _sym(2, "pkg.other.Right"), _sym(3, "pkg.mod.Wrong")]
    refs = [_ref(EdgeKind.CALLS, "pkg.mod.f", "Wrong", line=10)]
    # ParsedReference.evidence_line is 1-indexed; SCIP is 0-indexed.
    scip_index = ScipIndex(references={("a.py", 9): "scip-python python . . other/Right#"})
    edges = _scip_resolve(symbols, refs, scip_index, {"scip-python python . . other/Right#": 2})
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.CALLS)}


def test_scip_edges_are_resolved_tier() -> None:
    symbols = [_sym(1, "pkg.mod.f"), _sym(2, "pkg.mod.g")]
    refs = [_ref(EdgeKind.CALLS, "pkg.mod.f", "g", line=5)]
    scip_index = ScipIndex(references={("a.py", 4): "scip-sym-g"})
    edges = _scip_resolve(symbols, refs, scip_index, {"scip-sym-g": 2})
    assert len(edges) == 1
    assert edges[0].tier is Tier.RESOLVED
    assert edges[0].evidence_line == 5
    assert edges[0].evidence_file_id == 1


def test_scip_reference_with_no_data_at_that_line_produces_no_edge() -> None:
    symbols = [_sym(1, "pkg.mod.f"), _sym(2, "pkg.mod.g")]
    refs = [_ref(EdgeKind.CALLS, "pkg.mod.f", "g", line=5)]
    edges = _scip_resolve(symbols, refs, ScipIndex(), {})
    assert edges == []


def test_scip_target_outside_this_repo_produces_no_edge() -> None:
    # The scip symbol resolves to something real in SCIP's own view (e.g.
    # the stdlib), but scip_symbol_to_our_id has nothing for it because it
    # was never one of our own persisted symbols -- not an error, just
    # nothing to attach an edge to.
    symbols = [_sym(1, "pkg.mod.f")]
    refs = [_ref(EdgeKind.IMPORTS, "pkg.mod.f", "os.path", line=1)]
    scip_index = ScipIndex(references={("a.py", 0): "scip-python python stdlib . os/path#"})
    edges = _scip_resolve(symbols, refs, scip_index, {})
    assert edges == []


def test_scip_self_reference_produces_no_edge() -> None:
    symbols = [_sym(1, "pkg.mod.f")]
    refs = [_ref(EdgeKind.CALLS, "pkg.mod.f", "f", line=1)]
    scip_index = ScipIndex(references={("a.py", 0): "scip-sym-f"})
    edges = _scip_resolve(symbols, refs, scip_index, {"scip-sym-f": 1})
    assert edges == []


def test_scip_does_not_filter_by_symbol_kind_unlike_the_heuristic_tier() -> None:
    # A CALLS target that resolves to a VARIABLE -- the heuristic tier's
    # _CALLABLE_KINDS filter would refuse this (see test_resolve's own
    # CALLS tests), but a variable holding a callable is valid Python, and
    # SCIP's own type-aware answer should not be second-guessed here.
    symbols = [_sym(1, "pkg.mod.f"), _sym(2, "pkg.mod.handler", kind=SymbolKind.VARIABLE)]
    refs = [_ref(EdgeKind.CALLS, "pkg.mod.f", "handler", line=3)]
    scip_index = ScipIndex(references={("a.py", 2): "scip-sym-handler"})
    edges = _scip_resolve(symbols, refs, scip_index, {"scip-sym-handler": 2})
    assert _edge_pairs(edges) == {(1, 2, EdgeKind.CALLS)}
