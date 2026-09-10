from __future__ import annotations

import os
from pathlib import Path

import pytest
from repomind import workspace


def test_index_db_path_is_under_workspace_root(isolated_workspace: Path) -> None:
    db = workspace.index_db_path("/some/repo")
    assert db.parent.parent == isolated_workspace / "repos"
    assert db.name == "index.db"


def test_same_path_hashes_identically(isolated_workspace: Path) -> None:
    a = workspace.index_db_path("/some/repo")
    b = workspace.index_db_path("/some/repo")
    assert a == b


def test_different_paths_hash_differently(isolated_workspace: Path) -> None:
    a = workspace.index_db_path("/some/repo")
    b = workspace.index_db_path("/some/other/repo")
    assert a != b


def test_register_then_list_then_unregister(isolated_workspace: Path) -> None:
    workspace.register_repo("/tmp/proj")
    entries = workspace.list_registered_repos()
    assert len(entries) == 1
    assert entries[0].root_path == "/tmp/proj"

    workspace.unregister_repo("/tmp/proj")
    assert workspace.list_registered_repos() == []


def test_registered_but_deleted_repo_reports_stale_not_error(
    isolated_workspace: Path, tmp_path: Path
) -> None:
    gone = tmp_path / "will_be_deleted"
    gone.mkdir()
    workspace.register_repo(str(gone))
    gone.rmdir()

    entries = workspace.list_registered_repos()
    assert len(entries) == 1
    assert entries[0].exists is False


def test_workspace_dir_size_sums_every_file_in_the_workspace(isolated_workspace: Path) -> None:
    d = workspace.repo_workspace_dir("/tmp/proj")
    d.mkdir(parents=True)
    (d / "index.db").write_bytes(b"x" * 100)
    (d / "index.scip").write_bytes(b"y" * 50)

    assert workspace.workspace_dir_size("/tmp/proj") == 150


def test_workspace_dir_size_is_zero_for_a_repo_never_indexed(isolated_workspace: Path) -> None:
    assert workspace.workspace_dir_size("/tmp/never-indexed") == 0


def test_remove_repo_workspace_deletes_files_and_unregisters(isolated_workspace: Path) -> None:
    workspace.register_repo("/tmp/proj")
    d = workspace.repo_workspace_dir("/tmp/proj")
    d.mkdir(parents=True)
    (d / "index.db").write_bytes(b"data")

    removed = workspace.remove_repo_workspace("/tmp/proj")

    assert removed is True
    assert not d.exists()
    assert workspace.list_registered_repos() == []


def test_remove_repo_workspace_reports_false_when_nothing_to_remove(
    isolated_workspace: Path,
) -> None:
    workspace.register_repo("/tmp/proj")  # registered, but never actually indexed

    removed = workspace.remove_repo_workspace("/tmp/proj")

    assert removed is False
    assert workspace.list_registered_repos() == []  # the stale entry is still cleared


def test_remove_repo_workspace_refuses_while_a_live_process_holds_the_lock(
    isolated_workspace: Path,
) -> None:
    d = workspace.repo_workspace_dir("/tmp/proj")
    d.mkdir(parents=True)
    (d / "index.db").write_bytes(b"data")
    workspace.register_repo("/tmp/proj")

    with (
        workspace.index_lock("/tmp/proj"),
        pytest.raises(Exception, match=r"PID \d+"),
    ):
        workspace.remove_repo_workspace("/tmp/proj")

    # Refused, not partially applied: still registered, files still there.
    assert d.exists()
    assert len(workspace.list_registered_repos()) == 1


def test_index_lock_blocks_concurrent_acquisition_by_a_live_process(
    isolated_workspace: Path,
) -> None:
    with (
        workspace.index_lock("/tmp/proj"),
        pytest.raises(Exception, match=r"PID \d+"),
        workspace.index_lock("/tmp/proj"),
    ):
        pass  # both context managers entered; the inner one must have raised


def test_index_lock_releases_on_exit(isolated_workspace: Path) -> None:
    with workspace.index_lock("/tmp/proj"):
        assert workspace.lock_path("/tmp/proj").exists()
    assert not workspace.lock_path("/tmp/proj").exists()


def test_index_lock_releases_on_exception(isolated_workspace: Path) -> None:
    with pytest.raises(ValueError, match="boom"), workspace.index_lock("/tmp/proj"):
        raise ValueError("boom")
    assert not workspace.lock_path("/tmp/proj").exists()


def test_stale_lock_from_a_dead_pid_is_reclaimed(isolated_workspace: Path) -> None:
    import json

    lock_file = workspace.lock_path("/tmp/proj")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    # A PID essentially guaranteed not to correspond to a live process.
    dead_pid = 999_999
    lock_file.write_text(json.dumps({"pid": dead_pid}), encoding="utf-8")

    # Must not raise WorkspaceLockedError -- the lock is stale, reclaim it.
    with workspace.index_lock("/tmp/proj"):
        held = json.loads(lock_file.read_text(encoding="utf-8"))
        assert held["pid"] == os.getpid()


def test_pid_liveness_check_works_for_a_live_and_a_dead_pid(isolated_workspace: Path) -> None:
    """_is_pid_alive's two branches (ctypes.windll on Windows, os.kill on
    POSIX) are exercised for real, not simulated, by CI's own OS matrix
    (.github/workflows/ci.yml runs ubuntu-latest, macos-latest, and
    windows-latest) -- os.kill's exception semantics genuinely differ
    between Windows and POSIX (OSError vs. ProcessLookupError for an
    unknown PID), so faking "no windll" on this Windows dev machine to
    force the POSIX branch does not validly test POSIX behaviour; it only
    proves Windows' os.kill doesn't match what that branch expects, which
    is true but irrelevant, since that branch never runs on Windows for
    real. This test therefore only asserts the platform-appropriate,
    genuinely-taken branch behaves correctly on whichever OS runs it.
    """
    assert workspace._is_pid_alive(os.getpid()) is True
    assert workspace._is_pid_alive(999_999) is False
