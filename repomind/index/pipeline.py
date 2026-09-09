"""Index pipeline orchestration: discover -> parse -> persist.

RM-018. M1 scope only, per implementation-plan.md's staged delivery:
symbols in a database. No edges (RM-020 onward, M2), no chunks or
embeddings (RM-030 onward, M3), no SCIP (RM-022, time-boxed, also M2).
``repo.scip_status`` is set to SKIPPED here, not because SCIP failed, but
because this pipeline does not attempt it yet.

Resumability, stated precisely (F-1 requirement 9: "a re-run resumes
rather than restarting"): this pipeline's notion of resumability is
*safe to re-run*, not *skip already-completed files*. Every per-file write
(``replace_symbols``) is idempotent, so interrupting a run and calling
``index_repository`` again produces a correct, complete index with no
duplicate or orphaned rows -- it does not yet skip files a previous partial
run already finished. Genuine skip-unchanged-files resumption falls out of
RM-034's incremental-invalidation machinery (M3, blob_sha comparison),
which will make a restart fast as well as safe; building a separate,
throwaway checkpoint-resume scheme now that RM-034 will likely reshape
would be premature.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from pathlib import Path
from typing import TYPE_CHECKING

from repomind.errors import IndexingError
from repomind.ingest.discover import discover_files
from repomind.ingest.git import current_sha
from repomind.languages import get_language_pack_for_extension
from repomind.model import File, IndexRunStatus, Repo, ScipStatus, Symbol
from repomind.store.sqlite.graph import SqliteGraphStore
from repomind.workspace import index_db_path, index_lock, normalize_repo_path, register_repo

if TYPE_CHECKING:
    from collections.abc import Callable

    from repomind.languages.base import LanguagePack
    from repomind.model import IndexRun


@dataclasses.dataclass(frozen=True, slots=True)
class IndexProgress:
    """Passed to the optional progress callback. The pipeline never prints
    anything itself -- presentation is a surface's job (AD-1), not the
    library's.
    """

    files_done: int
    files_total: int
    current_path: str


@dataclasses.dataclass(frozen=True, slots=True)
class IndexResult:
    """The end-of-run summary (F-1 requirement 6)."""

    repo: Repo
    index_run: IndexRun
    files_indexed: int
    files_skipped: int
    """Discovered but not processed: unsupported language, or undecodable
    as UTF-8. Distinct from files discover_files already excluded (vendor
    dirs, size cap, .gitignore) -- those never reach this count at all."""
    symbol_counts: dict[str, int]
    edge_counts: dict[str, int]
    """All zero in M1 -- no edge extraction yet. Present now so this
    result's shape doesn't change once M2 lands."""
    elapsed_seconds: float


def index_repository(
    root: Path,
    *,
    progress_callback: Callable[[IndexProgress], None] | None = None,
) -> IndexResult:
    """Index ``root`` and persist the result to its central workspace
    (``~/.repomind/repos/<hash>/index.db``, design.md AD-5). Holds the
    repo's advisory lock for the duration
    (:func:`repomind.workspace.index_lock`).
    """
    started = time.monotonic()
    root = root.resolve()
    root_path_str = normalize_repo_path(root)

    with index_lock(root_path_str):
        store = SqliteGraphStore(index_db_path(root_path_str))
        try:
            result = _run(root, root_path_str, store, progress_callback)
        finally:
            store.close()

    register_repo(root_path_str)
    return dataclasses.replace(result, elapsed_seconds=time.monotonic() - started)


def _run(
    root: Path,
    root_path_str: str,
    store: SqliteGraphStore,
    progress_callback: Callable[[IndexProgress], None] | None,
) -> IndexResult:
    repo = store.upsert_repo(Repo(root_path=root_path_str))
    assert repo.id is not None  # upsert_repo always assigns one

    previous_run = store.get_latest_index_run(repo.id)
    from_sha = previous_run.to_sha if previous_run else None
    to_sha = current_sha(root)

    discovered = list(discover_files(root))
    run = store.start_index_run(repo.id, from_sha, to_sha, files_total=len(discovered))
    assert run.id is not None  # start_index_run always assigns one

    files_indexed = 0
    files_skipped = 0
    try:
        for i, disc in enumerate(discovered, start=1):
            pack = get_language_pack_for_extension(disc.abs_path.suffix)
            if pack is None:
                files_skipped += 1
            else:
                try:
                    raw = disc.abs_path.read_bytes()
                    text = raw.decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    files_skipped += 1
                else:
                    blob_sha = hashlib.sha256(raw).hexdigest()
                    _index_one_file(store, repo.id, disc.rel_path, text, blob_sha, pack)
                    files_indexed += 1

            store.update_index_run_progress(run.id, i)
            if progress_callback is not None:
                progress_callback(
                    IndexProgress(
                        files_done=i, files_total=len(discovered), current_path=disc.rel_path
                    )
                )

        store.set_repo_indexed_sha(repo.id, to_sha, ScipStatus.SKIPPED)
        store.finish_index_run(run.id, IndexRunStatus.OK)
    except Exception as exc:
        # Deliberately broad: this is the top-level run boundary. Any
        # failure here must still mark the run interrupted rather than
        # leave it stuck at RUNNING, or `status` would report a phantom
        # in-progress index forever (design.md section 12).
        store.finish_index_run(run.id, IndexRunStatus.INTERRUPTED, error=str(exc))
        raise IndexingError(f"indexing {root_path_str!r} failed: {exc}") from exc

    repo_id = repo.id
    repo = store.get_repo(repo_id) or repo  # pick up indexed_sha/scip_status just set
    return IndexResult(
        repo=repo,
        index_run=store.get_latest_index_run(repo_id) or run,
        files_indexed=files_indexed,
        files_skipped=files_skipped,
        symbol_counts=store.count_symbols_by_kind(repo_id),
        edge_counts=store.count_edges_by_tier(repo_id),
        elapsed_seconds=0.0,  # filled in by index_repository, once total is known
    )


def _index_one_file(
    store: SqliteGraphStore,
    repo_id: int,
    rel_path: str,
    text: str,
    blob_sha: str,
    pack: LanguagePack,
) -> None:
    # Path(...), not PurePosixPath: pathlib.Path accepts "/" as a separator
    # on every platform including Windows, and parse_python_file normalises
    # back to forward slashes itself via .as_posix() -- so a plain Path
    # round-trips rel_path correctly everywhere, while still satisfying the
    # LanguagePack protocol's `path: Path` parameter (PurePosixPath is not
    # a Path per typeshed, they're sibling PurePath subclasses).
    parsed = pack.parse_file(Path(rel_path), text, blob_sha)

    file_row = store.upsert_file(
        File(
            repo_id=repo_id,
            path=rel_path,
            lang=parsed.lang,
            blob_sha=blob_sha,
            n_lines=parsed.n_lines,
        )
    )
    assert file_row.id is not None  # upsert_file always assigns one

    symbols = [
        Symbol(
            repo_id=repo_id,
            file_id=file_row.id,
            kind=ps.kind,
            name=ps.name,
            qualified_name=ps.qualified_name,
            start_line=ps.start_line,
            end_line=ps.end_line,
            signature=ps.signature,
            docstring=ps.docstring,
        )
        for ps in parsed.symbols
    ]
    store.replace_symbols(file_row.id, symbols)
