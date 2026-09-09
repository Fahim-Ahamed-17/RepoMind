"""Shared pytest fixtures.

Every test that touches storage or the pipeline needs its workspace
isolated from the real ``~/.repomind`` -- see :func:`isolated_workspace`.
"""

from __future__ import annotations

from pathlib import Path

import pytest


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
    import (not yet resolved into an edge -- that's M2).
    """
    return Path(__file__).parent / "fixtures" / "simple"
