"""Index pipeline orchestration: discover -> parse -> resolve -> chunk ->
embed -> persist.

RM-018 (symbols) + RM-020/RM-021 (heuristic edges) + RM-022 (resolved
edges via SCIP) + RM-030/031/032 (chunks, embeddings, persistence).

Edge computation happens in three passes, for the reason design.md's own
flow diagram separates them:

  * ``defines`` edges are computed *per file*, right after that file's
    symbols are persisted (``_index_one_file``) -- both ends are already
    known within that one file (``ParsedSymbol.parent_qualified_name``),
    so there is nothing to gain by waiting.
  * Every other kind (imports, inherits, calls, references) needs the
    *whole repo's* symbol table, since a reference routinely crosses file
    boundaries -- deferred to one pass after every file has been parsed
    and persisted (``_run``, calling ``resolve_heuristic_edges``).
  * The same references are then given a *second* chance to resolve via
    SCIP (``resolve_scip_edges``), which needs both the whole symbol table
    (same reason as above) and SCIP's own subprocess output -- so it runs
    last, after the heuristic pass, not instead of it. Per design.md AD-9,
    a SCIP failure (missing binary, crash, timeout -- see
    ``languages/python/scip.py``'s module docstring for the specific,
    empirically-confirmed Windows failure modes it works around) is caught
    here and degrades to ``scip_status = ScipStatus.DEGRADED`` rather than
    failing the run: the heuristic edges already persisted stand
    regardless.

Chunking and embedding follow a similar per-file/whole-repo split, for a
different reason than edge resolution: chunking needs nothing beyond one
file's own already-parsed symbols (design.md section 10), so it happens
per file, right alongside ``replace_symbols`` (``_index_one_file``);
embedding is deferred to one call over every chunk in the whole repo
(``_run``), so :class:`~repomind.embed.base.Embedder` implementations get
to batch internally (design.md: "batches at 32 chunks") rather than
this module re-deriving that batching itself. Unlike a SCIP failure, an
embedding failure is never caught here -- design.md's own failure table:
"indexing without embeddings is not a useful partial state" -- so it
propagates to this function's own top-level exception handler like any
other genuine failure, marking the run interrupted.

Resumability, stated precisely (F-1 requirement 9: "a re-run resumes
rather than restarting"): this pipeline's notion of resumability is
*safe to re-run*, not *skip already-completed files*. Every per-file write
(``replace_symbols``, ``replace_chunks``) is idempotent, so interrupting a
run and calling ``index_repository`` again produces a correct, complete
index with no duplicate or orphaned rows -- it does not yet skip files a
previous partial run already finished. Genuine skip-unchanged-files
resumption falls out of RM-034's incremental-invalidation machinery (M3,
blob_sha comparison), which will make a restart fast as well as safe;
building a separate, throwaway checkpoint-resume scheme now that RM-034
will likely reshape would be premature.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from repomind.embed.local import LocalEmbedder
from repomind.errors import IndexingError, ScipUnavailableError
from repomind.index.chunker import chunk_file
from repomind.index.resolve import (
    defines_edges_for_file,
    resolve_heuristic_edges,
    resolve_scip_edges,
)
from repomind.ingest.discover import discover_files
from repomind.ingest.git import current_sha
from repomind.languages import get_language_pack_for_extension

# Deliberately Python-specific, not dispatched through LanguagePack: RM-022
# is Python-only by its own ticket scope. RM-071 (scip-typescript) is a
# separate, later ticket -- if and when a second language needs SCIP, that
# is where a per-language dispatch seam would be introduced, not invented
# speculatively now for one caller.
from repomind.languages.python.scip import run_scip_python
from repomind.model import Chunk, File, IndexRunStatus, Repo, ScipStatus, Symbol
from repomind.store.sqlite.graph import SqliteGraphStore
from repomind.workspace import (
    index_db_path,
    index_lock,
    normalize_repo_path,
    register_repo,
    repo_workspace_dir,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from repomind.languages.base import LanguagePack
    from repomind.model import IndexRun, ParsedReference

log = structlog.get_logger()


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
    """Keyed by tier (``resolved``/``heuristic``/``inferred``), per
    design.md section 4.3 -- never merged. ``resolved`` is 0 whenever SCIP
    was skipped (``--no-scip``) or degraded; ``heuristic`` reflects real
    ``defines`` and resolved-reference edges regardless (RM-020/RM-021)."""
    chunk_count: int
    """Retrieval units persisted (RM-030/032), all embedded by the time
    this result is returned -- an embedding failure fails the whole run
    (this module's own docstring), so there is no "chunked but not yet
    embedded" state for a caller to observe here."""
    elapsed_seconds: float


def index_repository(
    root: Path,
    *,
    progress_callback: Callable[[IndexProgress], None] | None = None,
    use_scip: bool = True,
) -> IndexResult:
    """Index ``root`` and persist the result to its central workspace
    (``~/.repomind/repos/<hash>/index.db``, design.md AD-5). Holds the
    repo's advisory lock for the duration
    (:func:`repomind.workspace.index_lock`).

    ``use_scip=False`` is ``--no-scip`` (F-1 requirement 10): skip the SCIP
    subprocess entirely rather than attempt and degrade. This is a genuine
    security control, not a convenience flag -- design.md AD-9 and section
    9.3 both state plainly that indexing an untrusted repository with SCIP
    enabled is equivalent to running that repository's build (SCIP
    indexers execute in, and may import, the target repo's own code). The
    resulting ``repo.scip_status`` is ``SKIPPED`` (never attempted), which
    reads differently from ``DEGRADED`` (attempted, failed) on purpose --
    see ``repomind.model.ScipStatus``.
    """
    started = time.monotonic()
    root = root.resolve()
    root_path_str = normalize_repo_path(root)

    with index_lock(root_path_str):
        store = SqliteGraphStore(index_db_path(root_path_str))
        try:
            result = _run(root, root_path_str, store, progress_callback, use_scip=use_scip)
        finally:
            store.close()

    register_repo(root_path_str)
    return dataclasses.replace(result, elapsed_seconds=time.monotonic() - started)


def _run(
    root: Path,
    root_path_str: str,
    store: SqliteGraphStore,
    progress_callback: Callable[[IndexProgress], None] | None,
    *,
    use_scip: bool,
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
    # (file_id, references) per successfully-parsed file, retained across
    # the loop for the whole-repo resolution pass below -- resolving a
    # reference needs the complete symbol table, which does not exist
    # until every file has been persisted. See this module's docstring.
    file_references: list[tuple[int, list[ParsedReference]]] = []
    # RM-022: SCIP's own output is keyed by relative path, not our file
    # ids -- built alongside file_references at zero extra store cost
    # (every file_id here was already returned by _index_one_file above),
    # rather than a second pass over store.list_files, which is capped at
    # DEFAULT_LIST_LIMIT and so is not safe to rely on for "every file".
    file_id_to_rel_path: dict[int, str] = {}
    # Every chunk persisted this run, across every file -- embedded in one
    # batch after the loop rather than per file, so the Embedder gets to
    # batch internally (this module's own docstring).
    all_chunks: list[Chunk] = []
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
                    file_id, references, chunks = _index_one_file(
                        store, repo.id, disc.rel_path, text, blob_sha, pack
                    )
                    file_references.append((file_id, references))
                    file_id_to_rel_path[file_id] = disc.rel_path
                    all_chunks.extend(chunks)
                    files_indexed += 1

            store.update_index_run_progress(run.id, i)
            if progress_callback is not None:
                progress_callback(
                    IndexProgress(
                        files_done=i, files_total=len(discovered), current_path=disc.rel_path
                    )
                )

        # Whole-repo pass: every kind of edge except `defines` (already
        # computed per file above) needs the complete symbol table, since a
        # reference routinely crosses file boundaries.
        all_symbols = store.all_symbols(repo.id)
        heuristic_edges = resolve_heuristic_edges(repo.id, all_symbols, file_references)
        store.insert_edges(heuristic_edges)

        scip_status = _run_scip(
            store,
            repo.id,
            root,
            root_path_str,
            to_sha,
            file_id_to_rel_path,
            file_references,
            all_symbols,
            use_scip=use_scip,
        )

        _embed_chunks(store, all_chunks)

        store.set_repo_indexed_sha(repo.id, to_sha, scip_status)
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
        chunk_count=store.count_chunks(repo_id),
        elapsed_seconds=0.0,  # filled in by index_repository, once total is known
    )


def _embed_chunks(store: SqliteGraphStore, chunks: list[Chunk]) -> None:
    """RM-031/032: embed every chunk from this run in one batch and
    persist the vectors. Deliberately raises straight through to
    ``_run``'s own top-level exception handler on any failure -- unlike
    ``_run_scip``, there is no degraded status to record here (design.md's
    failure table: "indexing without embeddings is not a useful partial
    state").

    ``LocalEmbedder()`` is constructed here, not once per
    ``index_repository`` call higher up, so a run with zero chunks (an
    empty repo, or one with only unsupported files) never even imports
    ``fastembed`` -- consistent with :class:`~repomind.embed.local.LocalEmbedder`
    itself only loading the actual model lazily, on first real use.
    """
    if not chunks:
        return
    embedder = LocalEmbedder()
    vectors = embedder.embed([c.text for c in chunks])
    embeddings = {c.id: v for c, v in zip(chunks, vectors, strict=True) if c.id is not None}
    store.set_chunk_embeddings(embeddings)


def _run_scip(
    store: SqliteGraphStore,
    repo_id: int,
    root: Path,
    root_path_str: str,
    to_sha: str | None,
    file_id_to_rel_path: dict[int, str],
    file_references: list[tuple[int, list[ParsedReference]]],
    all_symbols: Sequence[Symbol],
    *,
    use_scip: bool,
) -> ScipStatus:
    """RM-022: attempt SCIP resolution and report how it went.

    Never raises: a SCIP failure is exactly the case
    :class:`ScipUnavailableError` exists to carry, caught here and turned
    into ``DEGRADED`` per design.md AD-9 ("degrade loudly: log, record
    status, tell the user" -- docs/conventions.md's Logging section, whose
    own worked example is this exact situation). ``use_scip=False`` skips
    the attempt entirely, distinct from an attempt that failed -- see
    :func:`index_repository`'s docstring on why ``SKIPPED`` and
    ``DEGRADED`` must stay distinguishable.
    """
    if not use_scip:
        return ScipStatus.SKIPPED

    try:
        scip_index = run_scip_python(
            root,
            repo_workspace_dir(root_path_str) / "index.scip",
            project_version=to_sha,
        )
    except ScipUnavailableError as exc:
        log.warning("scip.degraded", repo=repo_id, reason=str(exc))
        return ScipStatus.DEGRADED

    # rel_path is unique per (repo_id, path) -- File's own uniqueness
    # constraint (schema.sql) -- so inverting file_id_to_rel_path back to
    # rel_path -> file_id is lossless.
    rel_path_to_file_id = {path: file_id for file_id, path in file_id_to_rel_path.items()}

    # SCIP symbol string -> our own persisted Symbol.id, built from SCIP's
    # Definition occurrences via find_symbol_at_location (same technique
    # index/resolve.py's own docstring points to). A (path, line) with no
    # entry in rel_path_to_file_id is a document SCIP type-checked that we
    # never indexed ourselves (stdlib, an installed dependency) -- not an
    # error, just nothing to attach it to.
    scip_symbol_to_our_id: dict[str, int] = {}
    for (rel_path, line), scip_symbol in scip_index.definitions.items():
        file_id = rel_path_to_file_id.get(rel_path)
        if file_id is None:
            continue
        sym = store.find_symbol_at_location(file_id, line + 1)  # SCIP is 0-indexed
        if sym is not None and sym.id is not None:
            scip_symbol_to_our_id[scip_symbol] = sym.id

    store.set_symbol_scip_ids({our_id: s for s, our_id in scip_symbol_to_our_id.items()})

    resolved_edges = resolve_scip_edges(
        repo_id,
        all_symbols,
        file_references,
        file_id_to_rel_path,
        scip_index,
        scip_symbol_to_our_id,
    )
    store.insert_edges(resolved_edges)
    return ScipStatus.OK


def _index_one_file(
    store: SqliteGraphStore,
    repo_id: int,
    rel_path: str,
    text: str,
    blob_sha: str,
    pack: LanguagePack,
) -> tuple[int, list[ParsedReference], Sequence[Chunk]]:
    """Parse, persist this file's symbols, ``defines`` edges, and chunks,
    and return ``(file_id, references, chunks)`` for the caller's deferred
    whole-repo resolution and embedding passes (see this module's
    docstring).
    """
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

    # Clears both this file's own `defines` edges and any edge from an
    # earlier run whose *evidence* pointed here (a call/import/etc. found
    # in this file's source, regardless of which file the target symbol
    # lives in) -- so re-indexing this same file on a later run does not
    # accumulate duplicates or leave a stale edge from source that changed.
    # Symmetric with replace_symbols' own idempotency, just below.
    store.delete_edges_from_file(file_row.id)

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
    persisted = store.replace_symbols(file_row.id, symbols)

    defines = defines_edges_for_file(repo_id, file_row.id, parsed.symbols, persisted)
    store.insert_edges(defines)

    qname_to_symbol_id = {s.qualified_name: s.id for s in persisted}
    chunks = [
        Chunk(
            repo_id=repo_id,
            file_id=file_row.id,
            symbol_id=(
                qname_to_symbol_id.get(pc.symbol_qualified_name)
                if pc.symbol_qualified_name is not None
                else None
            ),
            start_line=pc.start_line,
            end_line=pc.end_line,
            text=pc.text,
            n_tokens=pc.n_tokens,
        )
        for pc in chunk_file(parsed, text)
    ]
    persisted_chunks = store.replace_chunks(file_row.id, chunks)

    return file_row.id, parsed.references, persisted_chunks
