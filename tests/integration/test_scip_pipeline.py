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
from repomind.languages.python.scip import ScipIndex
from repomind.model import ScipStatus, Tier
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
