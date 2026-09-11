"""The CLI entry point.

Deliberately minimal: just enough of the surface to make each landed
milestone's own exit criterion demonstrable (implementation-plan.md
section 2), plus ``list``/``status``/``remove`` (F-15, RM-025) -- central
storage (design.md AD-5) is unusable without a way to discover and manage
what has been indexed, and F-15 depends only on RM-013 (M1), not on
anything M3/M4 still has to build. ``search`` (F-5) joined them once M3
landed (RM-030 through RM-035) for the same reason: its whole dependency
chain -- chunking, embedding, FTS5/sqlite-vec, RRF fusion, graph
expansion -- was already built, unlike the rest of RM-045's bundle below.

The rest of the full CLI surface (``ask``, ``impact``, ``graph``,
``serve``, ``--json`` on every read command, exit codes) is RM-045 in M4.
Building that now would mean either faking commands that call into
pipelines which do not exist yet (M4's synthesis, egress) or inventing
their shape ahead of the tickets that actually determine it -- both are
exactly what AGENTS.md's "Rule zero" says not to do.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn

from repomind.analyze.registry import RepoListing, repo_listing, repo_status
from repomind.analyze.reverse import DEFAULT_DEPTH, MAX_DEPTH, find_references
from repomind.embed.local import LocalEmbedder
from repomind.errors import RepoMindError, WorkspaceLockedError
from repomind.index.pipeline import IndexProgress, index_repository
from repomind.ingest.git import commits_behind, current_sha
from repomind.model import ScipStatus, SymbolKind, Tier
from repomind.retrieve.search import DEFAULT_RESULT_LIMIT
from repomind.retrieve.search import search as search_code
from repomind.store.sqlite.graph import SqliteGraphStore
from repomind.workspace import (
    index_db_path,
    list_registered_repos,
    normalize_repo_path,
    remove_repo_workspace,
    workspace_dir_size,
)

app = typer.Typer(
    name="repomind",
    help="Repository intelligence that runs on your machine.",
    add_completion=False,
)
console = Console()


@app.callback()
def _callback() -> None:
    """Empty on purpose: with exactly one @app.command() registered right
    now, Typer would otherwise collapse into "no subcommand name" mode
    (`repomind PATH` instead of `repomind index PATH`) -- a documented
    Typer behaviour that would silently change shape again the moment a
    second command is added in M4. This callback forces explicit
    subcommand mode from day one, matching design.md section 6.3's stated
    interface (`repomind index [PATH] ...`).
    """


@app.command()
def index(
    path: Path = typer.Argument(
        Path(), help="Repository to index. Defaults to the current directory."
    ),
    no_scip: bool = typer.Option(
        False,
        "--no-scip",
        help="Skip the SCIP subprocess entirely (F-1 requirement 10). Use this for "
        "untrusted repositories: SCIP indexers execute in, and may import, the "
        "target repo's own code (design.md AD-9, section 9.3).",
    ),
) -> None:
    """Index a Python repository: parse it and persist its symbols and
    relationships.

    M1+M2 scope -- symbols, heuristic-tier edges, and (unless --no-scip)
    resolved-tier edges via SCIP; not yet search (M3) or answers (M4).
    Always local: this command makes no network connection at any point
    (AGENTS.md invariant 1) -- SCIP is a local subprocess, not a network
    call, so it is unaffected by that invariant, but --no-scip is still the
    right choice for code you do not trust to execute.
    """
    # Output below is deliberately ASCII-only. Rich's legacy Windows console
    # writer (the code path older cmd.exe-style terminals take) can fail
    # outright on a Unicode glyph such as a checkmark, while plain styled
    # ASCII renders fine through the same path -- verified directly against
    # this failure mode. A CLI's own output should not be what breaks on a
    # user's terminal.
    root = path.resolve()
    if not root.is_dir():
        console.print(f"[red]error:[/red] {root} is not a directory")
        raise typer.Exit(code=1)

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.fields[total_display]}"),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task("indexing...", total=None, total_display="?")

        def on_progress(p: IndexProgress) -> None:
            if progress.tasks[0].total is None:
                progress.update(task_id, total=p.files_total, total_display=p.files_total)
            progress.update(
                task_id, completed=p.files_done, description=f"parsing {p.current_path}"
            )

        try:
            result = index_repository(root, progress_callback=on_progress, use_scip=not no_scip)
        except RepoMindError as exc:
            progress.stop()
            console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(code=1) from exc

    console.print(
        f"[green]done.[/green] indexed [bold]{root}[/bold] in {result.elapsed_seconds:.2f}s"
    )
    console.print(f"  files:    {result.files_indexed} indexed, {result.files_skipped} skipped")
    console.print(
        "  symbols:  " + ", ".join(f"{k}={v}" for k, v in result.symbol_counts.items() if v)
    )
    edges_summary = ", ".join(f"{k}={v}" for k, v in result.edge_counts.items())
    console.print(f"  edges:    {edges_summary}")
    console.print(f"  chunks:   {result.chunk_count} (embedded, searchable)")
    console.print(f"  SHA:      {result.repo.indexed_sha or '[dim]none (not a git repo)[/dim]'}")

    # "Tell the user" (docs/conventions.md Logging section) -- scip_status
    # is recorded either way, but a degraded run is easy to miss in a
    # summary that otherwise looks like full success.
    if result.repo.scip_status == ScipStatus.DEGRADED:
        console.print(
            "  [yellow]warning:[/yellow] SCIP unavailable this run -- resolved-tier "
            "edges are empty, heuristic tier only. Install scip-python "
            "(`npm install -g @sourcegraph/scip-python@0.3.0` -- not `latest`, which "
            "crashes on Windows) and re-index to fix."
        )
    elif no_scip:
        console.print("  [dim](SCIP skipped: --no-scip)[/dim]")


@app.command()
def refs(
    symbol: str = typer.Argument(
        ..., help="Qualified name (pkg.mod.Class.method) or path/to/file.py:line."
    ),
    depth: int = typer.Option(
        DEFAULT_DEPTH, "--depth", help=f"Hops to walk transitively, clamped to 1-{MAX_DEPTH}."
    ),
    tier: str | None = typer.Option(
        None, "--tier", help="Restrict to one confidence tier (resolved/heuristic/inferred)."
    ),
) -> None:
    """List what depends on SYMBOL: its callers and referrers, grouped by
    confidence tier, with the source location each relationship was found
    at (F-7). Operates on the current directory's already-built index --
    run `repomind index .` first. Requires no LLM (AGENTS.md invariant 5).
    """
    root = Path.cwd()
    root_path_str = normalize_repo_path(root)
    db_path = index_db_path(root_path_str)
    if not db_path.exists():
        console.print(f"[red]error:[/red] {root} has not been indexed yet. Run `repomind index .`")
        raise typer.Exit(code=1)

    store = SqliteGraphStore(db_path)
    try:
        repo = store.get_repo_by_path(root_path_str)
        if repo is None or repo.id is None:
            console.print(
                f"[red]error:[/red] {root} has not been indexed yet. Run `repomind index .`"
            )
            raise typer.Exit(code=1)

        tiers: tuple[Tier, ...] | None = None
        if tier is not None:
            try:
                tiers = (Tier(tier),)
            except ValueError:
                valid = ", ".join(t.value for t in Tier)
                console.print(f"[red]error:[/red] --tier must be one of: {valid}")
                raise typer.Exit(code=1) from None

        result = find_references(store, repo.id, symbol, depth=depth, tiers=tiers)
        if result is None:
            console.print(f"[red]error:[/red] no symbol found matching {symbol!r}")
            raise typer.Exit(code=1)

        if not result.hits:
            console.print(f"nothing depends on [bold]{result.target.qualified_name}[/bold]")
            return

        console.print(f"what depends on [bold]{result.target.qualified_name}[/bold]:")
        for hit_tier, hits in result.by_tier().items():
            console.print(f"\n[bold]{hit_tier.value}[/bold] ({len(hits)}):")
            for hit in hits:
                loc = (
                    f"{hit.evidence_path}:{hit.evidence_line}"
                    if hit.evidence_path is not None
                    else "(no evidence location)"
                )
                # Rich markup, not literal brackets: console.print() treats a
                # bare "[xxx]" as a style tag and silently drops anything it
                # doesn't recognise (verified directly against this failure
                # mode -- an earlier version tried literal "[function]" and
                # every kind label vanished with no error at all). "[dim]"
                # here is real markup, styling hit.source.kind.value itself,
                # not decorative brackets around it.
                console.print(
                    f"  depth={hit.depth}  [dim]{hit.source.kind.value}[/dim] "
                    f"{hit.source.qualified_name}  [dim]({hit.kind.value}, {loc})[/dim]"
                )
    finally:
        store.close()


@app.command()
def search(
    query: str = typer.Argument(..., help="Natural-language question or identifier to search for."),
    limit: int = typer.Option(
        DEFAULT_RESULT_LIMIT, "--limit", help="Maximum number of results to return."
    ),
    language: list[str] = typer.Option(
        [], "--language", help="Restrict to a language (repeatable). Default: every language."
    ),
    path_prefix: str | None = typer.Option(
        None, "--path-prefix", help="Restrict to paths starting with this prefix."
    ),
    kind: list[str] = typer.Option(
        [], "--kind", help="Restrict to a symbol kind (repeatable). Default: every kind."
    ),
) -> None:
    """Hybrid code search: vector similarity and FTS5 keyword matching,
    fused by Reciprocal Rank Fusion (F-5). Requires no LLM -- this is
    retrieval only, mode D (AGENTS.md invariant 5). Operates on the
    current directory's already-built index -- run `repomind index .`
    first.
    """
    root = Path.cwd()
    root_path_str = normalize_repo_path(root)
    db_path = index_db_path(root_path_str)
    if not db_path.exists():
        console.print(f"[red]error:[/red] {root} has not been indexed yet. Run `repomind index .`")
        raise typer.Exit(code=1)

    store = SqliteGraphStore(db_path)
    try:
        repo = store.get_repo_by_path(root_path_str)
        if repo is None or repo.id is None:
            console.print(
                f"[red]error:[/red] {root} has not been indexed yet. Run `repomind index .`"
            )
            raise typer.Exit(code=1)

        kinds: tuple[SymbolKind, ...] | None = None
        if kind:
            try:
                kinds = tuple(SymbolKind(k) for k in kind)
            except ValueError:
                valid = ", ".join(k.value for k in SymbolKind)
                console.print(f"[red]error:[/red] --kind must be one of: {valid}")
                raise typer.Exit(code=1) from None

        try:
            hits = search_code(
                store,
                LocalEmbedder(),
                repo.id,
                query,
                limit=limit,
                languages=language or None,
                path_prefix=path_prefix,
                kinds=kinds,
            )
        except RepoMindError as exc:
            console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(code=1) from exc

        if not hits:
            console.print("[dim]no results.[/dim]")
            return

        for hit in hits:
            location = f"{hit.file.path}:{hit.chunk.start_line}-{hit.chunk.end_line}"
            symbol_display = (
                f"  [dim]{hit.symbol.qualified_name}[/dim]" if hit.symbol is not None else ""
            )
            console.print(f"[bold]{location}[/bold]{symbol_display}")
            # First 3 non-blank lines, not just the first: a chunk merging
            # a short symbol with its surrounding remainder (chunker.py's
            # MERGE_BELOW_TOKENS) often opens with a module docstring, one
            # line of which says nothing about what actually matched.
            preview_lines = [line for line in hit.chunk.text.splitlines() if line.strip()][:3]
            for line in preview_lines:
                console.print(f"  {line.strip()[:100]}")
    finally:
        store.close()


def _format_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@app.command(name="list")
def list_repos() -> None:
    """List every indexed repository: path, SHA, SCIP status, size on disk
    (F-15 requirement 1), plus a total across all of them (requirement 5).
    A registry entry whose source repository has moved or been deleted is
    reported as such rather than raised as an error (requirement 4).
    """
    entries = list_registered_repos()
    if not entries:
        console.print("[dim]no repositories indexed yet. Run `repomind index .` in one.[/dim]")
        return

    total_bytes = 0
    for entry in entries:
        size_bytes = workspace_dir_size(entry.root_path)
        total_bytes += size_bytes

        db_path = index_db_path(entry.root_path)
        if db_path.exists():
            store = SqliteGraphStore(db_path)
            try:
                listing = repo_listing(
                    store, entry.root_path, exists=entry.exists, size_bytes=size_bytes
                )
            finally:
                store.close()
        else:
            # Registered but nothing on disk -- e.g. the workspace directory
            # was removed by hand rather than through `repomind remove`.
            listing = RepoListing(
                root_path=entry.root_path,
                exists=entry.exists,
                indexed_sha=None,
                indexed_at=None,
                scip_status=None,
                size_bytes=0,
            )

        stale = "" if listing.exists else "  [yellow](repository not found on disk)[/yellow]"
        console.print(f"[bold]{listing.root_path}[/bold]{stale}")
        sha_display = listing.indexed_sha[:12] if listing.indexed_sha else "[dim]none[/dim]"
        scip_display = listing.scip_status.value if listing.scip_status else "[dim]n/a[/dim]"
        console.print(
            f"  SHA: {sha_display}  indexed: {listing.indexed_at or '[dim]never[/dim]'}  "
            f"scip: {scip_display}  size: {_format_size(size_bytes)}"
        )

    repo_word = "repository" if len(entries) == 1 else "repositories"
    console.print(f"\n{len(entries)} {repo_word}, {_format_size(total_bytes)} total")


@app.command()
def status(
    path: Path = typer.Argument(
        Path(), help="Repository to report on. Defaults to the current directory."
    ),
) -> None:
    """Report SHA drift, SCIP status, and per-tier symbol/edge counts for
    one repository's index (F-15 requirement 2).
    """
    root = path.resolve()
    root_path_str = normalize_repo_path(root)
    db_path = index_db_path(root_path_str)
    if not db_path.exists():
        console.print(f"[red]error:[/red] {root} has not been indexed yet. Run `repomind index .`")
        raise typer.Exit(code=1)

    store = SqliteGraphStore(db_path)
    try:
        repo = store.get_repo_by_path(root_path_str)
        if repo is None or repo.id is None:
            console.print(
                f"[red]error:[/red] {root} has not been indexed yet. Run `repomind index .`"
            )
            raise typer.Exit(code=1)

        current = current_sha(root)
        behind = (
            commits_behind(root, repo.indexed_sha, current)
            if repo.indexed_sha is not None and current is not None
            else None
        )
        report = repo_status(
            store,
            repo.id,
            root_path_str,
            current_sha=current,
            commits_behind=behind,
            size_bytes=workspace_dir_size(root_path_str),
        )
        assert report is not None  # repo.id was just read from this same store

        console.print(f"[bold]{root}[/bold]")
        if report.indexed_sha is None:
            console.print("  SHA:      [dim]none (not a git repo)[/dim]")
        elif report.current_sha is None:
            console.print(
                f"  SHA:      {report.indexed_sha}  [dim](working copy: not a git repo)[/dim]"
            )
        elif report.indexed_sha == report.current_sha:
            console.print(f"  SHA:      {report.indexed_sha}  [green](up to date)[/green]")
        elif report.commits_behind is not None:
            console.print(
                f"  SHA:      {report.indexed_sha}  [yellow]({report.commits_behind} "
                f"commit(s) behind {report.current_sha})[/yellow]"
            )
        else:
            console.print(
                f"  SHA:      {report.indexed_sha}  [yellow](drifted from "
                f"{report.current_sha}, exact count unknown -- history rewritten?)[/yellow]"
            )

        scip_display = report.scip_status.value if report.scip_status else "[dim]n/a[/dim]"
        console.print(f"  scip:     {scip_display}")
        console.print(
            "  symbols:  " + ", ".join(f"{k}={v}" for k, v in report.symbol_counts.items() if v)
        )
        console.print("  edges:    " + ", ".join(f"{k}={v}" for k, v in report.edge_counts.items()))
        console.print(f"  chunks:   {report.chunk_count}")
        if report.last_run_status is not None:
            duration = (
                f"{report.last_run_duration_seconds:.2f}s"
                if report.last_run_duration_seconds is not None
                else "[dim]unknown[/dim]"
            )
            console.print(f"  last run: {report.last_run_status.value}, {duration}")
        console.print(f"  size:     {_format_size(report.size_bytes)}")

        # F-2 requirement 6 / design.md's own failure table: stated again
        # here, not just at index time -- a degraded run is easy to miss
        # in a summary from a `repomind index` invocation that already
        # scrolled off screen by the time someone checks `refs --tier
        # resolved` and wonders why it is empty.
        if report.scip_status == ScipStatus.DEGRADED:
            console.print(
                "  [yellow]warning:[/yellow] resolved-tier edges are empty -- SCIP was "
                "unavailable on the last index run."
            )
    finally:
        store.close()


@app.command()
def remove(
    path: Path = typer.Argument(..., help="Repository whose index should be deleted."),
) -> None:
    """Delete a repository's index (F-15 requirement 3). Never touches the
    repository itself -- only this tool's own copy under ``~/.repomind``.
    """
    root = path.resolve()
    root_path_str = normalize_repo_path(root)
    try:
        removed = remove_repo_workspace(root_path_str)
    except WorkspaceLockedError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if removed:
        console.print(f"[green]done.[/green] removed index for {root}")
    else:
        console.print(f"[dim]nothing to remove for {root} (no index found).[/dim]")


if __name__ == "__main__":
    app()
