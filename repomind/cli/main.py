"""The CLI entry point.

Deliberately minimal right now: just enough of ``repomind index`` to make
M1's own exit criterion literally demonstrable (implementation-plan.md
section 2: "`repomind index .` ... produces symbols with correct spans")
and to give ``pyproject.toml``'s ``repomind = "repomind.cli.main:app"``
console script something real to import -- without one, ``pip install -e .``
would ship a broken command.

The full CLI surface (``ask``, ``search``, ``refs``, ``impact``, ``graph``,
``list``, ``status``, ``serve``, ``--json`` on every read command, exit
codes) is RM-045 in M4. Building that now would mean either faking
commands that call into pipelines which do not exist yet (M3's retrieval,
M4's synthesis) or inventing their shape ahead of the tickets that
actually determine it -- both are exactly what AGENTS.md's "Rule zero"
says not to do. A later milestone may add one more command early, the
same way this file already does for ``index``, purely to keep that
milestone's own exit criterion checkable -- not the full surface F-7 (or
any other feature) eventually specifies.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn

from repomind.analyze.reverse import DEFAULT_DEPTH, MAX_DEPTH, find_references
from repomind.errors import RepoMindError
from repomind.index.pipeline import IndexProgress, index_repository
from repomind.model import Tier
from repomind.store.sqlite.graph import SqliteGraphStore
from repomind.workspace import index_db_path, normalize_repo_path

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
) -> None:
    """Index a Python repository: parse it and persist its symbols and
    relationships.

    M1+M2 scope so far -- symbols and heuristic-tier edges, not yet search
    (M3) or answers (M4); the `resolved` tier needs SCIP (RM-022, not yet
    landed). Always local: this command makes no network connection at
    any point (AGENTS.md invariant 1).
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
            result = index_repository(root, progress_callback=on_progress)
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
    console.print(
        f"  edges:    {edges_summary}  [dim](resolved tier requires SCIP -- RM-022)[/dim]"
    )
    console.print(f"  SHA:      {result.repo.indexed_sha or '[dim]none (not a git repo)[/dim]'}")


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


if __name__ == "__main__":
    app()
