"""Domain error hierarchy.

Per docs/conventions.md "Errors": raise domain errors, never bare ValueError or
Exception. Every message says what failed, why, and what the user can do.
"""

from __future__ import annotations


class RepoMindError(Exception):
    """Base class for all RepoMind errors."""


class IndexingError(RepoMindError):
    """Indexing a repository failed."""


class NotIndexedError(RepoMindError):
    """The repository has no index yet."""


class StaleIndexError(RepoMindError):
    """The index is behind HEAD by more than the configured drift threshold."""


class ProviderError(RepoMindError):
    """An LLM provider call failed."""


class EgressDeniedError(RepoMindError):
    """A remote call was blocked by deny_remote policy.

    Never caught and retried elsewhere -- this is a policy decision, not a
    transient fault. See docs/design.md section 6.4 and AGENTS.md invariant 4.
    """


class SchemaVersionError(RepoMindError):
    """The on-disk database schema does not match this version of RepoMind."""


class WorkspaceLockedError(RepoMindError):
    """Another process holds the advisory lock for this repo's workspace."""


class ScipUnavailableError(RepoMindError):
    """SCIP resolution could not run or produced nothing usable.

    Always caught internally by index/pipeline.py (design.md AD-9): a
    degraded run is not a failed run. Never crosses to a surface as an
    exit code -- see docs/conventions.md's own worked example for this
    exact situation ("scip-python not found on PATH; install it or pass
    --no-scip"), which is what this message should read like.
    """


class EmbeddingError(RepoMindError):
    """The local embedding model could not be loaded or run.

    Unlike :class:`ScipUnavailableError`, this is never caught and
    degraded from -- design.md's own failure table is explicit:
    "Indexing without embeddings is not a useful partial state." A
    repository with no `resolved` edges is still fully searchable by
    heuristic edges and full-text; one with no vectors has no semantic
    search at all, which is F-5's entire purpose. Raised naming the model,
    the cache location, and that a manual download there is the fix
    (docs/conventions.md's own error-message rule).
    """
