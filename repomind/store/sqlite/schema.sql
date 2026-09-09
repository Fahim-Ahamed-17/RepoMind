-- RepoMind SQLite schema. Verbatim from docs/design.md section 4.2 -- if you
-- need to change this, the design doc changes first (AGENTS.md "Rule zero").
--
-- One file per repo, holding graph + metadata + FTS5 + sqlite-vec together
-- (design.md AD-2), so a crash cannot desynchronise graph and vector writes.

PRAGMA foreign_keys = ON;

-- Schema version lives in SQLite's own PRAGMA user_version (an integer
-- stashed in the database header), not a table -- design.md section 12 and
-- conventions.md both say "schema_version pragma" literally. See
-- store/sqlite/graph.py CURRENT_SCHEMA_VERSION / _check_schema_version().

CREATE TABLE IF NOT EXISTS repo (
    id            INTEGER PRIMARY KEY,
    root_path     TEXT NOT NULL UNIQUE,   -- absolute, normalised
    remote_url    TEXT,
    indexed_sha   TEXT,                   -- the commit this index describes
    indexed_at    TEXT,
    scip_status   TEXT                    -- ok | degraded | skipped
);

CREATE TABLE IF NOT EXISTS file (
    id            INTEGER PRIMARY KEY,
    repo_id       INTEGER NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
    path          TEXT NOT NULL,          -- repo-relative, forward slashes
    lang          TEXT NOT NULL,
    blob_sha      TEXT NOT NULL,          -- content hash: drives incremental invalidation
    n_lines       INTEGER,
    UNIQUE (repo_id, path)
);

CREATE TABLE IF NOT EXISTS symbol (
    id             INTEGER PRIMARY KEY,
    repo_id        INTEGER NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
    file_id        INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,         -- file|module|class|function|method|variable
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,         -- pkg.mod.Class.method
    scip_symbol    TEXT,                  -- SCIP global id; NULL when unresolved
    start_line     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    signature      TEXT,
    docstring      TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbol_qname ON symbol(repo_id, qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbol_scip  ON symbol(repo_id, scip_symbol) WHERE scip_symbol IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_symbol_file  ON symbol(file_id);

-- The central table. Tier is first-class, never derived (design.md 4.3, AD-4).
CREATE TABLE IF NOT EXISTS edge (
    id               INTEGER PRIMARY KEY,
    repo_id          INTEGER NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
    src_symbol_id    INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    dst_symbol_id    INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,       -- calls|imports|inherits|references|defines|tests
    tier             TEXT NOT NULL CHECK (tier IN ('resolved','heuristic','inferred')),
    confidence       REAL NOT NULL DEFAULT 1.0,
    evidence_file_id INTEGER REFERENCES file(id) ON DELETE SET NULL,
    evidence_line    INTEGER,             -- where the relationship is observable in source
    UNIQUE (src_symbol_id, dst_symbol_id, kind, tier)
);
-- Reverse traversal is the hot path: "who depends on X" walks dst -> src.
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(dst_symbol_id, tier, kind);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edge(src_symbol_id, tier, kind);

CREATE TABLE IF NOT EXISTS chunk (
    id            INTEGER PRIMARY KEY,
    repo_id       INTEGER NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
    file_id       INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    symbol_id     INTEGER REFERENCES symbol(id) ON DELETE CASCADE,  -- NULL: file remainder
    start_line    INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    text          TEXT NOT NULL,
    n_tokens      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_symbol ON chunk(symbol_id);

-- FTS5 and sqlite-vec (chunk_vec) are created by store/sqlite/graph.py at
-- connection time, not here: sqlite-vec is a runtime-loaded extension, so its
-- virtual table must be created after the extension is loaded onto the
-- connection, which a static schema file executed by a plain sqlite3
-- connection cannot guarantee. See graph.py _ensure_schema().
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    text, content=chunk, content_rowid=id, tokenize=unicode61
);

-- Observability and resumability, not decoration (design.md 4.2 comment).
CREATE TABLE IF NOT EXISTS index_run (
    id            INTEGER PRIMARY KEY,
    repo_id       INTEGER NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    from_sha      TEXT,
    to_sha        TEXT,
    status        TEXT NOT NULL,          -- running|ok|failed|interrupted
    files_done    INTEGER DEFAULT 0,
    files_total   INTEGER,
    scip_status   TEXT,
    error         TEXT
);
