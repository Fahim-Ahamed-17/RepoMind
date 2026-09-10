"""End-to-end tests for RM-034: incremental re-indexing scopes a
re-index to changed files plus their direct graph neighbours (F-3),
rather than reprocessing the whole repo every time.

Uses a real git repository (``GitRepo.init`` + real commits), not the
static ``tests/fixtures/simple`` -- unlike test_pipeline.py's tests,
these need real commit history to diff against
(``ingest/git.py``'s ``changed_paths_since``), the same reason
tests/unit/test_git.py builds its own throwaway repos per test rather
than reusing a fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo as GitRepo
from repomind.index.pipeline import index_repository
from repomind.model import EdgeKind
from repomind.store.sqlite.graph import SqliteGraphStore
from repomind.workspace import index_db_path, normalize_repo_path


def _write_package(root: Path) -> None:
    (root / "pkg").mkdir(exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "module_a.py").write_text(
        'def make_widget(name: str) -> str:\n    return f"Widget({name})"\n',
        encoding="utf-8",
    )
    (root / "pkg" / "module_b.py").write_text(
        "from pkg.module_a import make_widget\n\n\ndef build_default() -> str:\n"
        '    return make_widget("default")\n',
        encoding="utf-8",
    )
    (root / "pkg" / "unrelated.py").write_text(
        "def standalone() -> int:\n    return 1\n", encoding="utf-8"
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    # A subdirectory of tmp_path, not tmp_path itself: isolated_workspace
    # (used by every test below) also roots ~/.repomind under this same
    # tmp_path. Indexing the repo root directly would, after the first
    # successful run, discover .repomind's own index.db sitting inside
    # the "repository" being indexed -- confirmed directly (files_total
    # jumped from 4 to 5 on a second run before this fix).
    root = tmp_path / "repo"
    root.mkdir()
    _write_package(root)
    with GitRepo.init(root) as repo:
        repo.index.add(
            ["pkg/__init__.py", "pkg/module_a.py", "pkg/module_b.py", "pkg/unrelated.py"]
        )
        repo.index.commit("initial")
    return root


def _commit(root: Path, message: str, *paths: str) -> None:
    """Stage ``paths`` and commit.

    Always via ``with``: GitPython keeps persistent ``git cat-file``
    subprocesses per ``Repo`` and only tears them down in ``close()``.
    Left to the garbage collector they surface as ResourceWarnings, which
    pyproject.toml's ``filterwarnings = ["error", ...]`` turns into a
    failure attributed to whichever test happened to be running when GC
    ran -- confirmed directly: the reported test moved between runs of
    this same file.
    """
    with GitRepo(root) as repo:
        repo.index.add(list(paths))
        repo.index.commit(message)


def _commit_removal(root: Path, message: str, *paths: str) -> None:
    with GitRepo(root) as repo:
        repo.index.remove(list(paths), working_tree=True)
        repo.index.commit(message)


def _open_store(root: Path) -> SqliteGraphStore:
    return SqliteGraphStore(index_db_path(normalize_repo_path(root)))


def test_first_index_of_a_git_repo_processes_every_file(
    isolated_workspace: Path, git_repo: Path
) -> None:
    result = index_repository(git_repo, use_scip=False)
    assert result.files_indexed == 4


def test_reindex_with_no_new_commits_processes_nothing(
    isolated_workspace: Path, git_repo: Path
) -> None:
    index_repository(git_repo, use_scip=False)
    second = index_repository(git_repo, use_scip=False)
    assert second.files_indexed == 0


def test_reindex_only_reprocesses_the_changed_file_and_its_neighbour(
    isolated_workspace: Path, git_repo: Path
) -> None:
    """module_b.py calls module_a.py's make_widget -- changing module_a.py
    (even without touching make_widget's signature) reassigns every
    symbol row for that file, so module_b.py's edge into the old row
    would be lost unless it is reprocessed too (F-3's "neighbour
    recomputation"). unrelated.py shares no edge with either and must be
    left alone.
    """
    index_repository(git_repo, use_scip=False)

    store = _open_store(git_repo)
    try:
        r = store.get_repo_by_path(normalize_repo_path(git_repo))
        assert r is not None and r.id is not None
        old_make_widget = store.find_symbol_by_qualified_name(r.id, "pkg.module_a.make_widget")
        assert old_make_widget is not None and old_make_widget.id is not None
    finally:
        store.close()

    (git_repo / "pkg" / "module_a.py").write_text(
        'def make_widget(name: str) -> str:\n    return f"Widget({name})"\n\n\n'
        "def unrelated_addition() -> None:\n    pass\n",
        encoding="utf-8",
    )
    _commit(git_repo, "touch module_a", "pkg/module_a.py")

    result = index_repository(git_repo, use_scip=False)

    assert result.files_indexed == 2  # module_a.py (changed) + module_b.py (neighbour)

    store = _open_store(git_repo)
    try:
        r = store.get_repo_by_path(normalize_repo_path(git_repo))
        assert r is not None and r.id is not None
        new_make_widget = store.find_symbol_by_qualified_name(r.id, "pkg.module_a.make_widget")
        assert new_make_widget is not None and new_make_widget.id is not None
        assert new_make_widget.id != old_make_widget.id  # module_a.py really was reprocessed

        build_default = store.find_symbol_by_qualified_name(r.id, "pkg.module_b.build_default")
        assert build_default is not None and build_default.id is not None

        incoming = store.edges_to(new_make_widget.id, kind=EdgeKind.CALLS)
        assert {e.src_symbol_id for e in incoming} == {build_default.id}
    finally:
        store.close()


def test_reindex_leaves_an_unrelated_files_symbols_untouched(
    isolated_workspace: Path, git_repo: Path
) -> None:
    index_repository(git_repo, use_scip=False)
    store = _open_store(git_repo)
    try:
        r = store.get_repo_by_path(normalize_repo_path(git_repo))
        assert r is not None and r.id is not None
        before = store.find_symbol_by_qualified_name(r.id, "pkg.unrelated.standalone")
        assert before is not None
    finally:
        store.close()

    (git_repo / "pkg" / "module_a.py").write_text(
        'def make_widget(name: str) -> str:\n    return f"Widget: {name}"\n',
        encoding="utf-8",
    )
    _commit(git_repo, "touch module_a only", "pkg/module_a.py")

    index_repository(git_repo, use_scip=False)

    store = _open_store(git_repo)
    try:
        r = store.get_repo_by_path(normalize_repo_path(git_repo))
        assert r is not None and r.id is not None
        after = store.find_symbol_by_qualified_name(r.id, "pkg.unrelated.standalone")
        assert after is not None
        assert after.id == before.id  # never reprocessed: same row, untouched
    finally:
        store.close()


def test_deleting_a_file_and_reindexing_cascades_its_removal(
    isolated_workspace: Path, git_repo: Path
) -> None:
    index_repository(git_repo, use_scip=False)

    _commit_removal(git_repo, "remove unrelated.py", "pkg/unrelated.py")

    result = index_repository(git_repo, use_scip=False)
    assert result.files_indexed == 0  # nothing left to *reprocess*; deletion isn't a reprocess

    store = _open_store(git_repo)
    try:
        r = store.get_repo_by_path(normalize_repo_path(git_repo))
        assert r is not None and r.id is not None
        assert store.get_file_by_path(r.id, "pkg/unrelated.py") is None
        assert store.find_symbol_by_qualified_name(r.id, "pkg.unrelated.standalone") is None
    finally:
        store.close()


def test_an_unreachable_indexed_sha_falls_back_to_a_full_index(
    isolated_workspace: Path, git_repo: Path
) -> None:
    """F-3 requirement 7's graceful degradation extends to a rewritten
    history, not just "no git"/"never indexed before": ``changed_paths_since``
    returns ``None`` when ``from_sha`` no longer resolves (tests/unit/
    test_git.py covers that in isolation), and ``_incremental_scope``
    must treat that the same as "no previous SHA at all" -- a full
    index, not a crash.
    """
    index_repository(git_repo, use_scip=False)

    store = _open_store(git_repo)
    try:
        r = store.get_repo_by_path(normalize_repo_path(git_repo))
        assert r is not None and r.id is not None
        # A commit sha that was never actually reachable here -- stands
        # in for "the branch this was indexed from got rebased away".
        store.set_repo_indexed_sha(r.id, "0" * 40, r.scip_status)
    finally:
        store.close()

    (git_repo / "pkg" / "module_a.py").write_text(
        'def make_widget(name: str) -> str:\n    return f"Widget({name})"\n\n\n'
        "def unrelated_addition() -> None:\n    pass\n",
        encoding="utf-8",
    )
    _commit(git_repo, "touch module_a", "pkg/module_a.py")

    result = index_repository(git_repo, use_scip=False)

    assert result.files_indexed == 4  # every file, not just the diff


def test_an_interrupted_incremental_run_retries_the_full_diff_not_a_smaller_one(
    isolated_workspace: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that gets interrupted after persisting some files' symbols
    but before whole-repo edge resolution runs must not have its retry
    treat those files as "already done" -- their content already matches
    what's stored, but their edges were never recomputed. See
    ``_incremental_scope``'s own docstring: this is exactly why it
    doesn't do a finer-grained per-file skip on top of the git diff.
    """
    import repomind.index.pipeline as pipeline_module
    from repomind.errors import IndexingError

    index_repository(git_repo, use_scip=False)  # first, full index

    (git_repo / "pkg" / "module_a.py").write_text(
        'def make_widget(name: str) -> str:\n    return f"Widget({name})"\n\n\n'
        "def unrelated_addition() -> None:\n    pass\n",
        encoding="utf-8",
    )
    _commit(git_repo, "touch module_a", "pkg/module_a.py")

    real_index_one_file = pipeline_module._index_one_file
    calls = {"n": 0}

    def _flaky(*args: object, **kwargs: object) -> object:
        # Crashes before touching any file in this run's scope -- the
        # narrower case where the crash lands *after* one scoped file was
        # already reprocessed is a known, separately documented
        # limitation (_incremental_scope's own docstring): reprocessing a
        # changed file cascade-deletes its old symbol rows immediately,
        # which can destroy the very edge a retry would use to
        # rediscover an as-yet-unprocessed neighbour. Not attempted here.
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash mid-run")
        return real_index_one_file(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "_index_one_file", _flaky)
    with pytest.raises(IndexingError):
        index_repository(git_repo, use_scip=False)
    # Restore _index_one_file directly, not via monkeypatch.undo(): that
    # undoes every patch on this test's shared monkeypatch fixture in
    # reverse order, including isolated_workspace's own workspace_root
    # patch (isolated_workspace takes the same monkeypatch fixture
    # instance) -- confirmed directly after it silently redirected the
    # retry below at the real, unfaked ~/.repomind.
    pipeline_module._index_one_file = real_index_one_file

    result = index_repository(git_repo, use_scip=False)

    assert result.files_indexed == 2  # the same diff as before the crash, not a smaller one

    store = _open_store(git_repo)
    try:
        r = store.get_repo_by_path(normalize_repo_path(git_repo))
        assert r is not None and r.id is not None
        make_widget = store.find_symbol_by_qualified_name(r.id, "pkg.module_a.make_widget")
        build_default = store.find_symbol_by_qualified_name(r.id, "pkg.module_b.build_default")
        assert make_widget is not None and make_widget.id is not None
        assert build_default is not None and build_default.id is not None

        incoming = store.edges_to(make_widget.id, kind=EdgeKind.CALLS)
        assert {e.src_symbol_id for e in incoming} == {build_default.id}
    finally:
        store.close()


def test_reindex_picks_up_a_newly_added_file(isolated_workspace: Path, git_repo: Path) -> None:
    index_repository(git_repo, use_scip=False)

    (git_repo / "pkg" / "module_c.py").write_text(
        "def brand_new() -> None:\n    pass\n", encoding="utf-8"
    )
    _commit(git_repo, "add module_c", "pkg/module_c.py")

    result = index_repository(git_repo, use_scip=False)

    assert result.files_indexed == 1
    store = _open_store(git_repo)
    try:
        r = store.get_repo_by_path(normalize_repo_path(git_repo))
        assert r is not None and r.id is not None
        assert store.find_symbol_by_qualified_name(r.id, "pkg.module_c.brand_new") is not None
    finally:
        store.close()
