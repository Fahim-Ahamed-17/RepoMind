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
commands that call into pipelines which do not exist yet (M2's edges,
M3's retrieval, M4's synthesis) or inventing their shape ahead of the
tickets that actually determine it -- both are exactly what AGENTS.md's
"Rule zero" says not to do.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn

from repomind.errors import RepoMindError
from repomind.index.pipeline import IndexProgress, index_repository

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
    """Index a Python repository: parse it and persist its symbols.

    M1 scope only -- symbols, not yet relationships (M2), search (M3), or
    answers (M4). Always local: this command makes no network connection
    at any point (AGENTS.md invariant 1).
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
    console.print(f"  edges:    {edges_summary}  [dim](edge extraction lands in M2)[/dim]")
    console.print(f"  SHA:      {result.repo.indexed_sha or '[dim]none (not a git repo)[/dim]'}")


if __name__ == "__main__":
    app()
