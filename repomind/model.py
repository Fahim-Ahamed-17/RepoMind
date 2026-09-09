"""Core domain types.

Mirrors the schema in docs/design.md section 4.2 field-for-field. This module
has no dependency on storage, parsing, or any adapter -- it sits at the bottom
of the layering in design.md section 3 (``repomind.model | repomind.errors``)
so every other layer can import it without creating a cycle.

RM-010.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SymbolKind(StrEnum):
    """What a symbol row represents.

    ``FILE`` and ``MODULE`` are included deliberately -- design.md AD-3 folds
    files and modules into the symbol table rather than a separate node type,
    specifically so ``edge`` stays a clean symbol-to-symbol relation with two
    non-null foreign keys (a bare ``import x`` at module scope belongs to no
    function, and a separate file/symbol split would force every edge to be
    polymorphic to represent that).
    """

    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"


class EdgeKind(StrEnum):
    """What kind of relationship an edge records."""

    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    REFERENCES = "references"
    DEFINES = "defines"
    TESTS = "tests"


class Tier(StrEnum):
    """How an edge's relationship was determined.

    This is the central modelling decision in the whole system -- see
    docs/design.md section 4.3 and AGENTS.md invariant 3. The three tiers
    differ in *kind*, not degree, and must never be merged, flattened into a
    single score, or presented to a caller without their tier attached.

    v1.0 does not emit INFERRED edges (features.md F-2 requirement 2); the
    member exists now because the tier column and every query built against
    it must already understand three tiers before F-24 activates the third.
    """

    RESOLVED = "resolved"
    """A type-aware indexer (SCIP) produced this edge. Treat it as a fact."""

    HEURISTIC = "heuristic"
    """Name/scope matching produced this edge. Probably right, not certain."""

    INFERRED = "inferred"
    """A language model proposed this edge. A lead, not a fact. Unused in v1."""


class ScipStatus(StrEnum):
    """Whether SCIP resolution ran, and how it went, for a given repo."""

    OK = "ok"
    DEGRADED = "degraded"
    SKIPPED = "skipped"


class IndexRunStatus(StrEnum):
    """Lifecycle state of one indexing run, for resumability (F-1 requirement 9)."""

    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class Repo:
    """One indexed repository. ``id`` is unset (``None``) until persisted."""

    root_path: str
    """Absolute, normalised filesystem path. Unique per store (schema.sql)."""

    id: int | None = None
    remote_url: str | None = None
    indexed_sha: str | None = None
    """The commit this index describes. None if the repo predates any commit
    or is not a git repository (F-3 requirement 7)."""

    indexed_at: str | None = None
    """ISO-8601 UTC timestamp."""

    scip_status: ScipStatus | None = None


@dataclass(frozen=True, slots=True)
class File:
    """One source file within an indexed repo."""

    repo_id: int
    path: str
    """Repo-relative, forward slashes, regardless of host OS."""

    lang: str
    blob_sha: str
    """Content hash. Drives incremental-invalidation (F-3): unchanged content
    means an unchanged hash, so the file is skipped even if its mtime moved."""

    id: int | None = None
    n_lines: int | None = None


@dataclass(frozen=True, slots=True)
class Symbol:
    """One named entity: a file, module, class, function, method, or variable."""

    repo_id: int
    file_id: int
    kind: SymbolKind
    name: str
    qualified_name: str
    """E.g. ``pkg.mod.Class.method``. Globally unique within a repo for
    resolved symbols; heuristic extraction may collide, which is fine --
    collisions are a precision question for the heuristic tier, not a
    uniqueness invariant of this type."""

    start_line: int
    """1-indexed, inclusive."""

    end_line: int
    """1-indexed, inclusive."""

    id: int | None = None
    scip_symbol: str | None = None
    """SCIP global symbol id. None when SCIP did not resolve this symbol --
    that absence is exactly what makes an edge's tier HEURISTIC rather than
    RESOLVED; see docs/design.md section 4.2."""

    signature: str | None = None
    docstring: str | None = None


@dataclass(frozen=True, slots=True)
class Edge:
    """One directed relationship between two symbols, with provenance.

    ``tier`` is never optional and never derived -- it is supplied by
    whichever resolver produced the edge (design.md AD-4). A caller that
    wants "all edges regardless of tier" must ask for that explicitly; there
    is no code path that silently drops the tier and merges lists.
    """

    repo_id: int
    src_symbol_id: int
    dst_symbol_id: int
    kind: EdgeKind
    tier: Tier
    id: int | None = None
    confidence: float = 1.0
    """Ranks *within* a tier only. Never compared across tiers -- see
    docs/design.md section 4.3 for why that comparison is meaningless."""

    evidence_file_id: int | None = None
    evidence_line: int | None = None
    """Where the relationship is observable in source, if known."""


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrieval unit: a symbol-bounded span of source text.

    Populated starting in M3 (RM-030); the type lives here now because
    ``index_run`` and the store protocols need to know its shape.
    """

    repo_id: int
    file_id: int
    start_line: int
    end_line: int
    text: str
    n_tokens: int
    id: int | None = None
    symbol_id: int | None = None
    """None for the file-level remainder outside any extracted symbol."""


@dataclass(frozen=True, slots=True)
class IndexRun:
    """One indexing attempt. Exists for resumability and observability, not
    decoration -- see docs/design.md section 4.2 comment on this table."""

    repo_id: int
    started_at: str
    status: IndexRunStatus
    id: int | None = None
    finished_at: str | None = None
    from_sha: str | None = None
    to_sha: str | None = None
    files_done: int = 0
    files_total: int | None = None
    scip_status: ScipStatus | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedSymbol:
    """A symbol as extracted by a language pack, before it has a database
    identity. ``languages/`` produces these; ``index/pipeline.py`` assigns
    ``repo_id``/``file_id`` and persists them as :class:`Symbol` rows.
    """

    kind: SymbolKind
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    signature: str | None = None
    docstring: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """The result of parsing one file: its symbols, ready for persistence.

    Edge extraction is deliberately *not* part of this type -- M1 (RM-017,
    RM-018) produces symbols only; edges are M2's job (RM-020 onward), per
    implementation-plan.md's staged delivery of F-1/F-2.
    """

    path: str
    lang: str
    blob_sha: str
    n_lines: int
    symbols: list[ParsedSymbol] = field(default_factory=list)
