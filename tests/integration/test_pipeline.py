"""End-to-end pipeline tests: discover -> parse -> resolve -> persist.

Covers M1's own exit criterion (implementation-plan.md section 2):
"`repomind index .` on a 1k-file Python repo produces symbols with correct
spans; no network calls" -- the no-network half lives in
tests/invariants/, this file covers "produces symbols with correct spans" --
plus M2's: "`repomind refs <symbol>` returns correct callers ..., grouped
by tier", exercised here as "the pipeline actually persists the
cross-file, heuristic-tier edges resolve.py computes."
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repomind.index.pipeline import IndexProgress, index_repository
from repomind.model import EdgeKind, IndexRunStatus, ScipStatus, SymbolKind, Tier
from repomind.store.sqlite.graph import SqliteGraphStore
from repomind.workspace import index_db_path, normalize_repo_path


def test_indexes_all_files_with_correct_symbol_counts(
    isolated_workspace: Path, simple_fixture_repo: Path
) -> None:
    result = index_repository(simple_fixture_repo)

    assert result.files_indexed == 4
    assert result.files_skipped == 0
    assert result.symbol_counts == {
        "file": 4,
        "module": 0,
        "class": 1,
        "function": 4,
        "method": 2,
        "variable": 2,
    }
    # `resolved` stays 0 until SCIP lands (RM-022); `inferred` is unused in
    # v1.0 entirely (features.md F-2 requirement 2). `heuristic` covers 9
    # `defines` edges (one per non-file symbol: Widget, count, __init__,
    # render, make_widget, _private_helper, build_default, DEFAULT_NAME,
    # main) plus 9 cross-reference edges -- see
    # test_specific_cross_file_edges_resolve_correctly below for what
    # those actually are, rather than asserting a bare count here alone.
    assert result.edge_counts == {"resolved": 0, "heuristic": 18, "inferred": 0}
    assert result.repo.scip_status == ScipStatus.SKIPPED
    assert result.index_run.status == IndexRunStatus.OK
    assert result.index_run.files_done == result.index_run.files_total == 4
    assert result.elapsed_seconds >= 0.0


def test_symbol_spans_and_qualified_names_are_correct(
    isolated_workspace: Path, simple_fixture_repo: Path
) -> None:
    index_repository(simple_fixture_repo)

    root_path = normalize_repo_path(simple_fixture_repo)
    store = SqliteGraphStore(index_db_path(root_path))
    try:
        repo = store.get_repo_by_path(root_path)
        assert repo is not None and repo.id is not None

        module_a = store.get_file_by_path(repo.id, "pkg/module_a.py")
        assert module_a is not None and module_a.id is not None

        widget = store.find_symbol_by_qualified_name(repo.id, "pkg.module_a.Widget")
        assert widget is not None
        assert widget.kind == SymbolKind.CLASS
        assert widget.file_id == module_a.id

        render = store.find_symbol_by_qualified_name(repo.id, "pkg.module_a.Widget.render")
        assert render is not None
        assert render.kind == SymbolKind.METHOD
        assert render.docstring == "Render the widget as text."
        assert render.signature == "render(self) -> str"

        # find_symbol_at_location: a line inside render() resolves to render,
        # not to the enclosing Widget class or the file itself.
        located = store.find_symbol_at_location(module_a.id, render.start_line + 1)
        assert located is not None
        assert located.qualified_name == "pkg.module_a.Widget.render"
    finally:
        store.close()


def test_specific_cross_file_edges_resolve_correctly(
    isolated_workspace: Path, simple_fixture_repo: Path
) -> None:
    """Not just a count: the pipeline must actually wire resolve.py's rules
    up to real, verifiable relationships in this fixture. Each assertion
    below is independently checkable by reading the fixture source
    (tests/fixtures/simple/) -- see script.py, pkg/module_b.py,
    pkg/module_a.py.
    """
    index_repository(simple_fixture_repo)

    root_path = normalize_repo_path(simple_fixture_repo)
    store = SqliteGraphStore(index_db_path(root_path))
    try:
        repo = store.get_repo_by_path(root_path)
        assert repo is not None and repo.id is not None

        def qname(name: str) -> int:
            sym = store.find_symbol_by_qualified_name(repo.id, name)
            assert sym is not None and sym.id is not None, name
            return sym.id

        def kinds_to(target: str) -> set[EdgeKind]:
            edges = store.edges_to(qname(target), tiers=(Tier.HEURISTIC,))
            return {e.kind for e in edges}

        def sources_of_kind(target: str, kind: EdgeKind) -> set[int]:
            edges = store.edges_to(qname(target), kind=kind, tiers=(Tier.HEURISTIC,))
            return {e.src_symbol_id for e in edges}

        # script.main() calls pkg.module_b.build_default() (cross-file, via
        # `from pkg.module_b import build_default`).
        assert qname("script.main") in sources_of_kind("pkg.module_b.build_default", EdgeKind.CALLS)

        # pkg.module_b.build_default() calls pkg.module_a.make_widget()
        # (cross-file, resolved via module_b's own same-file import).
        assert qname("pkg.module_b.build_default") in sources_of_kind(
            "pkg.module_a.make_widget", EdgeKind.CALLS
        )

        # The module-to-module import edges themselves.
        assert qname("pkg.module_b") in sources_of_kind("pkg.module_a.Widget", EdgeKind.IMPORTS)
        assert qname("script") in sources_of_kind(
            "pkg.module_b.build_default", EdgeKind.IMPORTS
        )

        # make_widget's return-type annotation (`-> Widget`) is a REFERENCES
        # edge, distinct from the CALLS edge its `return Widget(name)` body
        # separately produces -- both point at the same symbol.
        assert EdgeKind.REFERENCES in kinds_to("pkg.module_a.Widget")
        assert EdgeKind.CALLS in kinds_to("pkg.module_a.Widget")

        # `Widget.render()` is never called via a resolvable target anywhere
        # in this fixture (script.py's `widget.render()` needs type
        # inference the heuristic tier deliberately does not attempt) --
        # confirms the resolver's restraint, not just its reach. It still
        # has exactly one incoming edge: Widget's own DEFINES of it.
        incoming = store.edges_to(qname("pkg.module_a.Widget.render"))
        assert {e.kind for e in incoming} == {EdgeKind.DEFINES}

        # `defines`: Widget defines its own method render.
        assert qname("pkg.module_a.Widget") in sources_of_kind(
            "pkg.module_a.Widget.render", EdgeKind.DEFINES
        )
    finally:
        store.close()


def test_reindex_is_idempotent(isolated_workspace: Path, simple_fixture_repo: Path) -> None:
    first = index_repository(simple_fixture_repo)
    second = index_repository(simple_fixture_repo)

    assert first.symbol_counts == second.symbol_counts
    assert first.files_indexed == second.files_indexed
    assert first.edge_counts == second.edge_counts

    # Re-indexing does not accumulate duplicate rows -- replace_symbols
    # deletes-then-inserts per file, and _index_one_file's
    # delete_edges_from_file mirrors that for edges, so a count doubling
    # would mean either guarantee broke.
    root_path = normalize_repo_path(simple_fixture_repo)
    store = SqliteGraphStore(index_db_path(root_path))
    try:
        repo = store.get_repo_by_path(root_path)
        assert repo is not None and repo.id is not None
        assert store.count_symbols_by_kind(repo.id) == first.symbol_counts
        assert store.count_edges_by_tier(repo.id) == first.edge_counts
    finally:
        store.close()


def test_progress_callback_reports_every_file(
    isolated_workspace: Path, simple_fixture_repo: Path
) -> None:
    seen: list[IndexProgress] = []
    index_repository(simple_fixture_repo, progress_callback=seen.append)

    assert len(seen) == 4
    assert [p.files_done for p in seen] == [1, 2, 3, 4]
    assert all(p.files_total == 4 for p in seen)


def test_skips_unsupported_and_undecodable_files(
    isolated_workspace: Path, simple_fixture_repo: Path, tmp_path: Path
) -> None:
    import shutil

    repo_copy = tmp_path / "repo_with_extras"
    shutil.copytree(simple_fixture_repo, repo_copy)
    (repo_copy / "README.md").write_text("not python", encoding="utf-8")
    (repo_copy / "binary.py").write_bytes(b"\xff\xfe\x00\x01not valid utf-8 \xff")

    result = index_repository(repo_copy)

    assert result.files_indexed == 4  # the original 4 .py files
    assert result.files_skipped == 2  # README.md (unsupported) + binary.py (undecodable)


def test_a_failure_mid_run_marks_the_index_run_interrupted_not_stuck_running(
    isolated_workspace: Path, simple_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """design.md section 12: a failure must never leave index_run at
    RUNNING forever, or `repomind status` would report a phantom
    in-progress index. Exercises pipeline.py's except-Exception boundary,
    which nothing else in this suite reaches.
    """
    import repomind.index.pipeline as pipeline_module
    from repomind.errors import IndexingError
    from repomind.model import IndexRunStatus
    from repomind.store.sqlite.graph import SqliteGraphStore
    from repomind.workspace import index_db_path, normalize_repo_path

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(pipeline_module, "_index_one_file", _boom)

    with pytest.raises(IndexingError, match="simulated disk failure"):
        index_repository(simple_fixture_repo)

    root_path = normalize_repo_path(simple_fixture_repo)
    store = SqliteGraphStore(index_db_path(root_path))
    try:
        repo = store.get_repo_by_path(root_path)
        assert repo is not None and repo.id is not None
        run = store.get_latest_index_run(repo.id)
        assert run is not None
        assert run.status == IndexRunStatus.INTERRUPTED
        assert run.error is not None and "simulated disk failure" in run.error
    finally:
        store.close()
