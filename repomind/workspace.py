"""Where a repo's index lives, and how concurrent indexers stay out of each
other's way.

Implements design.md AD-5 (confirmed 2026-09-09): central storage at
``~/.repomind/repos/<hash>/index.db``, never inside the repo being indexed
(AGENTS.md invariant 6) -- the primary user is often exploring code they do
not own, so nothing here may assume write access to it.

RM-013.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from repomind.errors import WorkspaceLockedError

if TYPE_CHECKING:
    from collections.abc import Iterator


#: The literal path AGENTS.md and design.md both name repeatedly. Not
#: platformdirs.user_data_dir() -- that would resolve to a different,
#: OS-specific location and break the invariant's own wording.
def workspace_root() -> Path:
    return Path.home() / ".repomind"


def _repo_hash(root_path: str) -> str:
    """A short, filesystem-safe, stable identifier for a repo path.

    Truncated to 16 hex chars: readable in a directory listing, and at that
    length a collision is not a realistic concern for a personal tool
    indexing at most a few dozen repos.
    """
    digest = hashlib.sha256(root_path.encode("utf-8")).hexdigest()
    return digest[:16]


def normalize_repo_path(path: Path | str) -> str:
    """The canonical string form used as both the registry key input and
    :attr:`repomind.model.Repo.root_path`. Resolves symlinks and `..` so the
    same repo opened two different ways hashes to the same workspace.
    """
    return str(Path(path).expanduser().resolve())


def repo_workspace_dir(root_path: str) -> Path:
    return workspace_root() / "repos" / _repo_hash(root_path)


def index_db_path(root_path: str) -> Path:
    return repo_workspace_dir(root_path) / "index.db"


def lock_path(root_path: str) -> Path:
    return repo_workspace_dir(root_path) / "index.lock"


# -- registry ----------------------------------------------------------
# A flat JSON file mapping hash -> {root_path, registered_at}. Exists so
# `repomind list` can answer "which repos have I indexed" without walking
# the filesystem for hash-named directories and guessing -- design.md
# section 6.3: "repomind list ... the answer to where did it go."


def _registry_path() -> Path:
    return workspace_root() / "registry.json"


def _read_registry() -> dict[str, dict[str, str]]:
    path = _registry_path()
    if not path.exists():
        return {}
    data: dict[str, dict[str, str]] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _write_registry(entries: dict[str, dict[str, str]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    root_path: str
    registered_at: str
    exists: bool
    """False when the directory has since moved or been deleted -- reported
    as stale rather than raised as an error (F-15 requirement 4)."""


def register_repo(root_path: str) -> None:
    entries = _read_registry()
    entries[_repo_hash(root_path)] = {
        "root_path": root_path,
        "registered_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _write_registry(entries)


def unregister_repo(root_path: str) -> None:
    entries = _read_registry()
    entries.pop(_repo_hash(root_path), None)
    _write_registry(entries)


def list_registered_repos() -> list[RegistryEntry]:
    return [
        RegistryEntry(
            root_path=v["root_path"],
            registered_at=v["registered_at"],
            exists=Path(v["root_path"]).exists(),
        )
        for v in _read_registry().values()
    ]


# -- advisory locking ----------------------------------------------------
# design.md section 12: "Concurrent index of one repo | Advisory lock file
# in the workspace; second process reports which PID holds it." A best-
# effort local mechanism, not a distributed mutex -- adequate for a
# single-machine dev tool.


def _is_pid_alive(pid: int) -> bool:
    # getattr(..., None), not `if sys.platform == "win32":` -- mypy specially
    # narrows *that exact* comparison the same way typeshed's ctypes stub
    # gates `windll`, which means whichever platform mypy actually runs on
    # (this dev machine vs. CI's ubuntu-latest, see .github/workflows/ci.yml
    # "quality" job) it prunes the *other* branch as unreachable, hiding a
    # real attr-defined error on one platform while false-flagging an unused
    # `type: ignore` on the other. getattr's return isn't attribute-checked,
    # so both branches type-check for real, on every platform, with no
    # ignore comment needed at all. Verified against both the default
    # (win32) mypy run and `mypy --platform linux` matching CI.
    windll = getattr(ctypes, "windll", None)
    if windll is not None:
        query_limited_info = 0x1000
        handle = windll.kernel32.OpenProcess(query_limited_info, False, pid)
        if not handle:
            return False
        windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    else:
        return True


@contextlib.contextmanager
def index_lock(root_path: str) -> Iterator[None]:
    """Hold the advisory lock for ``root_path``'s workspace for the
    duration of the ``with`` block.

    Raises :class:`WorkspaceLockedError` naming the holding PID if another
    live process already holds it. A lock left behind by a process that no
    longer exists is treated as stale and silently reclaimed.
    """
    path = lock_path(root_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            holder = json.loads(path.read_text(encoding="utf-8"))
            holder_pid = int(holder["pid"])
        except (json.JSONDecodeError, KeyError, ValueError):
            holder_pid = -1  # unreadable lock file: treat as stale
        if holder_pid > 0 and _is_pid_alive(holder_pid):
            raise WorkspaceLockedError(
                f"another RepoMind process (PID {holder_pid}) is already indexing "
                f"{root_path!r}. Wait for it to finish, or if it crashed, remove "
                f"{path} by hand."
            )

    path.write_text(
        json.dumps({"pid": os.getpid(), "acquired_at": datetime.now(UTC).isoformat()}),
        encoding="utf-8",
    )
    try:
        yield
    finally:
        # Only remove it if it's still ours -- a crashed-and-reclaimed lock
        # from another process should not be deleted out from under it.
        with contextlib.suppress(json.JSONDecodeError, KeyError, ValueError, FileNotFoundError):
            current = json.loads(path.read_text(encoding="utf-8"))
            if int(current["pid"]) == os.getpid():
                path.unlink(missing_ok=True)
