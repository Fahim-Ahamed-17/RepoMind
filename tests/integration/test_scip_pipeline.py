"""End-to-end tests for RM-022's pipeline wiring: run_scip_python's output
flowing through to a persisted ``resolved``-tier edge and a backfilled
``Symbol.scip_symbol``, and the degraded path when it fails.

The real ``scip-python`` subprocess is never invoked here (see
tests/unit/test_scip.py's module docstring for why); ``run_scip_python``
is monkeypatched at the exact seam ``index/pipeline.py`` imports it
through, the same technique
tests/integration/test_pipeline.py::test_a_failure_mid_run_marks_the_index_run_interrupted_not_stuck_running
already uses to simulate a failure without needing the real failure
condition. ``--no-scip`` (``use_scip=False``) itself is covered by
tests/integration/test_pipeline.py, whose tests assert ``scip_status ==
ScipStatus.SKIPPED`` as part of staying SCIP-independent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repomind.errors import ScipUnavailableError
from repomind.index import pipeline as pipeline_module
from repomind.index.pipeline import index_repository
from repomind.languages.python.scip import ScipIndex, is_available
from repomind.model import EdgeKind, ScipStatus, Tier
from repomind.store.sqlite.graph import SqliteGraphStore
from repomind.workspace import index_db_path, normalize_repo_path


def test_scip_failure_degrades_without_losing_heuristic_edges(
    isolated_workspace: Path, simple_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> ScipIndex:
        raise ScipUnavailableError("simulated: scip-python not found on PATH")

    monkeypatch.setattr(pipeline_module, "run_scip_python", _boom)

    result = index_repository(simple_fixture_repo)  # use_scip defaults to True

    assert result.repo.scip_status == ScipStatus.DEGRADED
    assert result.edge_counts["resolved"] == 0
    assert result.edge_counts["heuristic"] > 0  # the heuristic pass is unaffected


def test_scip_success_produces_a_resolved_edge_and_backfills_scip_symbol(
    isolated_workspace: Path, simple_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fabricates a ScipIndex around one real relationship already present
    in tests/fixtures/simple: pkg/module_b.py's ``build_default`` calls
    ``make_widget``, defined at pkg/module_a.py line 17 (1-indexed) --
    SCIP's own 0-indexed line 16. The reference appears at module_b.py
    line 9 (1-indexed) -- SCIP's 0-indexed line 8. Both ends resolve
    through the real, unmodified tree-sitter parse and heuristic-resolver
    machinery; only the SCIP subprocess call itself is faked.
    """

    def _fake_run(root: Path, output_path: Path, **kwargs: object) -> ScipIndex:
        return ScipIndex(
            definitions={("pkg/module_a.py", 16): "scip-py . . pkg/module_a/make_widget()."},
            references={("pkg/module_b.py", 8): "scip-py . . pkg/module_a/make_widget()."},
        )

    monkeypatch.setattr(pipeline_module, "run_scip_python", _fake_run)

    result = index_repository(simple_fixture_repo)

    assert result.repo.scip_status == ScipStatus.OK
    assert result.edge_counts["resolved"] == 1

    root_path = normalize_repo_path(simple_fixture_repo)
    store = SqliteGraphStore(index_db_path(root_path))
    try:
        repo = store.get_repo_by_path(root_path)
        assert repo is not None and repo.id is not None

        make_widget = store.find_symbol_by_qualified_name(repo.id, "pkg.module_a.make_widget")
        assert make_widget is not None
        assert make_widget.scip_symbol == "scip-py . . pkg/module_a/make_widget()."

        build_default = store.find_symbol_by_qualified_name(repo.id, "pkg.module_b.build_default")
        assert build_default is not None and build_default.id is not None
        assert make_widget.id is not None

        resolved_edges = store.edges_to(make_widget.id, tiers=(Tier.RESOLVED,))
        assert {e.src_symbol_id for e in resolved_edges} == {build_default.id}
    finally:
        store.close()


@pytest.mark.scip
@pytest.mark.skipif(not is_available(), reason="scip-python not on PATH")
def test_real_scip_python_resolves_the_known_relationships_in_the_simple_fixture(
    isolated_workspace: Path, simple_fixture_repo: Path
) -> None:
    """The one test in this suite that invokes the real ``scip-python``
    binary end to end -- everything else here fakes ``run_scip_python`` at
    the seam (see this module's docstring for why). Guarded by
    ``skipif(not is_available())`` so it self-skips on any machine without
    ``scip-python`` on PATH rather than fail the suite there; wherever it
    does run, it locks in the exact three real relationships confirmed by
    hand while diagnosing RM-022's Windows-compatibility issues (pinned
    version 0.3.0, no ``--cwd``, a relative ``--output``, ``--exclude`` for
    dependency directories -- see scip.py's own module docstring for why
    each of those is load-bearing, not incidental).
    """
    result = index_repository(simple_fixture_repo)  # use_scip defaults to True

    assert result.repo.scip_status == ScipStatus.OK
    assert result.edge_counts["resolved"] == 3

    root_path = normalize_repo_path(simple_fixture_repo)
    store = SqliteGraphStore(index_db_path(root_path))
    try:
        repo = store.get_repo_by_path(root_path)
        assert repo is not None and repo.id is not None

        def qname(name: str) -> int:
            sym = store.find_symbol_by_qualified_name(repo.id, name)
            assert sym is not None and sym.id is not None, name
            return sym.id

        def resolved_sources(target: str) -> set[int]:
            edges = store.edges_to(qname(target), tiers=(Tier.RESOLVED,))
            return {e.src_symbol_id for e in edges}

        # pkg.module_b.build_default's own `-> Widget` return annotation.
        assert qname("pkg.module_b.build_default") in resolved_sources("pkg.module_a.Widget")
        # script.main() calls build_default().
        assert qname("script.main") in resolved_sources("pkg.module_b.build_default")
        # The `if __name__ == "__main__": main()` call at module scope.
        assert qname("script") in resolved_sources("script.main")
    finally:
        store.close()


@pytest.mark.scip
@pytest.mark.skipif(not is_available(), reason="scip-python not on PATH")
def test_resolved_and_heuristic_tiers_coexist_for_the_same_relationship(
    isolated_workspace: Path, simple_fixture_repo: Path
) -> None:
    """F-2 requirement 1/4 (features.md): tiers are never merged, not even
    when both a heuristic and a resolved edge exist for the literal same
    ``(src, dst, kind)`` -- confirmed here with real data rather than only
    the schema-level UNIQUE(src, dst, kind, tier) constraint that makes it
    *possible*: ``pkg.module_b.build_default``'s ``-> Widget`` return
    annotation is exactly such a case -- the heuristic resolver's own
    annotation-matching rule (index/resolve.py) and SCIP both independently
    find it, and both rows must survive, queryable separately.
    """
    result = index_repository(simple_fixture_repo)
    assert result.repo.scip_status == ScipStatus.OK

    root_path = normalize_repo_path(simple_fixture_repo)
    store = SqliteGraphStore(index_db_path(root_path))
    try:
        repo = store.get_repo_by_path(root_path)
        assert repo is not None and repo.id is not None

        widget = store.find_symbol_by_qualified_name(repo.id, "pkg.module_a.Widget")
        assert widget is not None and widget.id is not None
        build_default = store.find_symbol_by_qualified_name(repo.id, "pkg.module_b.build_default")
        assert build_default is not None and build_default.id is not None

        heuristic_edges = store.edges_to(
            widget.id, kind=EdgeKind.REFERENCES, tiers=(Tier.HEURISTIC,)
        )
        resolved_edges = store.edges_to(widget.id, kind=EdgeKind.REFERENCES, tiers=(Tier.RESOLVED,))
        # Not asserted as the *only* heuristic hit: pkg.module_a.make_widget
        # has its own `-> Widget` return annotation in the same file, a
        # second, separate real reference this test does not need to care
        # about -- only that build_default's own edge is present, in both
        # tiers, without one displacing the other.
        assert build_default.id in {e.src_symbol_id for e in heuristic_edges}
        assert {e.src_symbol_id for e in resolved_edges} == {build_default.id}

        # Both tiers together for build_default specifically: two distinct
        # rows, not one collapsed row -- the "never merged" invariant
        # holding under an unfiltered query, not just a tier-scoped one.
        both = store.edges_from(build_default.id, kind=EdgeKind.REFERENCES)
        both_to_widget = [e for e in both if e.dst_symbol_id == widget.id]
        assert len(both_to_widget) == 2
        assert {e.tier for e in both_to_widget} == {Tier.HEURISTIC, Tier.RESOLVED}
    finally:
        store.close()


def test_no_scip_flag_skips_the_attempt_entirely(
    isolated_workspace: Path, simple_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """use_scip=False must never even call run_scip_python -- a security
    control (design.md AD-9, section 9.3: SCIP indexers execute in the
    target repo), not a best-effort optimisation.
    """
    calls: list[object] = []
    monkeypatch.setattr(
        pipeline_module, "run_scip_python", lambda *a, **k: calls.append((a, k)) or ScipIndex()
    )

    result = index_repository(simple_fixture_repo, use_scip=False)

    assert calls == []
    assert result.repo.scip_status == ScipStatus.SKIPPED
