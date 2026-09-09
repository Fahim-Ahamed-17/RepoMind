"""AGENTS.md invariant 1: indexing never touches the network.

design.md sections 5.1 and 9.1 specify the mechanism literally: "asserted
by a test that monkeypatches socket.socket to raise". Written that way
deliberately, rather than relying solely on the ambient pytest-socket
plugin (enabled globally via pyproject.toml's ``--disable-socket``) -- a
dedicated monkeypatch keeps this specific guarantee self-contained and
failing loudly on its own even if the global pytest config ever changes.

If this test ever needs to change to make indexing pass, the fix is the
code that introduced a network call, never this test (AGENTS.md
"Working style" / "Tests").
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from repomind.index.pipeline import index_repository


def test_full_index_run_makes_no_network_connection(
    isolated_workspace: Path, simple_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _forbidden_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "indexing attempted to open a network socket -- this violates "
            "AGENTS.md invariant 1 (indexing never touches the network). "
            "The fix is in the code that opened it, not in this test."
        )

    monkeypatch.setattr(socket, "socket", _forbidden_socket)

    result = index_repository(simple_fixture_repo)

    assert result.files_indexed == 4
    assert result.files_skipped == 0


def test_full_index_run_makes_no_network_connection_even_on_reindex(
    isolated_workspace: Path, simple_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-index path (an existing index.db, a prior index_run row to
    read) is a different code path through the pipeline than a fresh
    index -- worth asserting separately rather than assuming the first
    test covers it.
    """
    index_repository(simple_fixture_repo)  # first run, socket not yet patched

    def _forbidden_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError("re-indexing attempted to open a network socket")

    monkeypatch.setattr(socket, "socket", _forbidden_socket)

    result = index_repository(simple_fixture_repo)
    assert result.files_indexed == 4
