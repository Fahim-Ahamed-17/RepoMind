"""Shared pytest fixtures.

Every test that touches storage or the pipeline needs its workspace
isolated from the real ``~/.repomind`` -- see :func:`isolated_workspace`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.fixture(autouse=True)
def fake_embedder(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real embedding needs ``bge-small-en-v1.5`` loaded from disk --
    slow, and on a machine where it is not already cached, a genuine
    network fetch that ``--disable-socket`` (docs/conventions.md: "No
    network in any test") would correctly block. Every test that runs the
    full ``index_repository()`` pipeline would otherwise pay that cost, or
    fail outright on a cold cache, just to get chunks stored -- none of
    them care which vectors land in ``chunk_vec``.

    So fake ``LocalEmbedder`` here, at the exact seam ``index/pipeline.py``
    imports it through, the same technique
    tests/integration/test_scip_pipeline.py uses for ``run_scip_python``,
    and reserve the real model for the one test that actually needs it:
    test_embed.py's ``embedding_model``-marked test, which talks to
    ``LocalEmbedder`` directly rather than through the pipeline and so is
    exempted below.
    """
    if "embedding_model" in request.keywords:
        return

    from repomind.embed.local import DIMENSIONS

    class _FakeEmbedder:
        dimensions = DIMENSIONS

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[0.0] * DIMENSIONS for _ in texts]

    monkeypatch.setattr("repomind.index.pipeline.LocalEmbedder", _FakeEmbedder)


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect :func:`repomind.workspace.workspace_root` to a throwaway
    directory under pytest's own ``tmp_path``, so no test ever reads or
    writes the real ``~/.repomind`` on the machine running the suite.
    """
    root = tmp_path / ".repomind"
    monkeypatch.setattr("repomind.workspace.workspace_root", lambda: root)
    return root


@pytest.fixture
def simple_fixture_repo() -> Path:
    """The small, non-git Python package under tests/fixtures/simple --
    exercises the filesystem-walk discovery path (no .git directory of its
    own) end to end: a package with __init__.py, a class with a method and
    a class-level variable, module-level functions, and a cross-module
    import resolved into real heuristic-tier edges (RM-020/RM-021).
    """
    return Path(__file__).parent / "fixtures" / "simple"


@pytest.fixture
def cyclic_fixture_repo() -> Path:
    """Genuinely circular imports (a.py <-> b.py, both importing and
    calling into each other) plus a three-level inheritance chain
    (Animal -> Dog -> Puppy) -- the graph edge cases RM-024's traversal
    must not hang on. See docs/conventions.md's fixture plan and
    tests/fixtures/cyclic/__init__.py.
    """
    return Path(__file__).parent / "fixtures" / "cyclic"
