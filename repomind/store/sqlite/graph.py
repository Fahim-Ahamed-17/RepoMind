"""SQLite implementation of :class:`repomind.store.base.GraphStore` and
:class:`repomind.store.base.VectorStore`.

RM-012 (graph), RM-024 (multi-hop traversal), RM-032 (chunks, FTS5,
sqlite-vec) -- one class satisfying both protocols against one
connection, matching design.md AD-2's "one SQLite file per repo, holding
graph, metadata, FTS5, and vectors via sqlite-vec." All raw SQL for
either protocol lives here and nowhere else in the codebase (AGENTS.md
invariant 7) -- this is the file a future backend swap (design.md section
11.3) would replace, independently per protocol if only one side of it
needs replacing.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

import sqlite_vec

from repomind.errors import SchemaVersionError
from repomind.model import (
    Chunk,
    Edge,
    EdgeKind,
    File,
    IndexRun,
    IndexRunStatus,
    Repo,
    ScipStatus,
    Symbol,
    SymbolKind,
    Tier,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

#: Bumped whenever schema.sql changes shape. A mismatch on an existing
#: database means "prompt for --force reindex", never a silent migration
#: (design.md section 12: "a wrong silent migration is worse than a rebuild
#: that takes five minutes").
#: 2 (RM-032): chunk_fts sync triggers added.
CURRENT_SCHEMA_VERSION = 2

DEFAULT_LIST_LIMIT = 1000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SqliteGraphStore:
    """Owns one SQLite connection for one repo's index database.

    Structurally satisfies :class:`repomind.store.base.GraphStore` -- there
    is no inheritance relationship, per the Protocol pattern in
    docs/conventions.md.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._load_vec_extension()
        self._ensure_schema()

    def _load_vec_extension(self) -> None:
        """``sqlite-vec`` is a runtime-loaded extension (design.md AD-2):
        unlike FTS5, which is compiled into most SQLite builds, ``vec0``
        must be loaded onto *this* connection before ``chunk_vec`` can be
        created or queried -- and again on every future connection to the
        same file, since a loaded extension is a property of the
        connection, not something a schema version on disk can remember.
        Extension loading is disabled again immediately after, matching
        ``sqlite-vec``'s own documented usage -- there is no reason for
        this connection to load arbitrary further extensions afterwards.
        """
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING "
            "vec0(chunk_id INTEGER PRIMARY KEY, embedding FLOAT[384])"
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteGraphStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- schema ------------------------------------------------------------

    def _ensure_schema(self) -> None:
        (version,) = self._conn.execute("PRAGMA user_version").fetchone()
        if version == 0:
            schema_sql = (
                resources.files("repomind.store.sqlite")
                .joinpath("schema.sql")
                .read_text(encoding="utf-8")
            )
            with self._conn:
                self._conn.executescript(schema_sql)
                self._conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            return
        if version != CURRENT_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"index database is schema version {version}, this RepoMind build "
                f"expects {CURRENT_SCHEMA_VERSION}. Re-run with --force to rebuild "
                "the index rather than attempting a silent migration."
            )

    # -- repo ----------------------------------------------------------

    def upsert_repo(self, repo: Repo) -> Repo:
        """Ensures a row for ``repo.root_path`` exists, creating one on
        first call. On conflict, only the fields ``repo`` actually
        supplies (non-``None``) are updated -- ``COALESCE`` against the
        existing row, not a blind overwrite. index/pipeline.py's own
        ``_run`` calls this every single run with a bare
        ``Repo(root_path=...)`` just to fetch the row's id, then reads
        ``indexed_sha`` to pick the incremental diff base (RM-034). A
        blind overwrite would clear that on every call and restore it
        only if *that* run reached :meth:`set_repo_indexed_sha` -- so one
        interrupted run would silently downgrade the next to a full
        re-index. tests/unit/test_store_sqlite.py's
        ``test_upsert_repo_does_not_wipe_fields_it_was_not_given`` covers
        it, and fails against the previous blind-overwrite version.

        Returns the row as persisted, not the argument echoed back: after
        a ``COALESCE`` the two genuinely differ, and handing a caller
        fields that do not match what is stored is the same class of trap.
        """
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO repo (root_path, remote_url, indexed_sha, indexed_at, scip_status)
                VALUES (:root_path, :remote_url, :indexed_sha, :indexed_at, :scip_status)
                ON CONFLICT(root_path) DO UPDATE SET
                    remote_url = COALESCE(excluded.remote_url, repo.remote_url),
                    indexed_sha = COALESCE(excluded.indexed_sha, repo.indexed_sha),
                    indexed_at = COALESCE(excluded.indexed_at, repo.indexed_at),
                    scip_status = COALESCE(excluded.scip_status, repo.scip_status)
                RETURNING *
                """,
                {
                    "root_path": repo.root_path,
                    "remote_url": repo.remote_url,
                    "indexed_sha": repo.indexed_sha,
                    "indexed_at": repo.indexed_at,
                    "scip_status": repo.scip_status.value if repo.scip_status else None,
                },
            )
            row = cur.fetchone()
        return _row_to_repo(row)

    def get_repo(self, repo_id: int) -> Repo | None:
        row = self._conn.execute("SELECT * FROM repo WHERE id = ?", (repo_id,)).fetchone()
        return _row_to_repo(row) if row else None

    def get_repo_by_path(self, root_path: str) -> Repo | None:
        row = self._conn.execute("SELECT * FROM repo WHERE root_path = ?", (root_path,)).fetchone()
        return _row_to_repo(row) if row else None

    def set_repo_indexed_sha(
        self, repo_id: int, sha: str | None, scip_status: ScipStatus | None
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE repo SET indexed_sha = ?, indexed_at = ?, scip_status = ? WHERE id = ?",
                (sha, _now_iso(), scip_status.value if scip_status else None, repo_id),
            )

    def delete_repo(self, repo_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM repo WHERE id = ?", (repo_id,))

    # -- file ------------------------------------------------------------

    def upsert_file(self, file: File) -> File:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO file (repo_id, path, lang, blob_sha, n_lines)
                VALUES (:repo_id, :path, :lang, :blob_sha, :n_lines)
                ON CONFLICT(repo_id, path) DO UPDATE SET
                    lang = excluded.lang,
                    blob_sha = excluded.blob_sha,
                    n_lines = excluded.n_lines
                RETURNING id
                """,
                {
                    "repo_id": file.repo_id,
                    "path": file.path,
                    "lang": file.lang,
                    "blob_sha": file.blob_sha,
                    "n_lines": file.n_lines,
                },
            )
            (new_id,) = cur.fetchone()
        return File(
            id=new_id,
            repo_id=file.repo_id,
            path=file.path,
            lang=file.lang,
            blob_sha=file.blob_sha,
            n_lines=file.n_lines,
        )

    def get_file(self, file_id: int) -> File | None:
        row = self._conn.execute("SELECT * FROM file WHERE id = ?", (file_id,)).fetchone()
        return _row_to_file(row) if row else None

    def get_file_by_path(self, repo_id: int, path: str) -> File | None:
        row = self._conn.execute(
            "SELECT * FROM file WHERE repo_id = ? AND path = ?", (repo_id, path)
        ).fetchone()
        return _row_to_file(row) if row else None

    def list_files(self, repo_id: int, limit: int = DEFAULT_LIST_LIMIT) -> Sequence[File]:
        rows = self._conn.execute(
            "SELECT * FROM file WHERE repo_id = ? ORDER BY path LIMIT ?", (repo_id, limit)
        ).fetchall()
        return [_row_to_file(r) for r in rows]

    def delete_file(self, file_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM file WHERE id = ?", (file_id,))

    # -- symbol ------------------------------------------------------------

    def replace_symbols(self, file_id: int, symbols: Iterable[Symbol]) -> Sequence[Symbol]:
        result: list[Symbol] = []
        with self._conn:
            self._conn.execute("DELETE FROM symbol WHERE file_id = ?", (file_id,))
            for sym in symbols:
                cur = self._conn.execute(
                    """
                    INSERT INTO symbol
                        (repo_id, file_id, kind, name, qualified_name, scip_symbol,
                         start_line, end_line, signature, docstring)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sym.repo_id,
                        file_id,
                        sym.kind.value,
                        sym.name,
                        sym.qualified_name,
                        sym.scip_symbol,
                        sym.start_line,
                        sym.end_line,
                        sym.signature,
                        sym.docstring,
                    ),
                )
                result.append(
                    Symbol(
                        id=cur.lastrowid,
                        repo_id=sym.repo_id,
                        file_id=file_id,
                        kind=sym.kind,
                        name=sym.name,
                        qualified_name=sym.qualified_name,
                        scip_symbol=sym.scip_symbol,
                        start_line=sym.start_line,
                        end_line=sym.end_line,
                        signature=sym.signature,
                        docstring=sym.docstring,
                    )
                )
        return result

    def get_symbol(self, symbol_id: int) -> Symbol | None:
        row = self._conn.execute("SELECT * FROM symbol WHERE id = ?", (symbol_id,)).fetchone()
        return _row_to_symbol(row) if row else None

    def find_symbol_by_qualified_name(self, repo_id: int, qualified_name: str) -> Symbol | None:
        row = self._conn.execute(
            "SELECT * FROM symbol WHERE repo_id = ? AND qualified_name = ? LIMIT 1",
            (repo_id, qualified_name),
        ).fetchone()
        return _row_to_symbol(row) if row else None

    def find_symbol_at_location(self, file_id: int, line: int) -> Symbol | None:
        row = self._conn.execute(
            """
            SELECT * FROM symbol
            WHERE file_id = ? AND start_line <= ? AND end_line >= ?
            ORDER BY (end_line - start_line) ASC
            LIMIT 1
            """,
            (file_id, line, line),
        ).fetchone()
        return _row_to_symbol(row) if row else None

    def list_symbols(
        self, repo_id: int, file_id: int | None = None, limit: int = DEFAULT_LIST_LIMIT
    ) -> Sequence[Symbol]:
        if file_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM symbol WHERE repo_id = ? AND file_id = ? "
                "ORDER BY start_line LIMIT ?",
                (repo_id, file_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM symbol WHERE repo_id = ? ORDER BY id LIMIT ?",
                (repo_id, limit),
            ).fetchall()
        return [_row_to_symbol(r) for r in rows]

    def all_symbols(self, repo_id: int) -> Sequence[Symbol]:
        rows = self._conn.execute(
            "SELECT * FROM symbol WHERE repo_id = ? ORDER BY id", (repo_id,)
        ).fetchall()
        return [_row_to_symbol(r) for r in rows]

    def count_symbols_by_kind(self, repo_id: int) -> dict[str, int]:
        counts = {k.value: 0 for k in SymbolKind}
        rows = self._conn.execute(
            "SELECT kind, COUNT(*) AS n FROM symbol WHERE repo_id = ? GROUP BY kind",
            (repo_id,),
        ).fetchall()
        counts.update({r["kind"]: r["n"] for r in rows})
        return counts

    def set_symbol_scip_ids(self, scip_symbol_by_id: Mapping[int, str]) -> None:
        with self._conn:
            self._conn.executemany(
                "UPDATE symbol SET scip_symbol = ? WHERE id = ?",
                [(scip_symbol, symbol_id) for symbol_id, scip_symbol in scip_symbol_by_id.items()],
            )

    # -- edge ----------------------------------------------------------

    def insert_edges(self, edges: Iterable[Edge]) -> Sequence[Edge]:
        result: list[Edge] = []
        with self._conn:
            for e in edges:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO edge
                        (repo_id, src_symbol_id, dst_symbol_id, kind, tier,
                         confidence, evidence_file_id, evidence_line)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        e.repo_id,
                        e.src_symbol_id,
                        e.dst_symbol_id,
                        e.kind.value,
                        e.tier.value,
                        e.confidence,
                        e.evidence_file_id,
                        e.evidence_line,
                    ),
                )
                # Re-select by natural key: INSERT OR IGNORE gives no
                # lastrowid on a pre-existing row, and we want a real id
                # either way (freshly inserted or already there).
                row = self._conn.execute(
                    """
                    SELECT * FROM edge
                    WHERE src_symbol_id = ? AND dst_symbol_id = ? AND kind = ? AND tier = ?
                    """,
                    (e.src_symbol_id, e.dst_symbol_id, e.kind.value, e.tier.value),
                ).fetchone()
                result.append(_row_to_edge(row))
        return result

    def delete_edges_from_file(self, evidence_file_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM edge WHERE evidence_file_id = ?", (evidence_file_id,))

    def edges_from(
        self,
        symbol_id: int,
        kind: EdgeKind | None = None,
        tiers: Sequence[Tier] | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> Sequence[Edge]:
        return self._edges_one_hop("src_symbol_id", symbol_id, kind, tiers, limit)

    def edges_to(
        self,
        symbol_id: int,
        kind: EdgeKind | None = None,
        tiers: Sequence[Tier] | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> Sequence[Edge]:
        return self._edges_one_hop("dst_symbol_id", symbol_id, kind, tiers, limit)

    def _edges_one_hop(
        self,
        anchor_column: str,
        symbol_id: int,
        kind: EdgeKind | None,
        tiers: Sequence[Tier] | None,
        limit: int,
    ) -> Sequence[Edge]:
        assert anchor_column in ("src_symbol_id", "dst_symbol_id")  # internal guard, not user input
        clauses = [f"{anchor_column} = ?"]
        params: list[object] = [symbol_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if tiers:
            placeholders = ",".join("?" for _ in tiers)
            clauses.append(f"tier IN ({placeholders})")
            params.extend(t.value for t in tiers)
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM edge WHERE {' AND '.join(clauses)} LIMIT ?",  # noqa: S608
            params,
        ).fetchall()
        return [_row_to_edge(r) for r in rows]

    def reverse_dependencies(
        self,
        symbol_id: int,
        max_depth: int,
        tiers: Sequence[Tier] | None = None,
    ) -> Sequence[tuple[Edge, int]]:
        # UNION (not UNION ALL) dedups the CTE's rows by their full
        # (symbol_id, depth, edge_id) tuple. Combined with the hard
        # `depth < max_depth` bound, that is what makes a cyclic import
        # graph terminate rather than hang: a cycle can still revisit the
        # same symbol at a *different* depth (a new, undeduped row), but
        # never at the same (symbol, depth, edge) triple twice, and the
        # depth bound caps how many times that can happen at all. See
        # design.md section 4.4 -- this is that query, with `edge_id`
        # threaded through so the caller gets real edges back, not bare
        # ids, and reads out a full row per (symbol, depth) rather than a
        # single edge lost to whichever one happened to insert first.
        tier_clause = ""
        params: list[object] = [symbol_id, max_depth]
        if tiers:
            placeholders = ",".join("?" for _ in tiers)
            tier_clause = f"AND e.tier IN ({placeholders})"
            params.extend(t.value for t in tiers)

        rows = self._conn.execute(
            f"""
            WITH RECURSIVE upstream(symbol_id, depth, edge_id) AS (
                SELECT ?, 0, NULL
              UNION
                SELECT e.src_symbol_id, u.depth + 1, e.id
                FROM edge e
                JOIN upstream u ON e.dst_symbol_id = u.symbol_id
                WHERE u.depth < ?
                  {tier_clause}
            )
            SELECT edge.*, u.depth AS hop_depth
            FROM upstream u
            JOIN edge ON edge.id = u.edge_id
            WHERE u.depth > 0
              AND u.depth = (
                  SELECT MIN(u2.depth) FROM upstream u2 WHERE u2.symbol_id = u.symbol_id
              )
              -- A diamond (A -> B -> D and A -> C -> D) reaches A via two
              -- *different* edges (A->B, A->C) at the same shortest depth
              -- and the same kind -- collapse those to one representative
              -- (lowest edge id, for determinism), since both say the same
              -- thing about A's relationship to the seed. A different KIND
              -- from the same symbol at the same depth is not collapsed:
              -- "A imports B" and "A calls B" are genuinely distinct facts.
              AND edge.id = (
                  SELECT MIN(e3.id) FROM upstream u3
                  JOIN edge e3 ON e3.id = u3.edge_id
                  WHERE u3.symbol_id = u.symbol_id
                    AND u3.depth = u.depth
                    AND e3.kind = edge.kind
              )
            ORDER BY u.depth, edge.src_symbol_id, edge.kind
            """,  # noqa: S608 -- tier_clause interpolates only "?" placeholders, never a value
            params,
        ).fetchall()
        return [(_row_to_edge(r), r["hop_depth"]) for r in rows]

    def count_edges_by_tier(self, repo_id: int) -> dict[str, int]:
        counts = {t.value: 0 for t in Tier}
        rows = self._conn.execute(
            "SELECT tier, COUNT(*) AS n FROM edge WHERE repo_id = ? GROUP BY tier",
            (repo_id,),
        ).fetchall()
        counts.update({r["tier"]: r["n"] for r in rows})
        return counts

    # -- index_run -------------------------------------------------------

    def start_index_run(
        self,
        repo_id: int,
        from_sha: str | None,
        to_sha: str | None,
        files_total: int | None,
    ) -> IndexRun:
        started_at = _now_iso()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO index_run
                    (repo_id, started_at, from_sha, to_sha, status, files_done, files_total)
                VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (repo_id, started_at, from_sha, to_sha, IndexRunStatus.RUNNING.value, files_total),
            )
        return IndexRun(
            id=cur.lastrowid,
            repo_id=repo_id,
            started_at=started_at,
            status=IndexRunStatus.RUNNING,
            from_sha=from_sha,
            to_sha=to_sha,
            files_total=files_total,
        )

    def update_index_run_progress(self, run_id: int, files_done: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE index_run SET files_done = ? WHERE id = ?", (files_done, run_id)
            )

    def finish_index_run(
        self, run_id: int, status: IndexRunStatus, error: str | None = None
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE index_run SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                (status.value, _now_iso(), error, run_id),
            )

    def get_latest_index_run(self, repo_id: int) -> IndexRun | None:
        row = self._conn.execute(
            "SELECT * FROM index_run WHERE repo_id = ? ORDER BY id DESC LIMIT 1",
            (repo_id,),
        ).fetchone()
        return _row_to_index_run(row) if row else None

    # -- chunk / FTS5 / sqlite-vec (VectorStore) --------------------------

    def replace_chunks(self, file_id: int, chunks: Iterable[Chunk]) -> Sequence[Chunk]:
        """Mirrors :meth:`replace_symbols` exactly: delete this file's
        existing chunks, insert the given ones. Both ``chunk_fts`` and
        ``chunk_vec`` follow automatically via schema.sql's own ``chunk_ad``
        trigger, which fires on this method's own DELETE just as it does
        on a cascaded one from :meth:`delete_file`/:meth:`delete_repo` --
        this method never touches either table directly. ``chunk_vec``
        specifically has no FK to enforce that cleanup itself (sqlite-vec's
        ``vec0`` module does not support one), which is exactly why
        ``chunk_ad`` deletes from it explicitly rather than relying on
        ``ON DELETE CASCADE`` the way ``symbol``/``edge`` do.
        """
        result: list[Chunk] = []
        with self._conn:
            self._conn.execute("DELETE FROM chunk WHERE file_id = ?", (file_id,))
            for chunk in chunks:
                cur = self._conn.execute(
                    """
                    INSERT INTO chunk
                        (repo_id, file_id, symbol_id, start_line, end_line, text, n_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.repo_id,
                        file_id,
                        chunk.symbol_id,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.text,
                        chunk.n_tokens,
                    ),
                )
                result.append(
                    Chunk(
                        id=cur.lastrowid,
                        repo_id=chunk.repo_id,
                        file_id=file_id,
                        symbol_id=chunk.symbol_id,
                        start_line=chunk.start_line,
                        end_line=chunk.end_line,
                        text=chunk.text,
                        n_tokens=chunk.n_tokens,
                    )
                )
        return result

    def set_chunk_embeddings(self, embeddings: Mapping[int, Sequence[float]]) -> None:
        with self._conn:
            for chunk_id, vector in embeddings.items():
                # Delete-then-insert, not INSERT OR REPLACE: confirmed
                # directly that vec0 does not honour ON CONFLICT the way a
                # regular table does (it still raises the PRIMARY KEY
                # violation). Re-embedding an already-vectorised chunk --
                # a caller retrying after a partial embed failure, most
                # plausibly -- must not fail on that account.
                self._conn.execute("DELETE FROM chunk_vec WHERE chunk_id = ?", (chunk_id,))
                self._conn.execute(
                    "INSERT INTO chunk_vec (chunk_id, embedding) VALUES (?, ?)",
                    (chunk_id, sqlite_vec.serialize_float32(list(vector))),
                )

    def get_chunk(self, chunk_id: int) -> Chunk | None:
        row = self._conn.execute("SELECT * FROM chunk WHERE id = ?", (chunk_id,)).fetchone()
        return _row_to_chunk(row) if row else None

    def count_chunks(self, repo_id: int) -> int:
        (count,) = self._conn.execute(
            "SELECT COUNT(*) FROM chunk WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        return int(count)

    def search_fts(
        self, repo_id: int, query: str, limit: int = DEFAULT_LIST_LIMIT
    ) -> Sequence[tuple[Chunk, float]]:
        rows = self._conn.execute(
            """
            SELECT chunk.*, bm25(chunk_fts) AS score
            FROM chunk_fts
            JOIN chunk ON chunk.id = chunk_fts.rowid
            WHERE chunk_fts MATCH ? AND chunk.repo_id = ?
            ORDER BY score
            LIMIT ?
            """,
            (query, repo_id, limit),
        ).fetchall()
        return [(_row_to_chunk(r), r["score"]) for r in rows]

    def search_vector(
        self, repo_id: int, embedding: Sequence[float], limit: int = DEFAULT_LIST_LIMIT
    ) -> Sequence[tuple[Chunk, float]]:
        # sqlite-vec's own KNN syntax requires a bare "k = ?" bound to run
        # at all (confirmed directly) -- filtering to this repo happens
        # afterwards in the outer query, not inside the vec0 MATCH itself,
        # since chunk_vec carries no repo_id of its own to filter on. A
        # single-repo-per-database file (design.md AD-5) means every row
        # already belongs to repo_id in practice; the WHERE clause here is
        # the same defensive belt-and-suspenders every other repo_id
        # parameter in this file is, not load-bearing today.
        rows = self._conn.execute(
            """
            SELECT chunk.*, chunk_vec.distance AS score
            FROM chunk_vec
            JOIN chunk ON chunk.id = chunk_vec.chunk_id
            WHERE chunk_vec.embedding MATCH ? AND chunk_vec.k = ? AND chunk.repo_id = ?
            ORDER BY score
            """,
            (sqlite_vec.serialize_float32(list(embedding)), limit, repo_id),
        ).fetchall()
        return [(_row_to_chunk(r), r["score"]) for r in rows]


# -- row mapping ---------------------------------------------------------
# Kept as free functions rather than methods: they are pure, and keeping
# them off the class makes it obvious they touch no connection state.


def _row_to_repo(row: sqlite3.Row) -> Repo:
    return Repo(
        id=row["id"],
        root_path=row["root_path"],
        remote_url=row["remote_url"],
        indexed_sha=row["indexed_sha"],
        indexed_at=row["indexed_at"],
        scip_status=ScipStatus(row["scip_status"]) if row["scip_status"] else None,
    )


def _row_to_file(row: sqlite3.Row) -> File:
    return File(
        id=row["id"],
        repo_id=row["repo_id"],
        path=row["path"],
        lang=row["lang"],
        blob_sha=row["blob_sha"],
        n_lines=row["n_lines"],
    )


def _row_to_symbol(row: sqlite3.Row) -> Symbol:
    return Symbol(
        id=row["id"],
        repo_id=row["repo_id"],
        file_id=row["file_id"],
        kind=SymbolKind(row["kind"]),
        name=row["name"],
        qualified_name=row["qualified_name"],
        scip_symbol=row["scip_symbol"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        signature=row["signature"],
        docstring=row["docstring"],
    )


def _row_to_edge(row: sqlite3.Row) -> Edge:
    return Edge(
        id=row["id"],
        repo_id=row["repo_id"],
        src_symbol_id=row["src_symbol_id"],
        dst_symbol_id=row["dst_symbol_id"],
        kind=EdgeKind(row["kind"]),
        tier=Tier(row["tier"]),
        confidence=row["confidence"],
        evidence_file_id=row["evidence_file_id"],
        evidence_line=row["evidence_line"],
    )


def _row_to_chunk(row: sqlite3.Row) -> Chunk:
    return Chunk(
        id=row["id"],
        repo_id=row["repo_id"],
        file_id=row["file_id"],
        symbol_id=row["symbol_id"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        text=row["text"],
        n_tokens=row["n_tokens"],
    )


def _row_to_index_run(row: sqlite3.Row) -> IndexRun:
    return IndexRun(
        id=row["id"],
        repo_id=row["repo_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        from_sha=row["from_sha"],
        to_sha=row["to_sha"],
        status=IndexRunStatus(row["status"]),
        files_done=row["files_done"],
        files_total=row["files_total"],
        scip_status=ScipStatus(row["scip_status"]) if row["scip_status"] else None,
        error=row["error"],
    )
