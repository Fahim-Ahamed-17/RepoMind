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
